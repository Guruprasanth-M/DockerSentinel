"""Detailed system information endpoints — htop/Task Manager level metrics.

Reads from /host_proc (bind-mounted host /proc) and /host_sys (host /sys)
to provide comprehensive hardware and OS information.
"""

from __future__ import annotations

import os
import re
import time
import subprocess
from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter

log = structlog.get_logger()

router = APIRouter()

HOST_PROC = "/host_proc"
HOST_SYS = "/host_sys"

# ── Per-core CPU tracking ────────────────────────────────────────────────────
_prev_per_core: Optional[List[List[int]]] = None
_prev_per_core_ts: float = 0.0
_prev_disk_stats: Optional[Dict[str, Dict]] = None
_prev_disk_ts: float = 0.0
_prev_net_counters: Optional[Dict[str, Dict]] = None
_prev_net_ts: float = 0.0

# ── Caching layer ────────────────────────────────────────────────────────────
# Static data (hostname, OS, CPU model, IPs, disk capacity) rarely changes.
# Cache it and only refresh dynamic data (usage %, rates) on each call.
_static_cache: Dict[str, Any] = {}
_static_cache_ts: float = 0.0
_STATIC_CACHE_TTL = 300.0  # 5 minutes — static hw info doesn't change often


# ── CPU Information ──────────────────────────────────────────────────────────

def _read_cpu_info() -> Dict[str, Any]:
    """Read detailed CPU info from /proc/cpuinfo."""
    info: Dict[str, Any] = {
        "model": "Unknown",
        "vendor": "Unknown",
        "physical_cores": 0,
        "logical_cores": 0,
        "frequency_mhz": {"current": 0.0, "min": 0.0, "max": 0.0},
        "cache": {"l1d": "N/A", "l1i": "N/A", "l2": "N/A", "l3": "N/A"},
        "per_core_usage": [],
        "total_usage": 0.0,
        "architecture": "unknown",
    }

    try:
        with open(os.path.join(HOST_PROC, "cpuinfo"), "r") as f:
            content = f.read()

        # Parse model name
        m = re.search(r"model name\s*:\s*(.+)", content)
        if m:
            info["model"] = m.group(1).strip()

        # Vendor
        m = re.search(r"vendor_id\s*:\s*(.+)", content)
        if m:
            info["vendor"] = m.group(1).strip()

        # Count logical cores (each "processor" entry)
        processors = re.findall(r"^processor\s*:", content, re.MULTILINE)
        info["logical_cores"] = len(processors)

        # Physical cores (unique core id per physical id)
        physical_ids = set()
        core_ids = set()
        for block in content.split("\n\n"):
            pid = re.search(r"physical id\s*:\s*(\d+)", block)
            cid = re.search(r"core id\s*:\s*(\d+)", block)
            if pid and cid:
                physical_ids.add(pid.group(1))
                core_ids.add((pid.group(1), cid.group(1)))
        info["physical_cores"] = len(core_ids) if core_ids else info["logical_cores"]

        # CPU MHz
        m = re.search(r"cpu MHz\s*:\s*([\d.]+)", content)
        if m:
            info["frequency_mhz"]["current"] = round(float(m.group(1)), 1)

        # Cache size (usually L2 or L3 from cpuinfo)
        m = re.search(r"cache size\s*:\s*(.+)", content)
        if m:
            info["cache"]["l3"] = m.group(1).strip()

        # Try reading frequency limits from /sys
        try:
            freq_path = os.path.join(HOST_SYS, "devices/system/cpu/cpu0/cpufreq")
            if os.path.exists(freq_path):
                for name, key in [("cpuinfo_min_freq", "min"), ("cpuinfo_max_freq", "max"),
                                  ("scaling_cur_freq", "current")]:
                    fpath = os.path.join(freq_path, name)
                    if os.path.exists(fpath):
                        with open(fpath) as ff:
                            val = int(ff.read().strip()) / 1000  # kHz to MHz
                            info["frequency_mhz"][key] = round(val, 1)
        except Exception:
            pass

        # Try reading cache sizes from /sys
        try:
            cache_base = os.path.join(HOST_SYS, "devices/system/cpu/cpu0/cache")
            if os.path.exists(cache_base):
                for idx_dir in sorted(os.listdir(cache_base)):
                    idx_path = os.path.join(cache_base, idx_dir)
                    if not os.path.isdir(idx_path):
                        continue
                    try:
                        with open(os.path.join(idx_path, "level")) as ff:
                            level = ff.read().strip()
                        with open(os.path.join(idx_path, "type")) as ff:
                            cache_type = ff.read().strip()
                        with open(os.path.join(idx_path, "size")) as ff:
                            size = ff.read().strip()
                        if level == "1" and "Data" in cache_type:
                            info["cache"]["l1d"] = size
                        elif level == "1" and "Instruction" in cache_type:
                            info["cache"]["l1i"] = size
                        elif level == "2":
                            info["cache"]["l2"] = size
                        elif level == "3":
                            info["cache"]["l3"] = size
                    except Exception:
                        pass
        except Exception:
            pass

        # Architecture
        m = re.search(r"^flags\s*:.*\blm\b", content, re.MULTILINE)
        info["architecture"] = "x86_64" if m else "x86"
        # Override with uname if available
        try:
            with open(os.path.join(HOST_PROC, "version"), "r") as f:
                ver = f.read()
            if "aarch64" in ver or "arm64" in ver:
                info["architecture"] = "aarch64"
        except Exception:
            pass

    except Exception as e:
        log.warning("cpu_info_read_error", error=str(e))

    # Per-core usage
    info["per_core_usage"], info["total_usage"] = _read_per_core_cpu()

    return info


