"""Safety whitelist for protected resources."""
import ipaddress
import structlog
from typing import Set

logger = structlog.get_logger("hostspectra.actions.whitelist")

# Ports that can never be blocked
PROTECTED_PORTS: Set[int] = {
    22,     # SSH
    51820,  # WireGuard VPN
}

# IPs/ranges that can never be blocked
PROTECTED_IP_RANGES = [
    ipaddress.ip_network("127.0.0.0/8"),      # Localhost IPv4
    ipaddress.ip_network("::1/128"),           # Localhost IPv6
    ipaddress.ip_network("10.0.0.0/8"),        # Private network (common Docker)
    ipaddress.ip_network("172.16.0.0/12"),     # Private network (Docker default)
    ipaddress.ip_network("192.168.0.0/16"),    # Private network
]

# Actions that are never allowed
FORBIDDEN_ACTIONS = {
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "disable_interface",
    "rm_rf",
    "format",
}

# Processes that can never be killed
PROTECTED_PROCESSES: Set[str] = {
    "systemd",
    "init",
    "sshd",
    "dockerd",
    "containerd",
    "docker-proxy",
    "kubelet",
    "kernel",
    "kthreadd",
}


def is_ip_protected(ip_str: str, config_protected: list = None) -> bool:
    """
    Check if an IP address is protected from blocking.
    
    Checks both hard-coded ranges and config-specified protected IPs.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        logger.warning("invalid_ip_for_protection_check", ip=ip_str)
        return True  # Err on the side of caution

    # Check hard-coded ranges
    for network in PROTECTED_IP_RANGES:
        if ip in network:
            logger.warning("ip_protected_hardcoded", ip=ip_str, network=str(network))
            return True

    # Check config-specified protected IPs
    if config_protected:
        for protected_ip in config_protected:
            try:
                if ip == ipaddress.ip_address(protected_ip):
                    logger.warning("ip_protected_config", ip=ip_str)
                    return True
            except ValueError:
                continue

    return False


def is_port_protected(port: int) -> bool:
    """Check if a port is protected from blocking."""
    if port in PROTECTED_PORTS:
        logger.warning("port_protected", port=port)
        return True
    return False


def is_process_protected(process_name: str) -> bool:
    """Check if a process is protected from being killed."""
    name_lower = process_name.lower()
    if name_lower in PROTECTED_PROCESSES:
        logger.warning("process_protected", process=process_name)
        return True
    return False


def is_action_forbidden(action: str) -> bool:
    """Check if an action is in the forbidden list."""
    if action.lower() in FORBIDDEN_ACTIONS:
        logger.warning("action_forbidden", action=action)
        return True
    return False


def validate_action(action: str, target: str, config_protected_ips: list = None) -> tuple:
    """
    Validate whether an action is allowed.
    
    Returns:
        (allowed: bool, reason: str)
    """
    # Check forbidden actions
    if is_action_forbidden(action):
        return False, f"Action '{action}' is permanently forbidden"

    # Check IP-based actions
    if action == "block_ip":
        if is_ip_protected(target, config_protected_ips):
            return False, f"IP {target} is protected and cannot be blocked"

    # Check process-based actions
    if action == "kill_process":
        if is_process_protected(target):
            return False, f"Process '{target}' is protected and cannot be killed"

    return True, "Action allowed"
