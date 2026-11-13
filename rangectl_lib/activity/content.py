from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .models import AiSettings, Persona


class ContentProvider:
    def generate(self, *, persona: Persona, topic: str, kind: str, seed: str = "") -> str:
        raise NotImplementedError


class StaticContentProvider(ContentProvider):
    def generate(self, *, persona: Persona, topic: str, kind: str, seed: str = "") -> str:
        name = persona.display_name or persona.username
        role = persona.role
        interest = ", ".join(persona.interests[:3]) if persona.interests else "normal office work"
        if kind == "email":
            return (
                f"Hi,\n\nI took a look at the {topic} item and left a few notes for follow-up. "
                f"This is from {name} in a {role} role, so the details are focused on {interest}.\n\nThanks,\n{name}\n"
            )
        if kind == "csv":
            return "Item,Owner,Status,Notes\n" f"{topic.title()},{name},Open,Follow up this week\n"
        return (
            f"{topic.title()}\n\n"
            f"{name} ({role}) worked on this during normal business activity. "
            f"The notes reference {interest} and routine operational follow-up.\n"
        )


class OpenAICompatibleContentProvider(ContentProvider):
    def __init__(self, settings: AiSettings) -> None:
        self.settings = settings

    def generate(self, *, persona: Persona, topic: str, kind: str, seed: str = "") -> str:
        endpoint = os.environ.get(self.settings.endpoint_env, "").rstrip("/")
        api_key = os.environ.get(self.settings.api_key_env, "")
        model = os.environ.get(self.settings.model_env, self.settings.default_model)
        if not endpoint or not api_key:
            return StaticContentProvider().generate(persona=persona, topic=topic, kind=kind, seed=seed)

        prompt = (
            "Generate realistic benign business content for a cyber range background user. "
            "Do not include real secrets, malware, exploit instructions, or external links unless explicitly part of the topic.\n"
            f"Persona: {persona.display_name or persona.username}, role={persona.role}, department={persona.department}, "
            f"interests={', '.join(persona.interests)}.\n"
            f"Kind: {kind}\nTopic: {topic}\nSeed: {seed}\n"
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You create concise, realistic, benign synthetic office artifacts."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.8,
        }
        request = urllib.request.Request(
            f"{endpoint}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return str(content).strip()
        except (KeyError, IndexError, json.JSONDecodeError, urllib.error.URLError, TimeoutError):
            return StaticContentProvider().generate(persona=persona, topic=topic, kind=kind, seed=seed)


class CachedContentProvider(ContentProvider):
    def __init__(self, inner: ContentProvider, cache_dir: Path) -> None:
        self.inner = inner
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, *, persona: Persona, topic: str, kind: str, seed: str = "") -> str:
        key = _cache_key({"persona": persona.id, "topic": topic, "kind": kind, "seed": seed})
        path = self.cache_dir / f"{key}.txt"
        if path.exists():
            return path.read_text(encoding="utf-8")
        content = self.inner.generate(persona=persona, topic=topic, kind=kind, seed=seed)
        path.write_text(content, encoding="utf-8")
        return content


def build_content_provider(settings: AiSettings, state_dir: Path) -> ContentProvider:
    if settings.provider in {"openai", "openai-compatible"}:
        inner: ContentProvider = OpenAICompatibleContentProvider(settings)
    else:
        inner = StaticContentProvider()
    cache_dir = Path(settings.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = state_dir / cache_dir
    return CachedContentProvider(inner, cache_dir)


def _cache_key(data: dict[str, Any]) -> str:
    serialized = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