def _read_per_core_cpu() -> tuple:
    """Read per-core CPU usage from /proc/stat.

    On the first call, does a quick 100ms self-sample to return real data
    instead of empty arrays.
    """
    global _prev_per_core, _prev_per_core_ts

    per_core_usage = []
    total_usage = 0.0

    try:
        with open(os.path.join(HOST_PROC, "stat"), "r") as f:
            lines = f.readlines()

        cores = []
        for line in lines:
            if line.startswith("cpu") and not line.startswith("cpu "):
                parts = line.split()
                times = [int(x) for x in parts[1:9]]
                cores.append(times)

        # Overall
        overall = lines[0].split()
        overall_times = [int(x) for x in overall[1:9]]

        # On first call, do a quick self-sample (100ms) so we return real data
        if _prev_per_core is None:
            _prev_per_core = cores
            _read_per_core_cpu._prev_overall = overall_times
            _prev_per_core_ts = time.time()
            time.sleep(0.1)
            # Re-read
            with open(os.path.join(HOST_PROC, "stat"), "r") as f:
                lines = f.readlines()
            cores = []
            for line in lines:
                if line.startswith("cpu") and not line.startswith("cpu "):
                    parts = line.split()
                    times = [int(x) for x in parts[1:9]]
                    cores.append(times)
            overall = lines[0].split()
            overall_times = [int(x) for x in overall[1:9]]

        # Per-core deltas
        for i, cur in enumerate(cores):
            if i < len(_prev_per_core):
                prev = _prev_per_core[i]
                total_diff = sum(cur) - sum(prev)
                idle_diff = (cur[3] + cur[4]) - (prev[3] + prev[4])
                if total_diff > 0:
                    per_core_usage.append(round(((total_diff - idle_diff) / total_diff) * 100, 1))
                else:
                    per_core_usage.append(0.0)

        # Total
        if hasattr(_read_per_core_cpu, '_prev_overall'):
            prev_o = _read_per_core_cpu._prev_overall
            td = sum(overall_times) - sum(prev_o)
            id_ = (overall_times[3] + overall_times[4]) - (prev_o[3] + prev_o[4])
            total_usage = round(((td - id_) / td) * 100, 1) if td > 0 else 0.0

        _prev_per_core = cores
        _read_per_core_cpu._prev_overall = overall_times
        _prev_per_core_ts = time.time()

    except Exception as e:
        log.warning("per_core_cpu_error", error=str(e))

    return per_core_usage, total_usage


# ── Memory Information ───────────────────────────────────────────────────────

def _read_memory_info() -> Dict[str, Any]:
    """Read detailed memory info from /proc/meminfo."""
    info: Dict[str, Any] = {
        "total_mb": 0, "used_mb": 0, "available_mb": 0,
        "cached_mb": 0, "buffers_mb": 0, "active_mb": 0, "inactive_mb": 0,
        "swap_total_mb": 0, "swap_used_mb": 0, "swap_free_mb": 0,
        "dirty_mb": 0, "writeback_mb": 0,
        "slab_mb": 0, "page_tables_mb": 0,
        "committed_mb": 0, "commit_limit_mb": 0,
        "mapped_mb": 0, "shared_mb": 0,
        "percent": 0.0, "swap_percent": 0.0,
    }

    try:
        meminfo = {}
        with open(os.path.join(HOST_PROC, "meminfo"), "r") as f:
            for line in f:
                parts = line.split()
                key = parts[0].rstrip(":")
                val = int(parts[1]) * 1024  # kB to bytes
                meminfo[key] = val

        to_mb = lambda x: round(x / (1024 * 1024), 1)

        total = meminfo.get("MemTotal", 0)
        available = meminfo.get("MemAvailable", 0)
        used = total - available

        info["total_mb"] = to_mb(total)
        info["available_mb"] = to_mb(available)
        info["used_mb"] = to_mb(used)
        info["cached_mb"] = to_mb(meminfo.get("Cached", 0))
        info["buffers_mb"] = to_mb(meminfo.get("Buffers", 0))
        info["active_mb"] = to_mb(meminfo.get("Active", 0))
        info["inactive_mb"] = to_mb(meminfo.get("Inactive", 0))
        info["swap_total_mb"] = to_mb(meminfo.get("SwapTotal", 0))
        info["swap_free_mb"] = to_mb(meminfo.get("SwapFree", 0))
        info["swap_used_mb"] = round(info["swap_total_mb"] - info["swap_free_mb"], 1)
        info["dirty_mb"] = to_mb(meminfo.get("Dirty", 0))
        info["writeback_mb"] = to_mb(meminfo.get("Writeback", 0))
        info["slab_mb"] = to_mb(meminfo.get("Slab", 0))
        info["page_tables_mb"] = to_mb(meminfo.get("PageTables", 0))
        info["committed_mb"] = to_mb(meminfo.get("Committed_AS", 0))
        info["commit_limit_mb"] = to_mb(meminfo.get("CommitLimit", 0))
        info["mapped_mb"] = to_mb(meminfo.get("Mapped", 0))
        info["shared_mb"] = to_mb(meminfo.get("Shmem", 0))
        info["percent"] = round((used / total * 100) if total > 0 else 0, 1)
        swap_total = meminfo.get("SwapTotal", 0)
        swap_used = swap_total - meminfo.get("SwapFree", 0)
        info["swap_percent"] = round((swap_used / swap_total * 100) if swap_total > 0 else 0, 1)

    except Exception as e:
        log.warning("memory_info_error", error=str(e))

    return info


