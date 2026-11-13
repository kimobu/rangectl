from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any, Sequence


def run_bounded(
    command: str | Sequence[str],
    *,
    timeout: int,
    shell: bool = False,
    tail_bytes: int = 4000,
) -> dict[str, Any]:
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        completed = subprocess.run(
            command,
            shell=shell,
            stdout=stdout,
            stderr=stderr,
            timeout=timeout,
        )
        return {
            "returncode": completed.returncode,
            "stdout": _read_tail(stdout, tail_bytes),
            "stderr": _read_tail(stderr, tail_bytes),
        }


def _read_tail(handle: Any, limit: int) -> str:
    size = handle.tell()
    handle.seek(max(0, size - limit), os.SEEK_SET)
    return handle.read().decode("utf-8", errors="replace")
