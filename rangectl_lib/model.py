from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
RANGES_DIR = ROOT / "ranges"
STATE_DIR = ROOT / "state"
RANGE_PATH_KEY = "__rangectl_range_path"
RANGE_BASE_DIR_KEY = "__rangectl_range_base_dir"


class RangeError(Exception):
    pass


class ValidationResult:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def resolve_range_path(range_arg: str) -> Path:
    candidate = Path(range_arg)
    if candidate.exists():
        return candidate.resolve()

    for content_dir in content_dirs():
        candidates = [
            content_dir / range_arg / "range.yml",
            content_dir / range_arg / "range.yaml",
            content_dir / "ranges" / f"{range_arg}.yml",
            content_dir / "ranges" / f"{range_arg}.yaml",
            content_dir / f"{range_arg}.yml",
            content_dir / f"{range_arg}.yaml",
        ]
        for path in candidates:
            if path.exists():
                return path.resolve()

    direct = RANGES_DIR / range_arg
    if direct.exists():
        return direct.resolve()

    candidates = [
        RANGES_DIR / f"{range_arg}.yml",
        RANGES_DIR / f"{range_arg}.yaml",
        RANGES_DIR / f"{range_arg}-sketch.yml",
        RANGES_DIR / f"{range_arg}-sketch.yaml",
    ]
    for path in candidates:
        if path.exists():
            return path.resolve()

    raise RangeError(f"range not found: {range_arg}")


def content_dirs() -> list[Path]:
    paths: list[Path] = []
    raw = os.environ.get("RANGECTL_CONTENT_DIRS", "")
    for value in raw.split(os.pathsep):
        if value.strip():
            paths.append(Path(value).expanduser())

    paths.extend([
        ROOT / "content",
        Path.home() / "lab-content",
        Path.home() / "rangectl-lab-content" / "packs",
    ])

    resolved: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        expanded = path.expanduser()
        if not expanded.exists():
            continue
        resolved_path = expanded.resolve()
        if resolved_path in seen:
            continue
        seen.add(resolved_path)
        resolved.append(resolved_path)
    return resolved


def load_range(range_arg: str) -> tuple[Path, dict[str, Any]]:
    path = resolve_range_path(range_arg)
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise RangeError(f"range file did not parse to a mapping: {path}")
    data[RANGE_PATH_KEY] = str(path)
    data[RANGE_BASE_DIR_KEY] = str(path.parent)
    return path, data


def range_name(data: dict[str, Any]) -> str:
    return str(data.get("metadata", {}).get("name", "unnamed"))


def range_state_dir(data: dict[str, Any]) -> Path:
    return STATE_DIR / range_name(data)


def range_base_dir(data: dict[str, Any]) -> Path:
    return Path(str(data.get(RANGE_BASE_DIR_KEY) or ROOT)).resolve()


