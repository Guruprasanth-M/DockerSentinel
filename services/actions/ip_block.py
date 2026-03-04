"""iptables-based IP blocking — C1: Host network namespace via nsenter."""
import asyncio
import os
import subprocess
import structlog

logger = structlog.get_logger("sentinel.actions.ip_block")

# C1: Use nsenter to run firewall commands in the host network namespace.
# Requires pid:host and SYS_PTRACE + NET_ADMIN capabilities in docker-compose.
NSENTER_PREFIX = ["nsenter", "--target", "1", "--net"]


async def block_ip(ip: str, reason: str = "") -> dict:
    """
    Block an IP address using iptables.
    
    Args:
        ip: IP address to block
        reason: Reason for blocking (for audit log)
        
    Returns:
        Result dict with status and details
    """
    try:
        # Check if rule already exists
        check = await _run_command(
            ["iptables", "-C", "INPUT", "-s", ip, "-j", "DROP"]
        )
        if check["returncode"] == 0:
            logger.info("ip_already_blocked", ip=ip)
            return {
                "status": "already_blocked",
                "ip": ip,
                "message": f"IP {ip} is already blocked",
            }

        # Add iptables DROP rule
        result = await _run_command(
            ["iptables", "-A", "INPUT", "-s", ip, "-j", "DROP"]
        )

        if result["returncode"] == 0:
            logger.info("ip_blocked", ip=ip, reason=reason)
            return {
                "status": "blocked",
                "ip": ip,
                "method": "iptables",
                "message": f"IP {ip} blocked successfully",
            }
        else:
            # Fallback: try with nftables
            return await _block_ip_nftables(ip, reason)

    except FileNotFoundError:
        logger.warning("iptables_not_found", fallback="nftables")
        return await _block_ip_nftables(ip, reason)
    except Exception as e:
        logger.error("ip_block_failed", ip=ip, error=str(e))
        return {
            "status": "failed",
            "ip": ip,
            "error": str(e),
            "message": f"Failed to block IP {ip}: {e}",
        }


async def unblock_ip(ip: str) -> dict:
    """
    Remove an IP block.
    
    Args:
        ip: IP address to unblock
        
    Returns:
        Result dict with status and details
    """
    try:
        result = await _run_command(
            ["iptables", "-D", "INPUT", "-s", ip, "-j", "DROP"]
        )

        if result["returncode"] == 0:
            logger.info("ip_unblocked", ip=ip)
            return {
                "status": "unblocked",
                "ip": ip,
                "method": "iptables",
                "message": f"IP {ip} unblocked successfully",
            }
        else:
            # Try nftables fallback
            return await _unblock_ip_nftables(ip)

    except FileNotFoundError:
        return await _unblock_ip_nftables(ip)
    except Exception as e:
        logger.error("ip_unblock_failed", ip=ip, error=str(e))
        return {
            "status": "failed",
            "ip": ip,
            "error": str(e),
        }


async def list_blocked_ips() -> list:
    """List all currently blocked IPs."""
    try:
        result = await _run_command(
            ["iptables", "-L", "INPUT", "-n", "--line-numbers"]
        )
        if result["returncode"] != 0:
            return []

        blocked = []
        for line in result["stdout"].split("\n"):
            if "DROP" in line and "all" in line:
                parts = line.split()
                for part in parts:
                    if _is_ip(part):
                        blocked.append(part)
                        break
        return blocked

    except Exception as e:
        logger.error("list_blocked_failed", error=str(e))
        return []


async def _block_ip_nftables(ip: str, reason: str = "") -> dict:
    """Fallback: block IP using nftables."""
    try:
        result = await _run_command([
            "nft", "add", "rule", "inet", "filter", "input",
            "ip", "saddr", ip, "drop",
        ])
        if result["returncode"] == 0:
            logger.info("ip_blocked_nftables", ip=ip, reason=reason)
            return {
                "status": "blocked",
                "ip": ip,
                "method": "nftables",
                "message": f"IP {ip} blocked via nftables",
            }
    except Exception:
        pass

    # Final fallback: log-only
    logger.warning("ip_block_no_firewall", ip=ip,
                    message="No firewall tool available, logging block request only")
    return {
        "status": "logged_only",
        "ip": ip,
        "method": "none",
        "message": f"IP {ip} block logged (no firewall tool available in container)",
    }


async def _unblock_ip_nftables(ip: str) -> dict:
    """Fallback: unblock IP using nftables."""
    try:
        # nftables doesn't have a simple "delete by IP" — find and remove
        result = await _run_command(["nft", "list", "ruleset", "-a"])
        if result["returncode"] == 0 and ip in result["stdout"]:
            for line in result["stdout"].split("\n"):
                if ip in line and "handle" in line:
                    handle = line.split("handle")[-1].strip()
                    await _run_command([
                        "nft", "delete", "rule", "inet", "filter", "input",
                        "handle", handle,
                    ])
            return {"status": "unblocked", "ip": ip, "method": "nftables"}
    except Exception:
        pass

    return {
        "status": "logged_only",
        "ip": ip,
        "method": "none",
        "message": f"IP {ip} unblock logged (no firewall tool available)",
    }


async def _run_command(cmd: list, use_nsenter: bool = True) -> dict:
    """Run a shell command asynchronously.
    
    C1: Prepends nsenter to operate in host network namespace when use_nsenter is True.
    """
    if use_nsenter:
        cmd = NSENTER_PREFIX + cmd
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        return {
            "returncode": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }
    except asyncio.TimeoutError:
        return {"returncode": -1, "stdout": "", "stderr": "Command timed out"}
    except FileNotFoundError:
        raise


def _is_ip(s: str) -> bool:
    """Check if a string looks like an IPv4 or IPv6 address."""
    import ipaddress
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False
