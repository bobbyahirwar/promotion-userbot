"""
Tracks the moment the bot finished startup so /health can report uptime.
Call set_start_time() once after all services are ready.
"""
from datetime import datetime, timezone

_start_time: datetime | None = None


def set_start_time() -> None:
    global _start_time
    _start_time = datetime.now(timezone.utc)


def get_uptime_str() -> str:
    """Return a human-readable uptime string, or 'Unknown' if not set."""
    if _start_time is None:
        return "Unknown"
    delta = datetime.now(timezone.utc) - _start_time
    total_seconds = int(delta.total_seconds())
    days    = total_seconds // 86400
    hours   = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)
