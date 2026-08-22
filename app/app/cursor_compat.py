"""Совместимость cursor-sdk с Python < 3.12 (os.get_blocking / os.set_blocking)."""

from __future__ import annotations

import os
import sys


def ensure_os_blocking_compat() -> None:
    """cursor-sdk/_bridge.py использует os.get_blocking (Python 3.12+)."""
    if hasattr(os, "get_blocking"):
        return

    if sys.platform == "win32":
        def get_blocking(fd: int) -> bool:  # noqa: ARG001
            return True

        def set_blocking(fd: int, blocking: bool) -> None:  # noqa: ARG001
            return None

    else:
        import fcntl

        def get_blocking(fd: int) -> bool:
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            return not (flags & os.O_NONBLOCK)

        def set_blocking(fd: int, blocking: bool) -> None:
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            if blocking:
                flags &= ~os.O_NONBLOCK
            else:
                flags |= os.O_NONBLOCK
            fcntl.fcntl(fd, fcntl.F_SETFL, flags)

    os.get_blocking = get_blocking  # type: ignore[attr-defined]
    os.set_blocking = set_blocking  # type: ignore[attr-defined]