def flatten_spaces(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    spaces: dict[str, dict[str, Any]] = {}
    for category, entries in (data.get("spaces") or {}).items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and "name" in entry:
                enriched = dict(entry)
                enriched["category"] = category
                spaces[str(entry["name"])] = enriched
    return spaces


def provider_for_owner(owner: str) -> str | None:
    if owner.startswith("tofu:"):
        return owner.split(":", 1)[1]
    if owner == "tart":
        return "tart"
    if owner == "external":
        return None
    return None


def validate_range(data: dict[str, Any]) -> ValidationResult:
    result = ValidationResult()

    if data.get("apiVersion") != "rangectl.dev/v1alpha1":
        result.error("apiVersion must be rangectl.dev/v1alpha1")
    if data.get("kind") != "Range":
        result.error("kind must be Range")

    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        result.error("metadata must be a mapping")
    else:
        name = metadata.get("name")
        if not isinstance(name, str) or not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
            result.error("metadata.name must match ^[a-z0-9][a-z0-9-]*$")

    providers = data.get("providers")
    if not isinstance(providers, dict):
        result.error("providers must be a mapping")
        providers = {}

    spaces_raw = data.get("spaces")
    if not isinstance(spaces_raw, dict):
        result.error("spaces must be a mapping")
        spaces_raw = {}

    spaces = flatten_spaces(data)
    networks: dict[str, ipaddress.IPv4Network] = {}
    for category, entries in spaces_raw.items():
        if not isinstance(entries, list):
            result.error(f"spaces.{category} must be a list")
            continue
        if category == "grey" and len(entries) > 1:
            result.error("only one grey space is supported in the first implementation")
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                result.error(f"spaces.{category}[{idx}] must be a mapping")
                continue
            name = entry.get("name")
            cidr = entry.get("cidr")
            gateway = entry.get("gateway")
            vlan = entry.get("vlan")
            if not isinstance(name, str):
                result.error(f"spaces.{category}[{idx}].name is required")
                continue
            if name in networks:
                result.error(f"duplicate space name: {name}")
                continue
            try:
                network = ipaddress.ip_network(str(cidr), strict=False)
                if network.version != 4:
                    result.error(f"space {name} must be IPv4")
                    continue
                networks[name] = network
            except ValueError as exc:
                result.error(f"space {name} has invalid cidr {cidr!r}: {exc}")
                continue
            try:
                gateway_ip = ipaddress.ip_address(str(gateway))
                if gateway_ip not in network:
                    result.error(f"space {name} gateway {gateway} is not inside {network}")
            except ValueError as exc:
                result.error(f"space {name} has invalid gateway {gateway!r}: {exc}")
            if not isinstance(vlan, int) or not 1 <= vlan <= 4094:
                result.error(f"space {name} vlan must be an integer from 1 to 4094")

    for left_name, left_net in networks.items():
        for right_name, right_net in networks.items():
            if left_name >= right_name:
                continue
            if left_net.overlaps(right_net):
                result.error(f"spaces overlap: {left_name} {left_net} and {right_name} {right_net}")

    hosts = data.get("hosts")
    if not isinstance(hosts, list):
        result.error("hosts must be a list")
        hosts = []

    names: set[str] = set()
    hostnames: set[str] = set()
    ips_by_space: dict[tuple[str, str], str] = {}
    tart_hosts = {
        str(host.get("name"))
        for host in ((providers.get("tart") or {}).get("hosts") or [])
        if isinstance(host, dict) and host.get("name")
    }

    for idx, host in enumerate(hosts):
        if not isinstance(host, dict):
            result.error(f"hosts[{idx}] must be a mapping")
            continue
        name = host.get("name")
        hostname = host.get("hostname")
        owner = host.get("owner")
        host_type = host.get("type")
        if not isinstance(name, str):
            result.error(f"hosts[{idx}].name is required")
            continue
        if name in names:
            result.error(f"duplicate host name: {name}")
        names.add(name)
        if not isinstance(hostname, str):
            result.error(f"host {name} hostname is required")
        elif hostname in hostnames:
            result.warn(f"duplicate hostname across hosts: {hostname}")
        else:
            hostnames.add(hostname)

        if owner not in {"tofu:proxmox", "tofu:aws", "tofu:azure", "tart", "external"}:
            result.error(f"host {name} has invalid owner {owner!r}")
        provider = provider_for_owner(str(owner))
        if provider is not None and provider not in providers:
            result.error(f"host {name} owner {owner} references missing provider {provider}")

        if owner == "tofu:proxmox" and host_type not in {"vm", "lxc", "router"}:
            result.error(f"host {name} owner tofu:proxmox cannot manage type {host_type}")
        if owner in {"tofu:aws", "tofu:azure"} and host_type != "cloud_vm":
            result.error(f"host {name} owner {owner} requires type cloud_vm")
        if owner == "tart" and host_type != "macos_tart":
            result.error(f"host {name} owner tart requires type macos_tart")
        if owner == "external" and host_type != "external":
            result.error(f"host {name} owner external requires type external")

        if host_type in {"vm", "lxc", "router", "macos_tart", "cloud_vm"} and not host.get("template") and host_type != "macos_tart":
            result.error(f"host {name} requires template")

        if host_type == "macos_tart":
            tart_host = host.get("tart_host", host.get("provider_host"))
            if tart_host not in tart_hosts:
                result.error(f"host {name} references unknown Tart host {tart_host!r}")

        _validate_host_ip(result, networks, ips_by_space, name, host.get("space"), host.get("ip"))
        for net_idx, network_ref in enumerate(host.get("networks") or []):
            if not isinstance(network_ref, dict):
                result.error(f"host {name} networks[{net_idx}] must be a mapping")
                continue
            _validate_host_ip(
                result,
                networks,
                ips_by_space,
                name,
                network_ref.get("space"),
                network_ref.get("ip"),
            )

    phases = data.get("phases")
    if phases is not None:
        if not isinstance(phases, list):
            result.error("phases must be a list")
        else:
            phase_names: set[str] = set()
            for idx, phase in enumerate(phases):
                if not isinstance(phase, dict):
                    result.error(f"phases[{idx}] must be a mapping")
                    continue
                phase_name = phase.get("name")
                if not isinstance(phase_name, str) or not phase_name:
                    result.error(f"phases[{idx}].name is required")
                elif phase_name in phase_names:
                    result.error(f"duplicate phase name: {phase_name}")
                else:
                    phase_names.add(phase_name)
                if "description" in phase and not isinstance(phase.get("description"), str):
                    result.error(f"phase {phase_name or idx} description must be a string")
                if "manual" in phase and not isinstance(phase.get("manual"), bool):
                    result.error(f"phase {phase_name or idx} manual must be true or false")
                playbooks = phase.get("playbooks")
                if playbooks is None:
                    result.warn(f"phase {phase_name or idx} has no playbooks")
                elif not isinstance(playbooks, list):
                    result.error(f"phase {phase_name or idx} playbooks must be a list")
                else:
                    for playbook_idx, playbook in enumerate(playbooks):
                        if not isinstance(playbook, str) or not playbook:
                            result.error(f"phase {phase_name or idx} playbooks[{playbook_idx}] must be a non-empty string")

    return result


def _validate_host_ip(
    result: ValidationResult,
    networks: dict[str, ipaddress.IPv4Network],
    ips_by_space: dict[tuple[str, str], str],
    host_name: str,
    space_name: Any,
    ip_value: Any,
) -> None:
    if space_name is None and ip_value is None:
        return
    if not isinstance(space_name, str):
        result.error(f"host {host_name} has ip but no valid space")
        return
    if space_name not in networks:
        result.error(f"host {host_name} references unknown space {space_name}")
        return
    try:
        ip = ipaddress.ip_address(str(ip_value))
    except ValueError as exc:
        result.error(f"host {host_name} has invalid ip {ip_value!r}: {exc}")
        return
    if ip not in networks[space_name]:
        result.error(f"host {host_name} ip {ip} is not inside space {space_name} {networks[space_name]}")
    key = (space_name, str(ip))
    if key in ips_by_space and ips_by_space[key] != host_name:
        result.error(f"duplicate ip {ip} in space {space_name}: {ips_by_space[key]} and {host_name}")
    else:
        ips_by_space[key] = host_name


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def merged_env_with_files(paths: list[Path]) -> dict[str, str]:
    merged = dict(os.environ)
    for path in paths:
        merged.update(load_env_file(path))
    return merged


def merged_env_with_file(path: Path) -> dict[str, str]:
    return merged_env_with_files([path])
