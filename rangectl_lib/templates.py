from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from rangectl_lib.model import ROOT, content_dirs, range_base_dir


def load_template_catalog(range_data: dict[str, Any] | None = None) -> dict[str, Any]:
    catalog: dict[str, Any] = {
        "proxmox": {
            "aliases": {},
            "vm_templates": {},
            "lxc_templates": {},
        }
    }
    for path in _catalog_paths(range_data):
        data = _load_yaml(path)
        if data:
            _merge_catalog(catalog, data)
    return catalog


def proxmox_template_catalog(range_data: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    raw = load_template_catalog(range_data).get("proxmox") or {}
    aliases = {str(key): str(value) for key, value in (raw.get("aliases") or {}).items()}

    vm_templates: dict[str, int] = {}
    for name, value in (raw.get("vm_templates") or {}).items():
        if isinstance(value, dict):
            vmid = value.get("vmid")
        else:
            vmid = value
        if vmid is not None:
            vm_templates[str(name)] = int(vmid)

    lxc_templates: dict[str, str] = {}
    for name, value in (raw.get("lxc_templates") or {}).items():
        if isinstance(value, dict):
            file_id = value.get("file_id") or value.get("template")
        else:
            file_id = value
        if file_id:
            lxc_templates[str(name)] = str(file_id)

    return {
        "aliases": aliases,
        "vm_templates": vm_templates,
        "lxc_templates": lxc_templates,
    }


def _catalog_paths(range_data: dict[str, Any] | None) -> list[Path]:
    candidates: list[Path] = []
    candidates.extend(sorted((ROOT / "templates").glob("*.yml")))
    candidates.extend(sorted((ROOT / "templates").glob("*.yaml")))
    for content_dir in content_dirs():
        candidates.extend(sorted((content_dir / "templates").glob("*.yml")))
        candidates.extend(sorted((content_dir / "templates").glob("*.yaml")))
    if range_data is not None:
        range_templates = range_base_dir(range_data) / "templates"
        candidates.extend(sorted(range_templates.glob("*.yml")))
        candidates.extend(sorted(range_templates.glob("*.yaml")))

    paths: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append(resolved)
    return paths


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _merge_catalog(target: dict[str, Any], source: dict[str, Any]) -> None:
    proxmox = source.get("proxmox")
    if not isinstance(proxmox, dict):
        return
    target_proxmox = target.setdefault("proxmox", {})
    for key in ("aliases", "vm_templates", "lxc_templates"):
        values = proxmox.get(key)
        if isinstance(values, dict):
            target_proxmox.setdefault(key, {}).update(values)