# ── Disk Information ─────────────────────────────────────────────────────────

def _read_disk_info() -> List[Dict[str, Any]]:
    """Read disk information from the HOST via /host_proc/1/mounts.

    Reads the host init process (PID 1) mount table to get real partitions
    instead of the container-local view.  Deduplicates by device so bind-
    mounts of the same partition only appear once.
    """
    global _prev_disk_stats, _prev_disk_ts

    disks: List[Dict[str, Any]] = []
    now = time.time()

    # ---- Host mount table (PID 1 = host) ----
    host_mounts: Dict[str, Dict[str, str]] = {}  # device -> first mount info
    try:
        mounts_path = os.path.join(HOST_PROC, "1", "mounts")
        with open(mounts_path, "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                device, mount, fstype = parts[0], parts[1], parts[2]
                if not device.startswith("/dev/"):
                    continue
                if fstype in ("squashfs", "tmpfs", "devtmpfs", "devpts"):
                    continue
                if "loop" in device:
                    continue
                # Deduplicate: keep the shortest (primary) mount path
                if device not in host_mounts or len(mount) < len(host_mounts[device]["mount"]):
                    host_mounts[device] = {
                        "device": device,
                        "mount": mount,
                        "filesystem": fstype,
                    }
    except Exception:
        pass

    # ---- Disk I/O stats from /proc/diskstats ----
    disk_stats = {}
    try:
        with open(os.path.join(HOST_PROC, "diskstats"), "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 14:
                    dev_name = parts[2]
                    disk_stats[dev_name] = {
                        "reads_completed": int(parts[3]),
                        "sectors_read": int(parts[5]),
                        "writes_completed": int(parts[7]),
                        "sectors_written": int(parts[9]),
                        "io_time_ms": int(parts[12]),
                    }
    except Exception:
        pass

    # ---- Build disk entries from host mounts ----
    for device, minfo in host_mounts.items():
        dev_name = device.split("/")[-1]
        base_dev = re.sub(r"\d+$", "", dev_name)
        mount_point = minfo["mount"]

        # Get usage via statvfs on a path we can actually access
        # For host root (/), we can read via /host_proc/1/root/ if mounted
        total_gb = used_gb = free_gb = pct = 0.0
        try:
            # Try direct statvfs on the mountpoint (works for bind-mounted paths)
            # For the host root, our bind-mounts like /host_etc come from it
            access_path = mount_point
            if mount_point == "/":
                # Root filesystem — use our bind mount source
                access_path = "/host_etc"
            elif mount_point.startswith("/mnt"):
                access_path = None  # Can't access from container
            elif mount_point.startswith("/boot"):
                access_path = None  # Can't access from container

            if access_path and os.path.exists(access_path):
                st = os.statvfs(access_path)
                total_gb = round((st.f_blocks * st.f_frsize) / (1024 ** 3), 2)
                free_gb = round((st.f_bfree * st.f_frsize) / (1024 ** 3), 2)
                used_gb = round(total_gb - free_gb, 2)
                pct = round((used_gb / total_gb) * 100, 1) if total_gb > 0 else 0
        except Exception:
            pass

        if total_gb == 0:
            continue  # Skip if we couldn't determine size

        disk_entry: Dict[str, Any] = {
            "device": device,
            "name": dev_name,
            "mount_point": mount_point,
            "filesystem": minfo["filesystem"],
            "type": "Unknown",
            "total_gb": total_gb,
            "used_gb": used_gb,
            "free_gb": free_gb,
            "percent": pct,
            "read_speed_mbps": 0.0,
            "write_speed_mbps": 0.0,
            "reads_per_sec": 0.0,
            "writes_per_sec": 0.0,
            "active_time_percent": 0.0,
        }

        # Determine disk type (SSD vs HDD)
        try:
            rotational_path = os.path.join(HOST_SYS, "block", base_dev, "queue", "rotational")
            if os.path.exists(rotational_path):
                with open(rotational_path) as rf:
                    disk_entry["type"] = "HDD" if rf.read().strip() == "1" else "SSD"
            else:
                real_dev = _resolve_block_device(mount_point, base_dev)
                if not real_dev and os.path.exists("/host_etc"):
                    real_dev = _resolve_block_device("/host_etc", base_dev)
                if real_dev:
                    rot_path = os.path.join(HOST_SYS, "block", real_dev, "queue", "rotational")
                    if os.path.exists(rot_path):
                        with open(rot_path) as rf:
                            disk_entry["type"] = "HDD" if rf.read().strip() == "1" else "SSD"
                    else:
                        disk_entry["type"] = "SSD"
                elif base_dev.startswith("nvme"):
                    disk_entry["type"] = "SSD"
                else:
                    disk_entry["type"] = "Virtual"
        except Exception:
            pass

        # Calculate I/O rates from diskstats
        stat_key = dev_name if dev_name in disk_stats else base_dev
        if stat_key in disk_stats and _prev_disk_stats and stat_key in _prev_disk_stats:
            elapsed = now - _prev_disk_ts
            if elapsed > 0:
                cur = disk_stats[stat_key]
                prev = _prev_disk_stats[stat_key]
                read_bytes = (cur["sectors_read"] - prev["sectors_read"]) * 512
                write_bytes = (cur["sectors_written"] - prev["sectors_written"]) * 512
                disk_entry["read_speed_mbps"] = round(read_bytes / elapsed / (1024 * 1024), 2)
                disk_entry["write_speed_mbps"] = round(write_bytes / elapsed / (1024 * 1024), 2)
                disk_entry["reads_per_sec"] = round(
                    (cur["reads_completed"] - prev["reads_completed"]) / elapsed, 1
                )
                disk_entry["writes_per_sec"] = round(
                    (cur["writes_completed"] - prev["writes_completed"]) / elapsed, 1
                )
                io_time_diff = cur["io_time_ms"] - prev["io_time_ms"]
                disk_entry["active_time_percent"] = round(
                    min(io_time_diff / (elapsed * 1000) * 100, 100), 1
                )

        disks.append(disk_entry)

    _prev_disk_stats = disk_stats
    _prev_disk_ts = now

    return disks


# ── Network Information ──────────────────────────────────────────────────────

def _read_network_info() -> List[Dict[str, Any]]:
    """Read detailed network interface information from the HOST.

    Uses /host_proc/1/net/dev (PID 1 = host network namespace) for traffic
    stats, and /host_sys/class/net/ for metadata (MAC, speed, MTU, state).
    IPv4 addresses are resolved via route table + fib_trie LOCAL entries.
    IPv6 addresses come from /host_proc/1/net/if_inet6.
    """
    global _prev_net_counters, _prev_net_ts

    interfaces: List[Dict[str, Any]] = []
    now = time.time()

    # ---- Read traffic stats from host PID 1 network namespace ----
    host_net_dev = os.path.join(HOST_PROC, "1", "net", "dev")
    net_stats: Dict[str, Dict] = {}
    try:
        with open(host_net_dev, "r") as f:
            lines = f.readlines()[2:]
        for line in lines:
            parts = line.split()
            iface = parts[0].rstrip(":")
            if iface == "lo":
                continue
            net_stats[iface] = {
                "rx_bytes": int(parts[1]),
                "rx_packets": int(parts[2]),
                "rx_errors": int(parts[3]),
                "rx_dropped": int(parts[4]),
                "tx_bytes": int(parts[9]),
                "tx_packets": int(parts[10]),
                "tx_errors": int(parts[11]),
                "tx_dropped": int(parts[12]),
            }
    except Exception:
        pass

    # ---- Resolve IP addresses from host namespace ----
    ipv4_addrs = _read_ipv4_addresses_host()
    ipv6_addrs = _read_ipv6_addresses_host()
    dns_servers = _read_dns_servers()

    # ---- Build interface entries ----
    for iface, stats in net_stats.items():
        # Skip Docker virtual interfaces
        if iface.startswith(("veth", "br-", "docker", "lxcbr")):
            continue

        entry: Dict[str, Any] = {
            "interface": iface,
            "type": _guess_interface_type(iface),
            "ipv4": ipv4_addrs.get(iface, "N/A"),
            "ipv6": ipv6_addrs.get(iface, "N/A"),
            "mac": _read_mac(iface),
            "speed_mbps": _read_link_speed(iface),
            "mtu": _read_mtu(iface),
            "status": _read_operstate(iface),
            "rx_bytes": stats["rx_bytes"],
            "tx_bytes": stats["tx_bytes"],
            "rx_packets": stats["rx_packets"],
            "tx_packets": stats["tx_packets"],
            "rx_errors": stats["rx_errors"],
            "tx_errors": stats["tx_errors"],
            "send_rate_bps": 0.0,
            "recv_rate_bps": 0.0,
            "dns_servers": dns_servers,
        }

        # Calculate rates from previous snapshot
        if _prev_net_counters and iface in _prev_net_counters:
            elapsed = now - _prev_net_ts
            if elapsed > 0:
                prev = _prev_net_counters[iface]
                entry["send_rate_bps"] = round(
                    (stats["tx_bytes"] - prev["tx_bytes"]) / elapsed, 1
                )
                entry["recv_rate_bps"] = round(
                    (stats["rx_bytes"] - prev["rx_bytes"]) / elapsed, 1
                )

        interfaces.append(entry)

    _prev_net_counters = net_stats
    _prev_net_ts = now

    return interfaces


def _read_ipv4_addresses_host() -> Dict[str, str]:
    """Map IPv4 addresses to interfaces using host PID 1 route + fib_trie.

    1. Parse /host_proc/1/net/route to get (iface, network, mask) tuples.
    2. Parse LOCAL /32 entries from /host_proc/1/net/fib_trie.
    3. Match each LOCAL IP to its interface via subnet membership.
    """
    import struct, socket as _socket

    # Step 1: Build interface -> [(network_int, mask_int)] from route table
    iface_subnets: Dict[str, List[tuple]] = {}
    try:
        route_path = os.path.join(HOST_PROC, "1", "net", "route")
        with open(route_path, "r") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) < 8:
                    continue
                iface = parts[0]
                dest_hex = int(parts[1], 16)
                mask_hex = int(parts[7], 16)
                if mask_hex == 0:
                    continue  # Skip default routes
                # /proc/net/route stores hex in host byte order (little-endian)
                dest_bytes = struct.pack("<I", dest_hex)
                mask_bytes = struct.pack("<I", mask_hex)
                dest_int = struct.unpack("!I", dest_bytes)[0]
                mask_int = struct.unpack("!I", mask_bytes)[0]
                iface_subnets.setdefault(iface, []).append((dest_int, mask_int))
    except Exception:
        pass

    # Step 2: Extract LOCAL /32 IPs from fib_trie
    local_ips: List[str] = []
    try:
        trie_path = os.path.join(HOST_PROC, "1", "net", "fib_trie")
        with open(trie_path, "r") as f:
            content = f.read()
        for m in re.finditer(
            r"\|--\s+([\d.]+)\s*\n\s+/32 host LOCAL", content
        ):
            ip = m.group(1)
            if not ip.startswith("127."):
                local_ips.append(ip)
    except Exception:
        pass

    # Step 3: Match each LOCAL IP to its interface
    addrs: Dict[str, str] = {}
    seen_ips = set()
    for ip in local_ips:
        if ip in seen_ips:
            continue
        seen_ips.add(ip)
        ip_int = struct.unpack("!I", _socket.inet_aton(ip))[0]
        for iface, subnets in iface_subnets.items():
            for net_int, mask_int in subnets:
                if (ip_int & mask_int) == net_int:
                    if iface not in addrs:
                        addrs[iface] = ip
                    break

    return addrs


def _read_ipv6_addresses_host() -> Dict[str, str]:
    """Read IPv6 addresses from host PID 1 /proc/net/if_inet6."""
    addrs: Dict[str, str] = {}
    try:
        ipv6_path = os.path.join(HOST_PROC, "1", "net", "if_inet6")
        with open(ipv6_path, "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 6:
                    hex_addr = parts[0]
                    iface = parts[5]
                    groups = [hex_addr[i : i + 4] for i in range(0, 32, 4)]
                    ipv6 = ":".join(groups)
                    import ipaddress

                    try:
                        ipv6 = str(ipaddress.IPv6Address(ipv6))
                    except Exception:
                        pass
                    if iface not in addrs:
                        addrs[iface] = ipv6
    except Exception:
        pass
    return addrs


def _read_dns_servers() -> List[str]:
    """Read DNS servers from /etc/resolv.conf."""
    servers = []
    try:
        with open("/etc/resolv.conf", "r") as f:
            for line in f:
                if line.strip().startswith("nameserver"):
                    servers.append(line.split()[1])
    except Exception:
        pass
    return servers


def _resolve_block_device(mountpoint: str, fallback_dev: str) -> Optional[str]:
    """Resolve the underlying block device name via major:minor from stat().

    On Linux, stat() a mountpoint gives the device major:minor.  We then
    look up /sys/dev/block/MAJOR:MINOR which symlinks to the real device.
    """
    try:
        st = os.stat(mountpoint)
        major = os.major(st.st_dev)
        minor = os.minor(st.st_dev)
        dev_link = os.path.join(HOST_SYS, "dev", "block", f"{major}:{minor}")
        if os.path.exists(dev_link):
            real_path = os.readlink(dev_link)
            # e.g. ../../devices/pci.../block/sda/sda1 → extract "sda"
            parts = real_path.split("/")
            for p in reversed(parts):
                if p and not p.endswith(str(minor)):
                    return re.sub(r'\d+$', '', p)
            # Fallback: last component without digits
            return re.sub(r'\d+$', '', parts[-1]) if parts else None
    except Exception:
        pass
    return None


def _guess_interface_type(iface: str) -> str:
    """Guess interface type from name."""
    if iface.startswith("eth") or iface.startswith("en"):
        return "Ethernet"
    elif iface.startswith("wl"):
        return "Wi-Fi"
    elif iface.startswith("ww"):
        return "Cellular"
    elif iface.startswith("tun") or iface.startswith("tap"):
        return "VPN"
    elif iface.startswith("bond"):
        return "Bond"
    return "Other"


def _read_mac(iface: str) -> str:
    """Read MAC address from /sys/class/net."""
    try:
        path = os.path.join(HOST_SYS, "class", "net", iface, "address")
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
    except Exception:
        pass
    return "N/A"


def _read_link_speed(iface: str) -> int:
    """Read link speed from /sys/class/net."""
    try:
        path = os.path.join(HOST_SYS, "class", "net", iface, "speed")
        if os.path.exists(path):
            with open(path) as f:
                speed = int(f.read().strip())
                return speed if speed > 0 else 0
    except Exception:
        pass
    return 0


def _read_mtu(iface: str) -> int:
    """Read MTU from /sys/class/net."""
    try:
        path = os.path.join(HOST_SYS, "class", "net", iface, "mtu")
        if os.path.exists(path):
            with open(path) as f:
                return int(f.read().strip())
    except Exception:
        pass
    return 0


def _read_operstate(iface: str) -> str:
    """Read operational state from /sys/class/net."""
    try:
        path = os.path.join(HOST_SYS, "class", "net", iface, "operstate")
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
    except Exception:
        pass
    return "unknown"


# ── System Information ───────────────────────────────────────────────────────

def _read_system_info() -> Dict[str, Any]:
    """Read system-level information."""
    info: Dict[str, Any] = {
        "hostname": "unknown",
        "os": "unknown",
        "kernel": "unknown",
        "architecture": "unknown",
        "uptime_seconds": 0,
        "uptime_formatted": "0m",
        "processes": 0,
        "threads": 0,
    }

    # Hostname (prefer host's /etc/hostname over procfs which is namespace-aware)
    try:
        if os.path.exists("/host_etc/hostname"):
            with open("/host_etc/hostname") as f:
                info["hostname"] = f.read().strip()
        else:
            with open(os.path.join(HOST_PROC, "sys", "kernel", "hostname"), "r") as f:
                info["hostname"] = f.read().strip()
    except Exception:
        import socket
        info["hostname"] = socket.gethostname()

    # OS info
    try:
        with open(os.path.join(HOST_PROC, "version"), "r") as f:
            info["kernel"] = f.read().strip().split()[2] if f else "unknown"
    except Exception:
        pass

    try:
        os_release = {}
        # Try host os-release first (bind-mounted), then container's
        for path in ["/host_etc/os-release", "/host_etc/lsb-release", "/etc/os-release"]:
            if os.path.exists(path):
                with open(path) as f:
                    for line in f:
                        if "=" in line:
                            k, v = line.strip().split("=", 1)
                            os_release[k] = v.strip('"')
                break
        info["os"] = os_release.get("PRETTY_NAME", "Linux")
    except Exception:
        info["os"] = "Linux"

    # Architecture
    try:
        with open(os.path.join(HOST_PROC, "version"), "r") as f:
            ver = f.read()
        if "x86_64" in ver:
            info["architecture"] = "x86_64"
        elif "aarch64" in ver:
            info["architecture"] = "aarch64"
        else:
            info["architecture"] = "x86"
    except Exception:
        pass

    # Uptime
    try:
        with open(os.path.join(HOST_PROC, "uptime"), "r") as f:
            uptime_secs = float(f.read().split()[0])
            info["uptime_seconds"] = int(uptime_secs)
            days = int(uptime_secs // 86400)
            hours = int((uptime_secs % 86400) // 3600)
            minutes = int((uptime_secs % 3600) // 60)
            parts = []
            if days > 0:
                parts.append(f"{days}d")
            if hours > 0:
                parts.append(f"{hours}h")
            parts.append(f"{minutes}m")
            info["uptime_formatted"] = " ".join(parts)
    except Exception:
        pass

    # Process and thread count
    try:
        proc_count = 0
        thread_count = 0
        proc_dir = os.path.join(HOST_PROC)
        for entry in os.listdir(proc_dir):
            if entry.isdigit():
                proc_count += 1
                try:
                    status_path = os.path.join(proc_dir, entry, "status")
                    with open(status_path) as f:
                        for line in f:
                            if line.startswith("Threads:"):
                                thread_count += int(line.split()[1])
                                break
                except Exception:
                    thread_count += 1
        info["processes"] = proc_count
        info["threads"] = thread_count
    except Exception:
        pass

    return info


# ── GPU Information ──────────────────────────────────────────────────────────

def _read_gpu_info() -> Optional[Dict[str, Any]]:
    """Try reading GPU info via nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            gpus = []
            for line in result.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 7:
                    gpus.append({
                        "name": parts[0],
                        "memory_total_mb": float(parts[1]),
                        "memory_used_mb": float(parts[2]),
                        "memory_free_mb": float(parts[3]),
                        "utilization_percent": float(parts[4]),
                        "temperature_c": float(parts[5]),
                        "driver_version": parts[6],
                    })
            return gpus if gpus else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


# ── Docker Container Stats (via Docker Engine API over socket) ───────────────

async def _docker_api_get(path: str) -> Any:
    """Call Docker Engine API via /var/run/docker.sock using async httpx."""
    import httpx
    transport = httpx.AsyncHTTPTransport(uds="/var/run/docker.sock")
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        resp = await client.get(path, timeout=5)
        resp.raise_for_status()
        return resp.json()


async def _get_single_container_stats(cid: str, name: str, state: str, status: str) -> Dict[str, Any]:
    """Get stats for a single container using async httpx."""
    default = {
        "name": name, "state": state, "status": status,
        "cpu_percent": 0, "memory_used_mb": 0,
        "memory_limit_mb": 0, "memory_percent": 0,
        "network_rx_bytes": 0, "network_tx_bytes": 0,
        "block_read_bytes": 0, "block_write_bytes": 0,
        "pids": 0,
    }
    try:
        import httpx
        transport = httpx.AsyncHTTPTransport(uds="/var/run/docker.sock")
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            resp = await client.get(
                f"/containers/{cid}/stats?stream=false", timeout=8
            )
            stats = resp.json()
    except Exception:
        return default

    try:
        # CPU %
        cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - \
                    stats["precpu_stats"]["cpu_usage"]["total_usage"]
        sys_delta = stats["cpu_stats"]["system_cpu_usage"] - \
                    stats["precpu_stats"].get("system_cpu_usage", 0)
        n_cpus = stats["cpu_stats"].get("online_cpus", 1)
        cpu_pct = round((cpu_delta / sys_delta) * n_cpus * 100, 2) if sys_delta > 0 else 0.0

        # Memory
        mem_usage = stats["memory_stats"].get("usage", 0)
        mem_cache = stats["memory_stats"].get("stats", {}).get("cache", 0)
        mem_used = mem_usage - mem_cache
        mem_limit = stats["memory_stats"].get("limit", 1)
        mem_pct = round(mem_used / mem_limit * 100, 2) if mem_limit else 0

        # Network
        net_rx = net_tx = 0
        for ndata in stats.get("networks", {}).values():
            net_rx += ndata.get("rx_bytes", 0)
            net_tx += ndata.get("tx_bytes", 0)

        # Block I/O
        blk_read = blk_write = 0
        for entry in stats.get("blkio_stats", {}).get("io_service_bytes_recursive", []) or []:
            if entry["op"] == "read":
                blk_read += entry["value"]
            elif entry["op"] == "write":
                blk_write += entry["value"]

        pids = stats.get("pids_stats", {}).get("current", 0)

        return {
            "name": name, "state": state, "status": status,
            "cpu_percent": cpu_pct,
            "memory_used_mb": round(mem_used / (1024 * 1024), 1),
            "memory_limit_mb": round(mem_limit / (1024 * 1024), 1),
            "memory_percent": mem_pct,
            "network_rx_bytes": net_rx, "network_tx_bytes": net_tx,
            "block_read_bytes": blk_read, "block_write_bytes": blk_write,
            "pids": pids,
        }
    except Exception:
        return default


async def _read_container_stats() -> List[Dict[str, Any]]:
    """Read Docker container stats via Docker Engine API — all in parallel."""
    import asyncio

    try:
        running = await _docker_api_get("/containers/json")
    except Exception as e:
        log.warning("container_list_error", error=str(e))
        return []

    # Fire all stats requests concurrently
    tasks = []
    for c in running:
        cid = c["Id"][:12]
        name = (c.get("Names") or ["/unknown"])[0].lstrip("/")
        state = c.get("State", "running")
        status_text = c.get("Status", "Up")
        tasks.append(_get_single_container_stats(cid, name, state, status_text))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    containers = []
    for r in results:
        if isinstance(r, Exception):
            continue
        containers.append(r)

    return containers


def _parse_size(s: str) -> int:
    """Parse human-readable size string like '128MiB', '1.5kB', '2.3GB'."""
    s = s.strip()
    multipliers = {
        "B": 1, "kB": 1000, "KB": 1024, "KiB": 1024,
        "MB": 1000000, "MiB": 1048576,
        "GB": 1000000000, "GiB": 1073741824,
        "TB": 1000000000000, "TiB": 1099511627776,
    }
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if s.endswith(suffix):
            try:
                return int(float(s[:-len(suffix)].strip()) * mult)
            except ValueError:
                return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


# ── API Endpoints ────────────────────────────────────────────────────────────

def _gather_system_info_sync() -> Dict[str, Any]:
    """Synchronous system info collection — runs in thread pool to avoid
    blocking the async event loop."""
    global _static_cache, _static_cache_ts

    now = time.time()
    need_static = (now - _static_cache_ts) >= _STATIC_CACHE_TTL or not _static_cache

    cpu = _read_cpu_info()
    memory = _read_memory_info()
    disks = _read_disk_info()
    network = _read_network_info()

    if need_static:
        _static_cache = {
            "system": _read_system_info(),
            "gpu": _read_gpu_info(),
            "cpu_static": {
                "model": cpu["model"],
                "vendor": cpu["vendor"],
                "physical_cores": cpu["physical_cores"],
                "logical_cores": cpu["logical_cores"],
                "cache": cpu["cache"],
                "architecture": cpu["architecture"],
            },
        }
        _static_cache_ts = now

    for k, v in _static_cache.get("cpu_static", {}).items():
        cpu[k] = v

    return {
        "cpu": cpu,
        "memory": memory,
        "disks": disks,
        "network": network,
        "system": _static_cache.get("system", _read_system_info()),
        "gpu": _static_cache.get("gpu", {}),
    }


@router.get("/system-info")
async def system_info():
    """Comprehensive system information — CPU, Memory, Disk, Network, GPU.

    Runs file-based reads in a thread pool so the async event loop stays free.
    Static hardware data is cached for 5 minutes.
    """
    import asyncio
    return await asyncio.to_thread(_gather_system_info_sync)


# Container stats cache — 3 second TTL (expensive Docker API calls)
_container_cache: Dict[str, Any] = {}
_container_cache_ts: float = 0.0
_CONTAINER_CACHE_TTL = 3.0


@router.get("/containers")
async def container_stats():
    """Docker container resource usage — CPU, memory, network, I/O per container.

    Stats are fetched concurrently via async httpx.  Results cached 3 seconds.
    """
    global _container_cache, _container_cache_ts

    now = time.time()
    if (now - _container_cache_ts) < _CONTAINER_CACHE_TTL and _container_cache:
        return _container_cache

    containers = await _read_container_stats()
    running = sum(1 for c in containers if c["cpu_percent"] >= 0)
    _container_cache = {
        "containers": containers,
        "total": len(containers),
        "running": running,
    }
    _container_cache_ts = now
    return _container_cache


@router.get("/dashboard-data")
async def dashboard_data():
    """Single combined endpoint for the dashboard — all data in ONE request.

    Runs system-info (threaded file reads) and container stats (async Docker
    API) concurrently so total latency is max(sys_info, container_stats)
    instead of the sum.
    """
    import asyncio

    sys_task = asyncio.to_thread(_gather_system_info_sync)
    ctr_task = container_stats()
    sys_info, ctr_info = await asyncio.gather(sys_task, ctr_task)

    return {
        **sys_info,
        "containers": ctr_info.get("containers", []),
        "containers_total": ctr_info.get("total", 0),
        "containers_running": ctr_info.get("running", 0),
    }


def _gather_dynamic_only_sync() -> Dict[str, Any]:
    """Read ONLY fast-changing metrics — CPU%, memory%, network rates, disk I/O.

    Skips all static data (model, IPs, hostnames, cache sizes) and Docker API.
    Designed to complete in <100ms for high-frequency polling.
    """
    per_core, total_usage = _read_per_core_cpu()

    # Quick memory from /proc/meminfo (only the key fields)
    mem: Dict[str, Any] = {}
    try:
        with open(os.path.join(HOST_PROC, "meminfo"), "r") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) < 2:
                    continue
                key = parts[0].strip()
                val_str = parts[1].strip().split()[0]
                try:
                    val_kb = int(val_str)
                except ValueError:
                    continue
                if key == "MemTotal":
                    mem["total_mb"] = round(val_kb / 1024, 1)
                elif key == "MemAvailable":
                    mem["available_mb"] = round(val_kb / 1024, 1)
                elif key == "Cached":
                    mem["cached_mb"] = round(val_kb / 1024, 1)
                elif key == "Buffers":
                    mem["buffers_mb"] = round(val_kb / 1024, 1)
        total = mem.get("total_mb", 1)
        avail = mem.get("available_mb", 0)
        mem["used_mb"] = round(total - avail, 1)
        mem["percent"] = round((total - avail) / total * 100, 1) if total > 0 else 0
    except Exception:
        pass

    # Network rates from /proc/1/net/dev
    net_rates: List[Dict[str, Any]] = []
    try:
        with open(os.path.join(HOST_PROC, "1/net/dev"), "r") as f:
            for line in f:
                if ":" not in line:
                    continue
                parts = line.strip().split(":")
                iface = parts[0].strip()
                if iface == "lo":
                    continue
                # Skip Docker virtual interfaces
                if iface.startswith(("veth", "br-", "docker", "lxcbr")):
                    continue
                fields = parts[1].split()
                if len(fields) >= 10:
                    net_rates.append({
                        "interface": iface,
                        "rx_bytes": int(fields[0]),
                        "tx_bytes": int(fields[8]),
                    })
    except Exception:
        pass

    # Disk I/O rates (only — no capacity reads)
    disk_io: List[Dict[str, Any]] = []
    try:
        now = time.time()
        with open(os.path.join(HOST_PROC, "diskstats"), "r") as f:
            for line in f:
                fields = line.split()
                if len(fields) < 14:
                    continue
                dev_name = fields[2]
                # Only root-level block devices (not partitions, not dm-/loop)
                if dev_name.startswith("loop") or dev_name.startswith("dm-"):
                    continue
                if any(c.isdigit() for c in dev_name) and not dev_name.startswith("nvme"):
                    continue
                sectors_read = int(fields[5])
                sectors_written = int(fields[9])
                io_ms = int(fields[12]) if len(fields) > 12 else 0

                if _prev_disk_stats and dev_name in _prev_disk_stats:
                    prev = _prev_disk_stats[dev_name]
                    dt = now - _prev_disk_ts if _prev_disk_ts else 1.0
                    if dt > 0:
                        rd = (sectors_read - prev.get("sectors_read", sectors_read)) * 512 / 1048576 / dt
                        wr = (sectors_written - prev.get("sectors_written", sectors_written)) * 512 / 1048576 / dt
                        disk_io.append({
                            "device": dev_name,
                            "read_mbps": round(max(0, rd), 2),
                            "write_mbps": round(max(0, wr), 2),
                        })
    except Exception:
        pass

    # Load average
    load: Dict[str, float] = {}
    try:
        with open(os.path.join(HOST_PROC, "loadavg"), "r") as f:
            parts = f.read().split()
            load["load_1m"] = float(parts[0])
            load["load_5m"] = float(parts[1])
            load["load_15m"] = float(parts[2])
    except Exception:
        pass

    return {
        "cpu_total": total_usage,
        "cpu_per_core": per_core,
        "memory": mem,
        "network": net_rates,
        "disk_io": disk_io,
        **load,
    }


@router.get("/dashboard-fast")
async def dashboard_fast():
    """Lightweight dynamic-only metrics for high-frequency polling (3s).

    Returns CPU %, memory %, network byte counters, disk I/O rates,
    and load average.  No Docker API calls, no static data.
    Designed to respond in <100ms.
    """
    import asyncio
    return await asyncio.to_thread(_gather_dynamic_only_sync)
