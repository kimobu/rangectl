from __future__ import annotations

import os
import tempfile
from pathlib import Path


def secure_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(temporary, mode)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def safe_child_path(directory: Path, filename: str) -> Path:
    if not filename or Path(filename).name != filename or filename in {".", ".."}:
        raise ValueError(f"unsafe filename: {filename!r}")
    directory = directory.resolve()
    target = (directory / filename).resolve()
    if target.parent != directory:
        raise ValueError(f"path escapes destination directory: {filename!r}")
    return target
