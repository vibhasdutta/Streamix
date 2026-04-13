"""Shared utility helpers for Streamix."""

from .os_detector import (
    IS_LINUX,
    IS_MACOS,
    IS_WINDOWS,
    OS,
    RAW_OS_NAME,
    RAW_OS_RELEASE,
    RAW_OS_VERSION,
    current_os,
    get_os,
)

__all__ = [
    "OS",
    "current_os",
    "get_os",
    "IS_WINDOWS",
    "IS_MACOS",
    "IS_LINUX",
    "RAW_OS_NAME",
    "RAW_OS_RELEASE",
    "RAW_OS_VERSION",
]
