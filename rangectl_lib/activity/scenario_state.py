from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class ScenarioStateStore(Protocol):
    remote: bool

    def read_text(self, name: str) -> str | None: ...

    def write_text(self, name: str, content: str) -> None: ...

    def append_text(self, name: str, content: str) -> None: ...


@dataclass(slots=True)
class LocalScenarioStateStore:
    root: Path
    remote: bool = False

    def read_text(self, name: str) -> str | None:
        path = self._path(name)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def write_text(self, name: str, content: str) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def append_text(self, name: str, content: str) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content)

    def _path(self, name: str) -> Path:
        return self.root / _safe_document_name(name)


@dataclass(slots=True)
class HttpScenarioStateStore:
    base_url: str
    token: str
    range_name: str
    host: str
    timeout: float = 30.0
    remote: bool = True
    _cache: dict[str, str | None] = field(default_factory=dict)

    def read_text(self, name: str) -> str | None:
        name = _safe_document_name(name)
        if name in self._cache:
            return self._cache[name]
        request = urllib.request.Request(self._url(name), headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                content = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                content = None
            else:
                raise RuntimeError(f"scenario state server GET failed with HTTP {exc.code}") from exc
        except OSError as exc:
            raise RuntimeError(f"scenario state server GET failed: {exc}") from exc
        self._cache[name] = content
        return content

    def write_text(self, name: str, content: str) -> None:
        name = _safe_document_name(name)
        if self._cache.get(name) == content:
            return
        data = content.encode("utf-8")
        headers = {**self._headers(), "Content-Type": "application/json"}
        if len(data) >= 64 * 1024:
            data = gzip.compress(data)
            headers["Content-Encoding"] = "gzip"
        request = urllib.request.Request(
            self._url(name),
            data=data,
            method="PUT",
            headers=headers,
        )
        self._send(request, "PUT")
        self._cache[name] = content

    def append_text(self, name: str, content: str) -> None:
        name = _safe_document_name(name)
        request = urllib.request.Request(
            self._url(name),
            data=content.encode("utf-8"),
            method="POST",
            headers={**self._headers(), "Content-Type": "application/x-ndjson"},
        )
        self._send(request, "POST")
        cached = self._cache.get(name)
        if cached is not None:
            self._cache[name] = cached + content

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _url(self, name: str) -> str:
        parts = [
            "v1",
            "state",
            urllib.parse.quote(self.range_name, safe=""),
            urllib.parse.quote(self.host, safe=""),
            urllib.parse.quote(name, safe=""),
        ]
        return self.base_url.rstrip("/") + "/" + "/".join(parts)

    def _send(self, request: urllib.request.Request, method: str) -> None:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"scenario state server {method} failed with HTTP {exc.code}") from exc
        except OSError as exc:
            raise RuntimeError(f"scenario state server {method} failed: {exc}") from exc


def build_scenario_state_store(config: object, state_dir: Path) -> ScenarioStateStore:
    base_url = os.environ.get("RANGECTL_SCENARIO_STATE_URL", "").strip()
    if not base_url:
        return LocalScenarioStateStore(state_dir / "scenarios")
    token = os.environ.get("RANGECTL_SCENARIO_STATE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("RANGECTL_SCENARIO_STATE_TOKEN is required with RANGECTL_SCENARIO_STATE_URL")
    return HttpScenarioStateStore(
        base_url=base_url,
        token=token,
        range_name=str(getattr(config, "range_name")),
        host=str(getattr(config, "host")),
    )


def migrate_legacy_scenario_state(state_dir: Path, store: ScenarioStateStore) -> bool:
    if not store.remote:
        return False
    legacy_dir = state_dir / "scenarios"
    if not legacy_dir.exists():
        return False
    files = sorted(path for path in legacy_dir.iterdir() if path.is_file())
    for path in files:
        name = _safe_document_name(path.name)
        content = path.read_text(encoding="utf-8")
        existing = store.read_text(name)
        if existing != content:
            store.write_text(name, content)
    shutil.rmtree(legacy_dir)
    return True


def migrate_legacy_activity_log(state_dir: Path, store: ScenarioStateStore) -> bool:
    if not store.remote:
        return False
    legacy_log = state_dir / "activity.jsonl"
    if not legacy_log.is_file():
        return False

    digest = hashlib.sha256()
    with legacy_log.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    document_prefix = f"legacy-events-{digest.hexdigest()[:16]}"
    maximum_chunk_bytes = 3 * 1024 * 1024
    chunk: list[str] = []
    chunk_bytes = 0
    document_number = 1
    with legacy_log.open("r", encoding="utf-8") as handle:
        for line in handle:
            line_bytes = len(line.encode("utf-8"))
            if chunk and chunk_bytes + line_bytes > maximum_chunk_bytes:
                store.write_text(f"{document_prefix}-{document_number:04d}.jsonl", "".join(chunk))
                document_number += 1
                chunk = []
                chunk_bytes = 0
            chunk.append(line)
            chunk_bytes += line_bytes
        if chunk:
            store.write_text(f"{document_prefix}-{document_number:04d}.jsonl", "".join(chunk))
    legacy_log.unlink()
    return True


def load_json_document(store: ScenarioStateStore, name: str) -> dict[str, object]:
    content = store.read_text(name)
    if content is None:
        return {}
    try:
        loaded = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _safe_document_name(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"invalid scenario state document name: {name!r}")
    if not all(ch.isalnum() or ch in {"-", "_", "."} for ch in name):
        raise ValueError(f"invalid scenario state document name: {name!r}")
    return name
