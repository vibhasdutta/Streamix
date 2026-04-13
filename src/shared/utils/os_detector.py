"""Detect and expose runtime operating system details.

This module is intentionally singleton-like: detection runs once at import
and module-level constants are reused everywhere in the project.
"""

from __future__ import annotations

from enum import Enum
import platform


class OS(Enum):
    """Canonical operating system values supported by the project."""

    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"


def _detect_os(system_name: str) -> OS:
    """Convert a raw platform.system value into an OS enum.

    Args:
        system_name: Raw value from platform.system().

    Returns:
        A canonical OS enum value.

    Raises:
        RuntimeError: If the current platform is not supported.
    """
    if system_name == "Windows":
        return OS.WINDOWS
    if system_name == "Darwin":
        return OS.MACOS
    if system_name == "Linux":
        return OS.LINUX

    raise RuntimeError(
        "Unsupported operating system detected: "
        f"{system_name!r}. Supported operating systems are Windows, macOS, and Linux."
    )


RAW_OS_NAME: str = platform.system()
"""Raw operating system name from platform.system()."""

RAW_OS_RELEASE: str = platform.release()
"""Raw operating system release from platform.release()."""

RAW_OS_VERSION: str = platform.version()
"""Raw operating system version from platform.version()."""

current_os: OS = _detect_os(RAW_OS_NAME)
"""Canonical current operating system value."""

IS_WINDOWS: bool = current_os is OS.WINDOWS
"""Whether the current operating system is Windows."""

IS_MACOS: bool = current_os is OS.MACOS
"""Whether the current operating system is macOS."""

IS_LINUX: bool = current_os is OS.LINUX
"""Whether the current operating system is Linux."""


def get_os() -> OS:
    """Return the canonical detected operating system value."""
    return current_os
