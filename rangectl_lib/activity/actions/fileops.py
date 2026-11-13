from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any, Callable


def register(add: Callable[[str, Any], None]) -> None:
    add("file.create_text", create_text)
    add("file.create_csv", create_csv)
    add("file.create_html", create_html)


def create_text(context: Any, action: Any) -> dict[str, Any]:
    topic = str(action.args.get("topic", "work notes"))
    directory = _directory(action)
    suffix = str(action.args.get("suffix", "txt"))
    name = _filename(action, topic, suffix)
    content = context.content.generate(persona=context.config.persona, topic=topic, kind="text", seed=name)
    return _write(context, directory / name, content)


def create_csv(context: Any, action: Any) -> dict[str, Any]:
    topic = str(action.args.get("topic", "operational data"))
    directory = _directory(action)
    name = _filename(action, topic, "csv")
    content = context.content.generate(persona=context.config.persona, topic=topic, kind="csv", seed=name)
    return _write(context, directory / name, content)


def create_html(context: Any, action: Any) -> dict[str, Any]:
    topic = str(action.args.get("topic", "status update"))
    directory = _directory(action)
    name = _filename(action, topic, "html")
    body = context.content.generate(persona=context.config.persona, topic=topic, kind="html", seed=name)
    content = f"<!doctype html><html><head><title>{topic}</title></head><body><pre>{body}</pre></body></html>\n"
    return _write(context, directory / name, content)


def _write(context: Any, target: Path, content: str) -> dict[str, Any]:
    if context.dry_run:
        return {"target": str(target), "bytes": len(content.encode("utf-8"))}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"target": str(target), "bytes": target.stat().st_size}


def _directory(action: Any) -> Path:
    return Path(str(action.args.get("directory", "~/Documents"))).expanduser()


def _filename(action: Any, topic: str, suffix: str) -> str:
    if action.args.get("filename"):
        return str(action.args["filename"])
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in topic).strip("-") or "artifact"
    return f"{slug}-{int(time.time())}-{random.randint(1000, 9999)}.{suffix.lstrip('.')}"
