"""Process termination via signals."""
import os
import signal
import structlog

logger = structlog.get_logger("hostspectra.actions.process_manager")


async def kill_process(pid: int, process_name: str = "") -> dict:
    """
    Terminate a process by PID.
    
    Uses SIGTERM (graceful) first. Does not use SIGKILL.
    
    Args:
        pid: Process ID to terminate
        process_name: Optional name for logging
        
    Returns:
        Result dict with status and details
    """
    try:
        # Validate PID
        if pid <= 1:
            return {
                "status": "rejected",
                "pid": pid,
                "message": f"PID {pid} is protected (init/systemd)",
            }

        # Check if process exists
        try:
            os.kill(pid, 0)  # Signal 0 = check existence
        except ProcessLookupError:
            return {
                "status": "not_found",
                "pid": pid,
                "message": f"Process {pid} not found",
            }
        except PermissionError:
            return {
                "status": "permission_denied",
                "pid": pid,
                "message": f"No permission to signal PID {pid}",
            }

        # Send SIGTERM (graceful termination)
        os.kill(pid, signal.SIGTERM)

        logger.info(
            "process_killed",
            pid=pid,
            process_name=process_name,
            signal="SIGTERM",
        )

        return {
            "status": "terminated",
            "pid": pid,
            "process_name": process_name,
            "signal": "SIGTERM",
            "message": f"Process {pid} ({process_name}) terminated",
        }

    except ProcessLookupError:
        return {
            "status": "not_found",
            "pid": pid,
            "message": f"Process {pid} already terminated",
        }
    except PermissionError:
        logger.error("process_kill_permission_denied", pid=pid)
        return {
            "status": "permission_denied",
            "pid": pid,
            "message": f"Permission denied to kill PID {pid}",
        }
    except Exception as e:
        logger.error("process_kill_failed", pid=pid, error=str(e))
        return {
            "status": "failed",
            "pid": pid,
            "error": str(e),
        }


def get_process_info(pid: int) -> dict:
    """Get basic info about a process by PID."""
    try:
        with open(f"/proc/{pid}/comm", "r") as f:
            name = f.read().strip()
        with open(f"/proc/{pid}/cmdline", "r") as f:
            cmdline = f.read().replace("\x00", " ").strip()
        return {"pid": pid, "name": name, "cmdline": cmdline}
    except (FileNotFoundError, PermissionError):
        return {"pid": pid, "name": "unknown", "cmdline": ""}
