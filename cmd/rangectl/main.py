#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from shutil import which
from typing import Any

from rangectl_lib.proxmox_api import ProxmoxClient, ProxmoxError
from rangectl_lib.tart import (
    TartError,
    TartRunner,
    apply_guest,
    guest_status,
    list_vms,
    start_guest,
    stop_guest,
    tart_command_for_host,
    tart_hosts_from_range,
    unresolved_tart_env,
    update_tart_runtime_state,
    validate_tart_host,
    vm_entry,
)
from rangectl_lib.model import (
    ROOT,
    RANGES_DIR,
    RangeError,
    content_dirs,
    load_range,
    merged_env_with_files,
    range_base_dir,
    range_name,
    range_state_dir,
    validate_range,
)
from rangectl_lib.render import (
    ad_compat_data_dir,
    render_all,
    render_docs,
)
from rangectl_lib.templates import proxmox_template_catalog
from rangectl_lib.template_builds import (
    inspect_template_source_url,
    load_template_build_definition,
    render_packer_windows_proxmox_build,
    run_packer_build,
    run_packer_validate,
    template_build_tool_status,
)
from rangectl_lib.activity.agent import run_agent
from rangectl_lib.activity.compiler import compile_activity
from rangectl_lib.activity.models import ActivityConfigError, AgentConfig, RangeActivity


RANGECTL_ENV_FILE = ROOT / ".rangectl.env"

CHECK_OK = "ok"
CHECK_WARN = "warn"
CHECK_ERROR = "error"


def _rangectl_env() -> dict[str, str]:
    env = merged_env_with_files([RANGECTL_ENV_FILE])
    if env.get("PROXMOX_VE_CA_FILE") and not env.get("SSL_CERT_FILE"):
        env["SSL_CERT_FILE"] = env["PROXMOX_VE_CA_FILE"]
    return env


def _rangectl_env_hint() -> str:
    return f"environment or {RANGECTL_ENV_FILE}"


def _load_range_or_error(range_arg: str) -> tuple[Path, dict[str, Any]] | None:
    try:
        return load_range(range_arg)
    except RangeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return None


def _print_validation_messages(result: Any) -> int:
    if result.warnings:
        print("\nWarnings:")
        for warning in result.warnings:
            print(f"  - {warning}")
    if result.errors:
        print("\nErrors:")
        for error in result.errors:
            print(f"  - {error}")
        return 1
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        path, data = load_range(args.range)
    except RangeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    result = validate_range(data)
    if args.online:
        providers = data.get("providers", {})
        if _proxmox_enabled(data):
            proxmox_env = _rangectl_env()
            proxmox = providers.get("proxmox", {}) if isinstance(providers, dict) else {}
            endpoint_key = proxmox.get("endpoint_env") or "PROXMOX_VE_ENDPOINT"
            if endpoint_key not in proxmox_env:
                result.error(f"online validation missing Proxmox endpoint env var {endpoint_key} in {_rangectl_env_hint()}")
            token_key = proxmox.get("token_env") or "PROXMOX_VE_API_TOKEN"
            has_token = token_key in proxmox_env
            has_password_auth = "PROXMOX_VE_USERNAME" in proxmox_env and "PROXMOX_VE_PASSWORD" in proxmox_env
            if not has_token and not has_password_auth:
                result.error(
                    "online validation needs either PROXMOX_VE_API_TOKEN or "
                    f"PROXMOX_VE_USERNAME/PROXMOX_VE_PASSWORD in {RANGECTL_ENV_FILE}"
                )
            if not result.errors:
                _populate_tf_vars(proxmox_env)
                _online_proxmox_preflight(result, data, proxmox_env)
        if any(host.get("owner") == "tart" for host in data.get("hosts") or []):
            tart_env = _rangectl_env()
            _online_tart_preflight(result, data, tart_env)
        if _aws_enabled(data):
            aws_env = _rangectl_env()
            _online_aws_preflight(result, data, aws_env)

    print(f"Range: {range_name(data)}")
    print(f"File: {path}")
    if result.warnings:
        print("\nWarnings:")
        for warning in result.warnings:
            print(f"  - {warning}")
    if result.errors:
        print("\nErrors:")
        for error in result.errors:
            print(f"  - {error}")
        return 1
    print("Validation: ok")
    return 0


def _aws_enabled(data: dict[str, Any]) -> bool:
    providers = data.get("providers") or {}
    aws = providers.get("aws") if isinstance(providers, dict) else {}
    return bool(
        isinstance(aws, dict)
        and aws.get("enabled")
        or any(host.get("owner") == "tofu:aws" for host in data.get("hosts") or [])
        or (data.get("cloud") or {}).get("aws")
    )


def _azure_enabled(data: dict[str, Any]) -> bool:
    providers = data.get("providers") or {}
    azure = providers.get("azure") if isinstance(providers, dict) else {}
    return bool(
        isinstance(azure, dict)
        and azure.get("enabled")
        or any(host.get("owner") == "tofu:azure" for host in data.get("hosts") or [])
    )


def _proxmox_enabled(data: dict[str, Any]) -> bool:
    providers = data.get("providers") or {}
    proxmox = providers.get("proxmox") if isinstance(providers, dict) else {}
    return bool(
        isinstance(proxmox, dict)
        and proxmox
        or any(host.get("owner") == "tofu:proxmox" for host in data.get("hosts") or [])
    )


INFRA_STACK_NAMES = {"proxmox", "aws", "azure", "tart"}


def _skip_provider_names(args: argparse.Namespace | None = None, env: dict[str, str] | None = None) -> set[str]:
    raw_values: list[str] = []
    if env and env.get("RANGECTL_SKIP_PROVIDERS"):
        raw_values.append(env["RANGECTL_SKIP_PROVIDERS"])
    elif os.environ.get("RANGECTL_SKIP_PROVIDERS"):
        raw_values.append(os.environ["RANGECTL_SKIP_PROVIDERS"])
    raw_values.extend(getattr(args, "skip_provider", None) or [])

    skipped: set[str] = set()
    for raw in raw_values:
        for item in str(raw).split(","):
            name = item.strip().lower()
            if name:
                skipped.add(name)
    unknown = skipped - INFRA_STACK_NAMES
    if unknown:
        raise ValueError(f"unknown provider in skip list: {', '.join(sorted(unknown))}")
    return skipped


def _enabled_infra_stacks(data: dict[str, Any], skip: set[str] | None = None) -> list[str]:
    skip = skip or set()
    stacks = []
    if _proxmox_enabled(data) and "proxmox" not in skip:
        stacks.append("proxmox")
    if _aws_enabled(data) and "aws" not in skip:
        stacks.append("aws")
    if _azure_enabled(data) and "azure" not in skip:
        stacks.append("azure")
    if any(host.get("owner") == "tart" for host in data.get("hosts") or []) and "tart" not in skip:
        stacks.append("tart")
    return stacks


def _selected_infra_stacks(data: dict[str, Any], host_filter: str | None, skip: set[str] | None = None) -> list[str]:
    skip = skip or set()
    if not host_filter:
        return _enabled_infra_stacks(data, skip)
    selected_hosts = _select_hosts(data, host_filter)
    selected = []
    if any(host.get("owner") == "tofu:proxmox" for host in selected_hosts):
        selected.append("proxmox")
    if any(host.get("owner") == "tofu:aws" for host in selected_hosts) or _aws_power_targets(data, host_filter):
        selected.append("aws")
    if any(host.get("owner") == "tofu:azure" for host in selected_hosts):
        selected.append("azure")
    if any(host.get("owner") == "tart" for host in selected_hosts):
        selected.append("tart")
    return [stack for stack in _enabled_infra_stacks(data, skip) if stack in selected]


def _online_aws_preflight(result: Any, data: dict[str, Any], env: dict[str, str]) -> None:
    if not which("aws"):
        result.error("AWS online validation requires the aws CLI in PATH")
        return

    providers = data.get("providers") or {}
    aws = providers.get("aws") if isinstance(providers, dict) else {}
    if not isinstance(aws, dict):
        aws = {}
    profile_key = aws.get("profile_env") or "AWS_PROFILE"
    region_key = aws.get("region_env") or "AWS_REGION"
    account_key = aws.get("account_id_env") or "AWS_ACCOUNT_ID"

    if profile_key not in env:
        result.error(f"online validation missing AWS profile env var {profile_key} in {_rangectl_env_hint()}")
    if region_key not in env:
        result.error(f"online validation missing AWS region env var {region_key} in {_rangectl_env_hint()}")
    if result.errors:
        return

    completed = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--output", "json"],
        env=env,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        result.error(f"AWS STS preflight failed: {(completed.stderr or completed.stdout).strip()}")
        return
    try:
        identity = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        result.error(f"AWS STS preflight returned invalid JSON: {exc}")
        return

    expected_account = env.get(account_key)
    actual_account = str(identity.get("Account") or "")
    if expected_account and actual_account != expected_account:
        result.error(f"AWS account mismatch: expected {expected_account}, got {actual_account}")
    arn = str(identity.get("Arn") or "")
    if "assumed-role/rangectl-lab-admin/" not in arn:
        result.warning(f"AWS profile {env.get(profile_key)!r} is authenticated as {arn}; expected rangectl-lab-admin role")


def _online_proxmox_preflight(result: Any, data: dict[str, Any], env: dict[str, str]) -> None:
    try:
        client = ProxmoxClient.from_env(env)
        client.get("version")
    except ProxmoxError as exc:
        result.error(f"Proxmox API preflight failed: {exc}")
        return

    node = env.get("TF_VAR_pm_node") or env.get("PROXMOX_NODE")
    pool = env.get("TF_VAR_pm_pool") or env.get("PROXMOX_POOL")
    storages = {
        env.get("TF_VAR_storage"),
        env.get("TF_VAR_pfsense_storage"),
        env.get("TF_VAR_lxc_storage"),
    }
    storages.discard(None)

    try:
        nodes = {item.get("node") for item in client.get("nodes") or []}
        if node not in nodes:
            result.error(f"Proxmox node {node!r} not found; available nodes: {', '.join(sorted(str(n) for n in nodes if n))}")

        pool_ids = {item.get("poolid") for item in client.get("pools") or []}
        if pool and pool not in pool_ids:
            result.error(f"Proxmox pool {pool!r} not found")

        if node:
            storage_ids = {item.get("storage") for item in client.get(f"nodes/{node}/storage") or []}
            for storage in sorted(str(item) for item in storages if item):
                if storage not in storage_ids:
                    result.error(f"Proxmox storage {storage!r} not found on node {node}")

        resources = client.get("cluster/resources?type=vm") or []
        by_vmid = {int(item["vmid"]): item for item in resources if item.get("vmid") is not None}
        existing_names = {str(item.get("name")) for item in resources if item.get("name")}
        range_names = {
            _proxmox_resource_name(host)
            for host in data.get("hosts") or []
            if host.get("owner") == "tofu:proxmox"
        }
        conflicts = sorted(existing_names & range_names)
        if conflicts:
            result.warn(
                "Proxmox already has resources with range host names: "
                + ", ".join(conflicts)
                + ". If these are not already imported into this range state, import or remove them before Terraform apply."
            )
        needed_templates = _needed_proxmox_vm_template_ids(data)
        for label, vmid in sorted(needed_templates.items(), key=lambda item: item[1]):
            resource = by_vmid.get(vmid)
            if not resource:
                result.error(f"Proxmox VM template {label} expects VMID {vmid}, but that VMID was not found")
            elif not resource.get("template"):
                result.warn(f"Proxmox VMID {vmid} for template {label} exists but is not marked as a template")

        if node:
            for template in sorted(_needed_proxmox_lxc_templates(data)):
                storage, file_path = _split_proxmox_file_id(template)
                try:
                    contents = client.get(f"nodes/{node}/storage/{storage}/content?content=vztmpl") or []
                except ProxmoxError as exc:
                    result.error(f"Could not list LXC templates on storage {storage!r}: {exc}")
                    continue
                volids = {item.get("volid") for item in contents}
                if template not in volids and file_path not in volids:
                    result.error(f"Proxmox LXC template {template!r} not found on node {node}")
    except ProxmoxError as exc:
        result.error(f"Proxmox API preflight failed: {exc}")


def _online_tart_preflight(result: Any, data: dict[str, Any], env: dict[str, str]) -> None:
    missing = unresolved_tart_env(data, env)
    for env_name in missing:
        result.error(f"online validation missing Tart env var {env_name} in {_rangectl_env_hint()}")
    if missing:
        return

    try:
        tart_hosts = tart_hosts_from_range(data, env)
        seen: set[str] = set()
        for host in data.get("hosts") or []:
            if host.get("owner") != "tart":
                continue
            tart_host_name = str(host.get("tart_host", host.get("provider_host")))
            if tart_host_name not in seen:
                if tart_host_name not in tart_hosts:
                    result.error(f"Tart host {tart_host_name!r} is not configured")
                    continue
                seen.add(tart_host_name)
                info = validate_tart_host(TartRunner(tart_hosts[tart_host_name], env), probe_vm=str(host.get("name")))
                interfaces = info.get("bridge_interfaces") or []
                for guest in data.get("hosts") or []:
                    if guest.get("owner") != "tart":
                        continue
                    if str(guest.get("tart_host", guest.get("provider_host"))) != tart_host_name:
                        continue
                    network = guest.get("network") or {}
                    if network.get("mode") == "bridged":
                        interface = str(network.get("interface"))
                        if interfaces and not any(item.startswith(interface) or f'"{interface}"' in item for item in interfaces):
                            result.error(
                                f"Tart host {tart_host_name} does not expose bridged interface {interface!r}; "
                                f"available: {', '.join(interfaces)}"
                            )
    except TartError as exc:
        result.error(f"Tart preflight failed: {exc}")


def _needed_proxmox_vm_template_ids(data: dict[str, Any]) -> dict[str, int]:
    needed: dict[str, int] = {}
    template_catalog = proxmox_template_catalog(data)
    aliases = template_catalog["aliases"]
    vm_template_ids = template_catalog["vm_templates"]
    for host in data.get("hosts") or []:
        if host.get("owner") != "tofu:proxmox" or host.get("type") == "lxc":
            continue
        template = aliases.get(str(host.get("template")), str(host.get("template")))
        if template in vm_template_ids:
            needed[template] = int(vm_template_ids[template])
    return needed


def _needed_proxmox_lxc_templates(data: dict[str, Any]) -> set[str]:
    needed: set[str] = set()
    lxc_templates = proxmox_template_catalog(data)["lxc_templates"]
    for host in data.get("hosts") or []:
        if host.get("owner") == "tofu:proxmox" and host.get("type") == "lxc":
            template = lxc_templates.get(str(host.get("template")), str(host.get("template")))
            needed.add(template)
    return needed


def _split_proxmox_file_id(file_id: str) -> tuple[str, str]:
    storage, _, path = file_id.partition(":")
    return storage, path


def cmd_render(args: argparse.Namespace) -> int:
    try:
        _, data = load_range(args.range)
    except RangeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    result = validate_range(data)
    if result.errors:
        for error in result.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    written = render_all(data)
    for path in written:
        print(path)
    return 0


def cmd_doc_create(args: argparse.Namespace) -> int:
    try:
        _, data = load_range(args.range)
    except RangeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    result = validate_range(data)
    if result.errors:
        for error in result.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    path = render_docs(data, Path("docs") / "generated")
    print(path)
    return 0


def cmd_activity(args: argparse.Namespace) -> int:
    if args.activity_command == "run":
        try:
            config = AgentConfig.from_file(Path(args.config))
        except (ActivityConfigError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        return run_agent(config, once=args.once, dry_run=args.dry_run)

    try:
        _, data = load_range(args.range)
    except RangeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        activity = RangeActivity.from_range(data)
    except ActivityConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.activity_command == "validate":
        print(f"Range: {range_name(data)}")
        print(f"Activity enabled: {activity.enabled}")
        print(f"Personas: {len(activity.personas)}")
        for persona in activity.personas:
            profile = activity.profiles.get(persona.role) or activity.profiles.get("office")
            action_count = len(profile.actions) if profile else 0
            print(
                f"  - {persona.id}: host={persona.host} user={persona.username} "
                f"role={persona.role} email={persona.email.backend} actions={action_count}"
            )
        if not activity.personas:
            print("WARNING: no personas discovered or configured")
        return 0

    if args.activity_command == "compile":
        out_dir = Path(args.out) if args.out else None
        try:
            written = compile_activity(data, out_dir=out_dir)
        except ActivityConfigError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        for path in written:
            print(path)
        if not written:
            print("WARNING: no activity configs written")
        return 0

    if args.activity_command == "trigger":
        trigger = {
            "type": args.type,
            "args": _parse_key_values(args.arg or []),
            "trackable_id": args.trackable_id,
        }
        hosts = [item.strip() for item in args.hosts.split(",") if item.strip()]
        if not hosts:
            print("ERROR: --hosts must name at least one compiled activity host", file=sys.stderr)
            return 2
        for host in hosts:
            trigger_dir = range_state_dir(data) / "activity" / host / "triggers"
            trigger_dir.mkdir(parents=True, exist_ok=True)
            path = trigger_dir / f"{int(time.time() * 1000)}-{args.type.replace('.', '-')}.json"
            path.write_text(json.dumps(trigger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(path)
        return 0

    if args.activity_command == "collect":
        base = range_state_dir(data) / "activity"
        logs = sorted(base.glob("*/activity.jsonl"))
        for log in logs:
            if args.paths:
                print(log)
                continue
            for line in log.read_text(encoding="utf-8").splitlines():
                print(line)
        if not logs and not args.paths:
            print(f"WARNING: no local activity logs found under {base}")
        return 0

    print(f"ERROR: unsupported activity command {args.activity_command}", file=sys.stderr)
    return 2


def _parse_key_values(values: list[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator:
            parsed[key] = True
            continue
        try:
            parsed[key] = json.loads(value)
        except json.JSONDecodeError:
            parsed[key] = value
    return parsed


def cmd_list(args: argparse.Namespace) -> int:
    by_name: dict[str, Path] = {}
    for content_dir in content_dirs():
        candidate_paths = list(content_dir.glob("*/range.y*ml"))
        candidate_paths.extend((content_dir / "ranges").glob("*.y*ml") if (content_dir / "ranges").exists() else [])
        for path in sorted(candidate_paths):
            try:
                _, data = load_range(str(path))
                name = range_name(data)
            except Exception:
                name = path.parent.name if path.name.startswith("range.") else path.stem
            by_name.setdefault(name, path)
    for path in sorted(RANGES_DIR.glob("*.y*ml")):
        try:
            _, data = load_range(str(path))
            name = range_name(data)
        except Exception:
            name = path.stem
        existing = by_name.get(name)
        if existing is None or ("-sketch" in existing.stem and "-sketch" not in path.stem):
            by_name[name] = path
    for name, path in sorted(by_name.items()):
        print(f"{name}\t{path}")
    return 0


def cmd_inventory(args: argparse.Namespace) -> int:
    try:
        _, data = load_range(args.range)
    except RangeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    path = range_state_dir(data) / "inventory" / "hosts.yml"
    if not path.exists() or args.render:
        result = validate_range(data)
        if result.errors:
            for error in result.errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        render_all(data)
    if args.path:
        print(path)
    else:
        print(path.read_text(encoding="utf-8"))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    loaded = _load_range_or_error(args.range)
    if loaded is None:
        return 2
    _, data = loaded
    state_path = range_state_dir(data) / "range-state.json"
    if not state_path.exists():
        print(f"Range: {range_name(data)}")
        print("State: not rendered")
        print(f"Expected: {state_path}")
        return 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    print(f"Range: {state['name']}")
    print(f"State: {state_path}")
    print(f"Rendered: {_format_mtime(state_path)}")
    inventory_path = range_state_dir(data) / "inventory" / "hosts.yml"
    print(f"Inventory: {inventory_path} ({'present' if inventory_path.exists() else 'missing'})")
    print(f"Hosts: {len(state.get('hosts', []))}")
    by_owner: dict[str, int] = {}
    for host in state.get("hosts", []):
        owner = host.get("owner", "unknown")
        by_owner[owner] = by_owner.get(owner, 0) + 1
    for owner, count in sorted(by_owner.items()):
        print(f"  {owner}: {count}")
    tart_runtime = ((state.get("runtime") or {}).get("tart") or {}).get("hosts") or {}
    if tart_runtime:
        print("Tart runtime:")
        for name, info in sorted(tart_runtime.items()):
            print(
                f"  {name}: {info.get('state', 'unknown')} "
                f"ip={info.get('discovered_ip') or '-'} mac={info.get('mac_address') or '-'}"
            )
    activity_dir = range_state_dir(data) / "activity"
    if activity_dir.exists():
        configs = sorted(activity_dir.glob("*.activity.json"))
        logs = sorted(activity_dir.glob("*/activity.jsonl"))
        print(f"Activity: configs={len(configs)} logs={len(logs)} path={activity_dir}")
    manual_phases = [
        str(phase.get("name"))
        for phase in data.get("phases") or []
        if isinstance(phase, dict) and phase.get("manual")
    ]
    if manual_phases:
        print(f"Manual configure phases: {', '.join(manual_phases)}")
    run_dir = range_state_dir(data) / "runs"
    runs = sorted(run_dir.glob("*.json")) if run_dir.exists() else []
    if runs:
        latest = runs[-1]
        try:
            run_data = json.loads(latest.read_text(encoding="utf-8"))
            print(
                "Latest run: "
                f"{run_data.get('command')} rc={run_data.get('returncode')} "
                f"finished={run_data.get('finished_at')} path={latest}"
            )
        except (OSError, json.JSONDecodeError):
            print(f"Latest run: {latest}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    loaded = _load_range_or_error(args.range)
    if loaded is None:
        return 2
    path, data = loaded
    result = validate_range(data)
    if result.errors:
        for error in result.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Range: {range_name(data)}")
    print(f"File: {path}")
    print(f"Base: {range_base_dir(data)}")
    hosts = data.get("hosts") or []
    print(f"Hosts: {len(hosts)}")
    owner_counts: dict[str, int] = {}
    for host in hosts:
        owner = str(host.get("owner"))
        owner_counts[owner] = owner_counts.get(owner, 0) + 1
    if owner_counts:
        print("Host owners:")
        for owner, count in sorted(owner_counts.items()):
            print(f"  - {owner}: {count}")
    print("Planned phases:")
    if args.phase in {"all", "render"}:
        print("  - render generated inventory, provider inputs, provider manifests, docs")
    if args.phase in {"all", "infra"}:
        try:
            skipped_providers = _skip_provider_names(args)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        stacks = _enabled_infra_stacks(data, skipped_providers)
        print(f"  - infra plan/apply providers: {', '.join(stacks) if stacks else 'none'}")
        if skipped_providers:
            print(f"    skipped providers: {', '.join(sorted(skipped_providers))}")
    if args.phase in {"all", "configure"}:
        phases = [phase for phase in data.get("phases") or [] if isinstance(phase, dict)]
        if not phases:
            print("  - configure phases: none")
        else:
            print("  - configure phases:")
            for phase in phases:
                manual = " manual" if phase.get("manual") else ""
                playbooks = _phase_playbooks(data, phase)
                status = "missing-playbook" if any(not item["exists"] for item in playbooks) else "ready"
                print(f"    - {phase.get('name')}{manual} ({status})")
                description = phase.get("description")
                if description:
                    print(f"      {description}")
                for item in playbooks:
                    marker = "ok" if item["exists"] else "missing"
                    print(f"      {marker}: {item['path']}")
    print("Next commands:")
    print(f"  ./rangectl validate {range_name(data)} --online")
    print(f"  ./rangectl infra plan {range_name(data)}")
    print(f"  ./rangectl up {range_name(data)}")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    args.phase = "all"
    args.auto_approve = True
    return _run_up_sequence(args)


def cmd_destroy(args: argparse.Namespace) -> int:
    infra_args = argparse.Namespace(
        action="destroy",
        range=args.range,
        hosts=args.hosts,
        execute=False,
        auto_approve=args.auto_approve,
        allow_replace=False,
        refresh=args.refresh,
        skip_provider=args.skip_provider,
    )
    return cmd_infra(infra_args)


def cmd_phases(args: argparse.Namespace) -> int:
    loaded = _load_range_or_error(args.range)
    if loaded is None:
        return 2
    _, data = loaded
    phases = []
    for phase in data.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        phases.append({
            "name": str(phase.get("name")),
            "description": str(phase.get("description") or ""),
            "manual": bool(phase.get("manual")),
            "playbooks": _phase_playbooks(data, phase),
            "command": f"./rangectl configure {range_name(data)} --phase {phase.get('name')}",
        })
    if args.json:
        print(json.dumps({"range": range_name(data), "phases": phases}, indent=2, sort_keys=True))
        return 0
    print(f"Range: {range_name(data)}")
    if not phases:
        print("Configure phases: none")
        return 0
    print("Configure phases:")
    for phase in phases:
        manual = " manual" if phase["manual"] else ""
        print(f"  - {phase['name']}{manual}")
        if phase["description"]:
            print(f"    {phase['description']}")
        for playbook in phase["playbooks"]:
            marker = "ok" if playbook["exists"] else "missing"
            print(f"    {marker}: {playbook['path']}")
        print(f"    run: {phase['command']}")
    return 0


def cmd_env(args: argparse.Namespace) -> int:
    loaded = _load_range_or_error(args.range)
    if loaded is None:
        return 2
    _, data = loaded
    env = _rangectl_env()
    requirements = _env_requirements(data, env)
    if args.json:
        print(json.dumps({
            "range": range_name(data),
            "env_file": str(RANGECTL_ENV_FILE),
            "requirements": requirements,
        }, indent=2, sort_keys=True))
        return 0
    print(f"Range: {range_name(data)}")
    print(f"Environment file: {RANGECTL_ENV_FILE}")
    if not requirements:
        print("No provider-specific environment values required.")
        return 0
    for requirement in requirements:
        status = CHECK_OK if requirement["present"] else CHECK_ERROR if requirement["required"] else CHECK_WARN
        required = "required" if requirement["required"] else "optional"
        print(f"{status}\t{requirement['name']}\t{required}\t{requirement['detail']}")
    missing = [item for item in requirements if item["required"] and not item["present"]]
    return 1 if missing else 0


def cmd_doctor(args: argparse.Namespace) -> int:
    loaded = _load_range_or_error(args.range)
    if loaded is None:
        return 2
    path, data = loaded
    checks = _doctor_checks(path, data, _rangectl_env(), online=args.online)
    if args.json:
        print(json.dumps({"range": range_name(data), "checks": checks}, indent=2, sort_keys=True))
    else:
        print(f"Range: {range_name(data)}")
        print(f"File: {path}")
        for check in checks:
            detail = f" - {check['detail']}" if check.get("detail") else ""
            print(f"{check['status']}\t{check['name']}{detail}")
    return 1 if any(check["status"] == CHECK_ERROR for check in checks) else 0


def _format_mtime(path: Path) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(path.stat().st_mtime))
    except OSError:
        return "unknown"


def _phase_playbooks(data: dict[str, Any], phase: dict[str, Any]) -> list[dict[str, Any]]:
    base_dir = range_base_dir(data)
    playbooks = []
    for playbook in phase.get("playbooks") or []:
        path = _resolve_pack_path(str(playbook), base_dir)
        playbooks.append({
            "source": str(playbook),
            "path": str(path),
            "exists": path.exists(),
        })
    return playbooks


def _env_requirements(data: dict[str, Any], env: dict[str, str]) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []

    def add(name: str, required: bool, detail: str, present: bool | None = None) -> None:
        requirements.append({
            "name": name,
            "required": required,
            "present": bool(env.get(name)) if present is None else present,
            "detail": detail,
        })

    if _proxmox_enabled(data):
        providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
        proxmox = providers.get("proxmox") if isinstance(providers.get("proxmox"), dict) else {}
        endpoint_key = proxmox.get("endpoint_env") or "PROXMOX_VE_ENDPOINT"
        token_key = proxmox.get("token_env") or "PROXMOX_VE_API_TOKEN"
        add(str(endpoint_key), True, "Proxmox API endpoint")
        has_token = bool(env.get(str(token_key)))
        has_user_password = bool(env.get("PROXMOX_VE_USERNAME") and env.get("PROXMOX_VE_PASSWORD"))
        add(str(token_key), not has_user_password, "Proxmox API token; alternatively set username/password", present=has_token)
        add("PROXMOX_VE_USERNAME", not has_token, "Proxmox API username when token auth is not used")
        add("PROXMOX_VE_PASSWORD", not has_token, "Proxmox API password when token auth is not used")
        for key, detail in (
            ("PROXMOX_NODE", "default Proxmox node; defaults to pve1"),
            ("PROXMOX_POOL", "default Proxmox pool; defaults to rangectl"),
            ("PROXMOX_STORAGE", "default VM storage"),
            ("PROXMOX_PFSENSE_STORAGE", "default pfSense/router storage"),
            ("PROXMOX_LXC_STORAGE", "default LXC storage"),
            ("PROXMOX_NETWORK_BRIDGE", "default VM bridge"),
            ("PROXMOX_NETWORK_SPAN_BRIDGE", "optional monitor/SPAN bridge"),
        ):
            add(key, False, detail)

    if any(host.get("owner") == "tart" for host in data.get("hosts") or []):
        for key in unresolved_tart_env(data, {}):
            add(key, True, "Tart host connection value")

    if _aws_enabled(data):
        providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
        aws = providers.get("aws") if isinstance(providers.get("aws"), dict) else {}
        add(str(aws.get("profile_env") or "AWS_PROFILE"), True, "AWS profile")
        add(str(aws.get("region_env") or "AWS_REGION"), True, "AWS region")
        add(str(aws.get("account_id_env") or "AWS_ACCOUNT_ID"), False, "expected AWS account guardrail")

    if _azure_enabled(data):
        add("AZURE_SUBSCRIPTION_ID", False, "Azure subscription id")

    return requirements


def _doctor_checks(path: Path, data: dict[str, Any], env: dict[str, str], online: bool) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    def add(status: str, name: str, detail: str = "") -> None:
        checks.append({"status": status, "name": name, "detail": detail})

    add(CHECK_OK, "range file", str(path))
    result = validate_range(data)
    for warning in result.warnings:
        add(CHECK_WARN, "range validation", warning)
    for error in result.errors:
        add(CHECK_ERROR, "range validation", error)
    if result.ok:
        add(CHECK_OK, "range validation")

    content = content_dirs()
    if content:
        add(CHECK_OK, "content packs", ", ".join(str(path) for path in content))
    else:
        add(CHECK_WARN, "content packs", "no external content pack directories found")

    state_dir = range_state_dir(data)
    add(CHECK_OK if state_dir.exists() else CHECK_WARN, "state directory", str(state_dir) if state_dir.exists() else f"not rendered yet: {state_dir}")

    phases = [phase for phase in data.get("phases") or [] if isinstance(phase, dict)]
    if phases:
        missing = []
        for phase in phases:
            missing.extend(item["path"] for item in _phase_playbooks(data, phase) if not item["exists"])
        if missing:
            add(CHECK_ERROR, "configure playbooks", ", ".join(missing))
        else:
            add(CHECK_OK, "configure playbooks", f"{len(phases)} phases")
        if not which("ansible-playbook"):
            add(CHECK_ERROR, "ansible-playbook", "not found in PATH")
        else:
            add(CHECK_OK, "ansible-playbook", which("ansible-playbook") or "")

    try:
        skipped_providers = _skip_provider_names(env=env)
    except ValueError as exc:
        add(CHECK_ERROR, "infra providers", str(exc))
        skipped_providers = set()
    stacks = _enabled_infra_stacks(data, skipped_providers)
    if stacks:
        add(CHECK_OK, "infra providers", ", ".join(stacks))
    else:
        add(CHECK_WARN, "infra providers", "none selected")
    if any(stack in {"proxmox", "aws", "azure"} for stack in stacks):
        tofu = which("tofu") or which("terraform")
        add(CHECK_OK if tofu else CHECK_ERROR, "tofu/terraform", tofu or "not found in PATH")
    if "aws" in stacks:
        add(CHECK_OK if which("aws") else CHECK_ERROR, "aws cli", which("aws") or "not found in PATH")

    missing_env = [item for item in _env_requirements(data, env) if item["required"] and not item["present"]]
    if missing_env:
        add(CHECK_ERROR, "environment", "missing: " + ", ".join(item["name"] for item in missing_env))
    else:
        add(CHECK_OK, "environment", str(RANGECTL_ENV_FILE))

    if _proxmox_enabled(data):
        needed = _needed_proxmox_vm_template_ids(data)
        lxc_needed = _needed_proxmox_lxc_templates(data)
        add(CHECK_OK if needed or lxc_needed else CHECK_WARN, "template catalog", f"vm={len(needed)} lxc={len(lxc_needed)}")

    if online and not result.errors:
        online_result = validate_range(data)
        if _proxmox_enabled(data):
            _populate_tf_vars(env)
            _online_proxmox_preflight(online_result, data, env)
        if any(host.get("owner") == "tart" for host in data.get("hosts") or []):
            _online_tart_preflight(online_result, data, env)
        if _aws_enabled(data):
            _online_aws_preflight(online_result, data, env)
        for warning in online_result.warnings:
            add(CHECK_WARN, "online validation", warning)
        for error in online_result.errors:
            add(CHECK_ERROR, "online validation", error)
        if not online_result.errors:
            add(CHECK_OK, "online validation")

    return checks


def _run_up_sequence(args: argparse.Namespace) -> int:
    phase = getattr(args, "phase", "all")
    phases = ["render", "infra", "configure"] if phase == "all" else [phase]
    auto_approve = args.auto_approve
    for phase in phases:
        if phase == "render":
            rc = cmd_render(args)
        elif phase == "infra":
            infra_args = argparse.Namespace(
                action="apply",
                range=args.range,
                auto_approve=auto_approve,
                allow_replace=args.allow_replace,
                hosts=None,
                execute=False,
                refresh=True,
                skip_provider=args.skip_provider,
            )
            rc = cmd_infra(infra_args)
        elif phase == "configure":
            configure_args = argparse.Namespace(
                range=args.range,
                phase=args.configure_phase,
                tags=args.tags,
                limit=args.limit,
                extra_vars=getattr(args, "extra_vars", None),
                dry_run=args.dry_run_configure,
            )
            rc = cmd_configure(configure_args)
        else:
            print(f"ERROR: unsupported up phase {phase}", file=sys.stderr)
            return 1
        if rc != 0:
            return rc
    return 0


def cmd_configure(args: argparse.Namespace) -> int:
    try:
        _, data = load_range(args.range)
    except RangeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    result = validate_range(data)
    if result.errors:
        for error in result.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    render_all(data)
    inventory_path = range_state_dir(data) / "inventory" / "hosts.yml"

    selected = []
    skipped_manual = []
    for phase in data.get("phases") or []:
        if args.phase and phase.get("name") != args.phase:
            continue
        if not args.phase and phase.get("manual"):
            skipped_manual.append(str(phase.get("name")))
            continue
        selected.append(phase)
    if args.phase and not selected:
        print(f"ERROR: unknown configure phase {args.phase}", file=sys.stderr)
        return 1

    commands: list[list[str]] = []
    missing: list[str] = []
    base_dir = range_base_dir(data)
    state_dir = range_state_dir(data).resolve()
    for phase in selected:
        for playbook in phase.get("playbooks") or []:
            playbook_path = _resolve_pack_path(str(playbook), base_dir)
            command = ["ansible-playbook", "-i", str(inventory_path), str(playbook_path)]
            command.extend(["--extra-vars", f"rangectl_root={ROOT}"])
            command.extend(["--extra-vars", f"rangectl_range_base={base_dir}"])
            command.extend(["--extra-vars", f"rangectl_state_dir={state_dir}"])
            command.extend(["--extra-vars", f"rangectl_ad_data_path={ad_compat_data_dir(data)}"])
            command.extend(["--extra-vars", f"rangectl_ad_scripts_path={base_dir / 'ad' / str((data.get('ansible') or {}).get('vars', {}).get('domain_name') or range_name(data)) / 'scripts'}"])
            command.extend(["--extra-vars", f"rangectl_ad_files_path={base_dir / 'ad' / str((data.get('ansible') or {}).get('vars', {}).get('domain_name') or range_name(data)) / 'files'}"])
            if args.tags:
                command.extend(["--tags", args.tags])
            if args.limit:
                command.extend(["--limit", args.limit])
            for extra_vars in getattr(args, "extra_vars", None) or []:
                command.extend(["--extra-vars", extra_vars])
            commands.append(command)
            if not playbook_path.exists():
                missing.append(str(playbook_path))

    if args.dry_run:
        for command in commands:
            print(" ".join(command))
        if skipped_manual:
            print("\nSkipped manual configure phases:")
            for phase_name in skipped_manual:
                print(f"  - {phase_name}")
            print("Run a manual phase explicitly with: ./rangectl configure RANGE --phase PHASE")
        if missing:
            print("\nMissing playbooks:")
            for path in missing:
                print(f"  - {path}")
        return 0

    if missing:
        print("ERROR: missing playbooks:", file=sys.stderr)
        for path in missing:
            print(f"  - {path}", file=sys.stderr)
        return 1

    for command in commands:
        print(" ".join(command))
        env = _rangectl_env()
        env.setdefault("ANSIBLE_CONFIG", str(ROOT / "ansible.cfg"))
        env["ANSIBLE_ROLES_PATH"] = _ansible_roles_path(env, base_dir)
        completed = subprocess.run(command, cwd=ROOT, env=env)
        if completed.returncode != 0:
            return completed.returncode
    _print_macos_elastic_approval_notice(data, selected, args.limit)
    if skipped_manual:
        print()
        print("Skipped manual configure phases:")
        for phase_name in skipped_manual:
            print(f"  - {phase_name}")
        print("Run a manual phase explicitly with: ./rangectl configure RANGE --phase PHASE")
    return 0


def _resolve_pack_path(value: str, base_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    pack_path = base_dir / path
    if pack_path.exists():
        return pack_path.resolve()
    return (ROOT / path).resolve()


def _ansible_roles_path(env: dict[str, str], base_dir: Path) -> str:
    candidates = [
        base_dir / "roles",
        ROOT / "roles",
    ]
    existing = [str(path.resolve()) for path in candidates if path.exists()]
    current = env.get("ANSIBLE_ROLES_PATH")
    if current:
        existing.extend([entry for entry in current.split(os.pathsep) if entry])
    return os.pathsep.join(dict.fromkeys(existing))


def _print_macos_elastic_approval_notice(data: dict[str, Any], phases: list[dict[str, Any]], host_filter: str | None) -> None:
    includes_macos_telemetry = _phases_include_named_phase(phases, "macos-telemetry")
    includes_jump_desktop = _phases_include_named_phase(phases, "jump-desktop")
    if not includes_macos_telemetry and not includes_jump_desktop:
        return

    hosts = [
        host
        for host in _select_hosts(data, host_filter)
        if _is_macos_host(host) and "macos_managed" in _host_ansible_groups(host)
    ]
    if not hosts:
        return

    print()
    print("================================================================================")
    print("ACTION REQUIRED: macOS admin approvals")
    print("================================================================================")
    if includes_macos_telemetry:
        print("Elastic Agent has been installed, but macOS requires local admin approval before")
        print("Elastic Endpoint is fully functional. On each macOS guest, sign in as admin and")
        print("approve the Elastic system extension / network filter prompts in System Settings.")
        print()
        print("Elastic:")
        print("  Open: System Settings -> Privacy & Security")
        print("  Approve: Elastic Endpoint / Elastic Agent system extension and network content filter")
        print("  Also verify: Full Disk Access for Elastic Agent / Elastic Endpoint if prompted")
    if includes_jump_desktop:
        print()
        print("Jump Desktop:")
        print("  Open: System Settings -> Privacy & Security")
        print("  Approve: Accessibility, Screen Recording, and Remote Desktop for Jump Desktop Connect")
        print("  If a profile was staged, approve it in System Settings -> Privacy & Security -> Profiles")
    print()
    print("Stop here until approvals are complete. Lab tools and user simulation are")
    print("manual phases and were not started by the default configure flow.")
    print()
    print("Affected hosts:")
    for host in hosts:
        print(f"  - {host.get('name')} ({host.get('ip', 'unknown-ip')})")
    print("================================================================================")


def _phases_include_named_phase(phases: list[dict[str, Any]], phase_name: str) -> bool:
    for phase in phases:
        if phase.get("name") == phase_name:
            return True
        for playbook in phase.get("playbooks") or []:
            if phase_name in str(playbook):
                return True
    return False


def _is_macos_host(host: dict[str, Any]) -> bool:
    os_name = str(host.get("os") or "").lower()
    host_type = str(host.get("type") or "").lower()
    return os_name.startswith("macos") or "macos" in host_type or "macos" in _host_ansible_groups(host)


def _host_ansible_groups(host: dict[str, Any]) -> set[str]:
    ansible = host.get("ansible") if isinstance(host.get("ansible"), dict) else {}
    groups = ansible.get("groups") if isinstance(ansible.get("groups"), list) else []
    return {str(group) for group in groups}


def cmd_infra(args: argparse.Namespace) -> int:
    try:
        _, data = load_range(args.range)
    except RangeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    result = validate_range(data)
    if result.errors:
        for error in result.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    render_all(data)
    env = _rangectl_env()
    try:
        skipped_providers = _skip_provider_names(args, env)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    stacks = _selected_infra_stacks(data, args.hosts, skipped_providers)
    if not stacks:
        print("No infrastructure providers selected")
        return 0
    if skipped_providers:
        print(f"Skipping providers: {', '.join(sorted(skipped_providers))}")
    if args.action in {"apply", "destroy"} and not args.auto_approve:
        print(f"ERROR: infra {args.action} requires --auto-approve", file=sys.stderr)
        return 1
    _populate_tf_vars(env)
    try:
        parallelism_args = _terraform_parallelism_args(env)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    binary = None
    for stack in stacks:
        if stack == "tart":
            rc = _run_tart_infra(data, env, args)
            if rc != 0:
                return rc
            continue

        if binary is None:
            binary = which("tofu") or which("terraform")
            if not binary:
                print("ERROR: neither tofu nor terraform is available in PATH", file=sys.stderr)
                return 1

        stack_dir = range_state_dir(data) / "render" / "tofu" / stack
        if not stack_dir.exists():
            print(f"ERROR: stack does not exist: {stack}", file=sys.stderr)
            return 1
        fmt = [binary, "fmt"]
        print(f"[{stack}] {' '.join(fmt)}", flush=True)
        completed = subprocess.run(fmt, cwd=stack_dir, env=env)
        if completed.returncode != 0:
            return completed.returncode

        init = [binary, "init", "-input=false"]
        print(f"[{stack}] {' '.join(init)}", flush=True)
        completed = subprocess.run(init, cwd=stack_dir, env=env)
        if completed.returncode != 0:
            return completed.returncode

        if args.action == "init":
            continue

        if args.action == "adopt":
            if stack != "proxmox":
                print(f"[{stack}] adopt is not implemented for this provider")
                continue
            rc = _terraform_import_existing(data, stack_dir, binary, env, args)
            if rc != 0:
                return rc
            continue

        target_args = _terraform_target_args(data, stack, args.hosts)

        if args.action == "apply":
            rc = _guarded_terraform_apply(stack, stack_dir, binary, env, args, target_args, parallelism_args)
            if rc != 0:
                return rc
            continue

        command = [binary, args.action, *target_args]
        if args.action in {"plan", "destroy"}:
            command.extend(parallelism_args)
        if args.action in {"plan", "destroy"}:
            command.append("-input=false")
        if args.action in {"plan", "apply", "destroy"} and not getattr(args, "refresh", True):
            command.append("-refresh=false")
        if args.action in {"apply", "destroy"} and args.auto_approve:
            command.append("-auto-approve")
        if args.action in {"apply", "destroy"} and not args.auto_approve:
            print(f"ERROR: infra {args.action} requires --auto-approve", file=sys.stderr)
            return 1
        print(f"[{stack}] {' '.join(command)}", flush=True)
        completed = subprocess.run(command, cwd=stack_dir, env=env)
        if completed.returncode != 0:
            return completed.returncode
    return 0


def _guarded_terraform_apply(
    stack: str,
    stack_dir: Path,
    binary: str,
    env: dict[str, str],
    args: argparse.Namespace,
    target_args: list[str] | None = None,
    parallelism_args: list[str] | None = None,
) -> int:
    target_args = target_args or []
    parallelism_args = parallelism_args or []
    plan_path = stack_dir / ".rangectl-apply.tfplan"
    plan = [binary, "plan", *target_args, *parallelism_args, "-input=false", "-out", str(plan_path), "-detailed-exitcode"]
    if not getattr(args, "refresh", True):
        plan.append("-refresh=false")
    print(f"[{stack}] {' '.join(plan)}", flush=True)
    completed = subprocess.run(plan, cwd=stack_dir, env=env)
    if completed.returncode == 1:
        return 1

    destructive = _terraform_plan_destructive_changes(binary, stack_dir, plan_path, env)
    if destructive and not args.allow_replace:
        print("ERROR: apply plan contains destroy/replace actions:", file=sys.stderr)
        for item in destructive:
            print(f"  - {item}", file=sys.stderr)
        print("Refusing apply. Reconcile/import config drift or pass --allow-replace intentionally.", file=sys.stderr)
        return 1

    apply = [binary, "apply", *parallelism_args, "-auto-approve", str(plan_path)]
    print(f"[{stack}] {' '.join(apply)}", flush=True)
    completed = subprocess.run(apply, cwd=stack_dir, env=env)
    return completed.returncode


def _terraform_parallelism_args(env: dict[str, str]) -> list[str]:
    value = env.get("RANGECTL_TERRAFORM_PARALLELISM") or env.get("PROXMOX_PARALLELISM")
    if not value:
        return []
    try:
        parallelism = int(value)
    except ValueError as exc:
        raise ValueError("RANGECTL_TERRAFORM_PARALLELISM must be a positive integer") from exc
    if parallelism < 1:
        raise ValueError("RANGECTL_TERRAFORM_PARALLELISM must be a positive integer")
    return [f"-parallelism={parallelism}"]


def _terraform_plan_destructive_changes(binary: str, stack_dir: Path, plan_path: Path, env: dict[str, str]) -> list[str]:
    show = subprocess.run(
        [binary, "show", "-json", str(plan_path)],
        cwd=stack_dir,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if show.returncode != 0:
        return ["unable to inspect Terraform plan JSON"]
    parsed = json.loads(show.stdout)
    destructive = []
    for change in parsed.get("resource_changes") or []:
        actions = change.get("change", {}).get("actions") or []
        if "delete" in actions:
            destructive.append(f"{change.get('address')} actions={actions}")
    return destructive


def _terraform_target_args(data: dict[str, Any], stack: str, host_filter: str | None) -> list[str]:
    if not host_filter:
        return []
    targets = []
    selected_hosts = _select_hosts(data, host_filter)
    if stack == "proxmox":
        for host in selected_hosts:
            if host.get("owner") == "tofu:proxmox":
                targets.extend(["-target", _terraform_address_for_proxmox_host(host)])
    elif stack == "aws" and _aws_power_targets(data, host_filter):
        targets.extend(["-target", "aws_instance.app"])
    return targets


def _terraform_import_existing(
    data: dict[str, Any],
    stack_dir: Path,
    binary: str,
    env: dict[str, str],
    args: argparse.Namespace,
) -> int:
    try:
        client = ProxmoxClient.from_env(env)
        targets, missing = _resolve_proxmox_targets(client, data, args.hosts, allow_missing=True)
    except ProxmoxError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    host_by_name = {host["name"]: host for host in data.get("hosts") or [] if host.get("owner") == "tofu:proxmox"}
    for name in missing:
        print(f"skip missing {name}")
    for target in targets:
        host = host_by_name.get(target["name"])
        if not host:
            continue
        address = _terraform_address_for_proxmox_host(host)
        import_id = f"{target['node']}/{target['vmid']}"
        command = [binary, "import", address, import_id]
        if not args.execute:
            print(" ".join(command))
            continue
        print(" ".join(command), flush=True)
        completed = subprocess.run(command, cwd=stack_dir, env=env)
        if completed.returncode != 0:
            return completed.returncode
    if not args.execute:
        print("Use --execute to import existing Proxmox resources into Terraform state.")
    return 0


def _terraform_address_for_proxmox_host(host: dict[str, Any]) -> str:
    name = str(host.get("name"))
    host_type = host.get("type")
    if host_type == "lxc":
        return f'proxmox_virtual_environment_container.lxc["{name}"]'
    if host_type == "router":
        return f'proxmox_virtual_environment_vm.router["{name}"]'
    return f'proxmox_virtual_environment_vm.vm["{name}"]'


def _run_tart_infra(data: dict[str, Any], env: dict[str, str], args: argparse.Namespace) -> int:
    hosts = _select_hosts(data, args.hosts, owner="tart")
    if not hosts:
        print("[tart] no Tart hosts selected")
        return 0

    if args.action == "init":
        print("[tart] no init required")
        return 0
    if args.action == "adopt":
        print("[tart] adopt is not required; guests are matched by configured name")
        return 0
    if args.action == "plan":
        for host in hosts:
            source = str(host.get("source"))
            if "/" in source or ":" in source:
                print(f"[tart] {' '.join(tart_command_for_host(data, host, 'pull', env))}")
            print(f"[tart] {' '.join(tart_command_for_host(data, host, 'clone', env))}  # if missing")
            print(f"[tart] {' '.join(tart_command_for_host(data, host, 'set', env))}")
            print(f"[tart] {' '.join(tart_command_for_host(data, host, 'start', env))}")
        return 0
    if args.action not in {"apply", "destroy"}:
        print(f"ERROR: unsupported Tart infra action {args.action}", file=sys.stderr)
        return 1

    missing = unresolved_tart_env(data, env)
    if missing:
        print("ERROR: missing Tart provider environment values:", file=sys.stderr)
        for value in missing:
            print(f"  - {value}", file=sys.stderr)
        print(f"Set them in {_rangectl_env_hint()}.", file=sys.stderr)
        return 1

    tart_hosts = tart_hosts_from_range(data, env)
    statuses = []
    try:
        for host in hosts:
            tart_host_name = str(host.get("tart_host", host.get("provider_host")))
            runner = TartRunner(tart_hosts[tart_host_name], env)
            if args.action == "apply":
                apply_guest(runner, host)
                start_guest(runner, host)
                statuses.append(guest_status(runner, host, wait=30))
                continue
            stop_guest(runner, host)
            name = str(host["name"])
            if vm_entry(runner, name) is not None:
                runner.run(["delete", name])
    except (KeyError, TartError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if statuses:
        status_path = update_tart_runtime_state(data, statuses)
        for status in statuses:
            print(
                f"[tart] {status['name']}\t{status['state']}\trunning={str(status['running']).lower()}"
                f"\tdesired={status.get('desired_ip') or '-'}\tdiscovered={status.get('discovered_ip') or '-'}"
                f"\tmac={status.get('mac_address') or '-'}"
            )
            if status.get("ip_error"):
                print(f"  ip: {status['ip_error']}")
        print(f"[tart] State: {status_path}")
    elif args.action == "destroy":
        for host in hosts:
            print(f"[tart] destroyed {host['name']}")
    return 0


def cmd_power(args: argparse.Namespace) -> int:
    try:
        _, data = load_range(args.range)
    except RangeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    selected_hosts = _select_hosts(data, args.hosts)
    proxmox_hosts = [host for host in selected_hosts if host.get("owner") == "tofu:proxmox"]
    tart_hosts = [host for host in selected_hosts if host.get("owner") == "tart"]
    aws_targets = _aws_power_targets(data, args.hosts)
    unsupported = []
    if args.hosts:
        unsupported = [
            host for host in selected_hosts
            if host.get("owner") not in {"tofu:proxmox", "tart"}
        ]

    if unsupported:
        for host in unsupported:
            print(f"{host.get('name')}: power is not implemented for owner {host.get('owner')}", file=sys.stderr)
        return 1

    if proxmox_hosts:
        rc = _run_proxmox_power(data, args, allow_missing=args.action == "status")
        if rc != 0:
            return rc
    if tart_hosts:
        env = _rangectl_env()
        rc = _run_tart_power(data, env, args, tart_hosts)
        if rc != 0:
            return rc
    if aws_targets:
        rc = _run_aws_power(data, args, aws_targets)
        if rc != 0:
            return rc
    if not proxmox_hosts and not tart_hosts and not aws_targets:
        print("No power-manageable hosts selected")
    return 0


def _run_proxmox_power(data: dict[str, Any], args: argparse.Namespace, allow_missing: bool) -> int:
    env = _rangectl_env()
    _populate_tf_vars(env)
    try:
        client = ProxmoxClient.from_env(env)
        targets, missing = _resolve_proxmox_targets(client, data, args.hosts, allow_missing=allow_missing)
    except ProxmoxError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.action == "status":
        for target in targets:
            try:
                status = client.get(f"nodes/{target['node']}/{target['kind']}/{target['vmid']}/status/current") or {}
                print(f"{target['name']}\tproxmox\t{target['kind']}/{target['vmid']}\t{status.get('status', 'unknown')}")
            except ProxmoxError as exc:
                print(f"ERROR: {target['name']}: {exc}", file=sys.stderr)
                return 1
        for name in missing:
            print(f"{name}\tproxmox\tmissing")
        return 0

    endpoint_action = "stop" if args.action == "off" and args.force else {
        "on": "start",
        "off": "shutdown",
        "reboot": "reboot",
    }[args.action]
    for target in targets:
        path = f"nodes/{target['node']}/{target['kind']}/{target['vmid']}/status/{endpoint_action}"
        if not args.execute:
            print(f"DRY-RUN {target['name']}: POST {path}")
            continue
        print(f"{target['name']}: POST {path}", flush=True)
        client.post(path)
    if not args.execute:
        print("Use --execute to run power changes.")
    return 0


def _run_tart_power(data: dict[str, Any], env: dict[str, str], args: argparse.Namespace, hosts: list[dict[str, Any]]) -> int:
    if args.execute or args.action == "status":
        missing = unresolved_tart_env(data, env)
        if missing:
            print("ERROR: missing Tart provider environment values:", file=sys.stderr)
            for value in missing:
                print(f"  - {value}", file=sys.stderr)
            print(f"Set them in {_rangectl_env_hint()}.", file=sys.stderr)
            return 1
    try:
        tart_hosts = tart_hosts_from_range(data, env)
        statuses = []
        for host in hosts:
            tart_host_name = str(host.get("tart_host", host.get("provider_host")))
            runner = TartRunner(tart_hosts[tart_host_name], env)
            if args.action == "status":
                statuses.append(guest_status(runner, host))
                continue
            if not args.execute:
                actions = ["stop", "start"] if args.action == "reboot" else [{"on": "start", "off": "stop"}[args.action]]
                for action in actions:
                    print(f"DRY-RUN {host['name']}: {' '.join(tart_command_for_host(data, host, action, env))}")
                continue
            if args.action == "on":
                start_guest(runner, host)
            elif args.action == "off":
                stop_guest(runner, host)
            elif args.action == "reboot":
                stop_guest(runner, host)
                start_guest(runner, host)
            statuses.append(guest_status(runner, host))
    except (KeyError, TartError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if statuses:
        status_path = update_tart_runtime_state(data, statuses)
        for status in statuses:
            print(
                f"{status['name']}\ttart\t{status['state']}\trunning={str(status['running']).lower()}"
                f"\tdesired={status.get('desired_ip') or '-'}\tdiscovered={status.get('discovered_ip') or '-'}"
            )
        print(f"State: {status_path}")
    if args.action != "status" and not args.execute:
        print("Use --execute to run power changes.")
    return 0


def _aws_power_targets(data: dict[str, Any], host_filter: str | None) -> list[str]:
    cloud = data.get("cloud") or {}
    aws = cloud.get("aws") if isinstance(cloud, dict) else {}
    if not isinstance(aws, dict):
        return []
    instance = aws.get("instance") if isinstance(aws.get("instance"), dict) else {}
    name = instance.get("name") or f"{range_name(data)}-{aws.get('scenario', 'scenario')}-app01"
    name = str(name)
    if not _aws_enabled(data):
        return []
    if not host_filter:
        return [name]
    wanted = {item.strip() for item in host_filter.split(",") if item.strip()}
    return [name] if name in wanted or "aws" in wanted else []


def _run_aws_power(data: dict[str, Any], args: argparse.Namespace, names: list[str]) -> int:
    if not which("aws"):
        print("ERROR: AWS power commands require the aws CLI in PATH", file=sys.stderr)
        return 1
    env = _aws_env(data)
    instances = _aws_describe_instances(env, names)
    if instances is None:
        return 1
    by_name = {item["name"]: item for item in instances}
    for name in names:
        item = by_name.get(name)
        if args.action == "status":
            if not item:
                print(f"{name}\taws\tmissing")
            else:
                print(f"{name}\taws\t{item['instance_id']}\t{item['state']}")
            continue
        if not item:
            print(f"ERROR: AWS instance not found by Name tag: {name}", file=sys.stderr)
            return 1
        command = {
            "on": "start-instances",
            "off": "stop-instances",
            "reboot": "reboot-instances",
        }[args.action]
        cli = ["aws", "ec2", command, "--instance-ids", item["instance_id"]]
        if not args.execute:
            print(f"DRY-RUN {name}: {' '.join(cli)}")
            continue
        print(f"{name}: {' '.join(cli)}", flush=True)
        completed = subprocess.run(cli, env=env)
        if completed.returncode != 0:
            return completed.returncode
    if args.action != "status" and not args.execute:
        print("Use --execute to run power changes.")
    return 0


def _aws_env(data: dict[str, Any]) -> dict[str, str]:
    env = _rangectl_env()
    providers = data.get("providers") or {}
    aws_provider = providers.get("aws") if isinstance(providers, dict) else {}
    if isinstance(aws_provider, dict):
        region_key = aws_provider.get("region_env") or "AWS_REGION"
        if region_key not in env and aws_provider.get("region"):
            env[str(region_key)] = str(aws_provider["region"])
        if "AWS_DEFAULT_REGION" not in env and region_key in env:
            env["AWS_DEFAULT_REGION"] = env[str(region_key)]
    return env


def _aws_describe_instances(env: dict[str, str], names: list[str]) -> list[dict[str, str]] | None:
    filters = [
        "Name=tag:Name,Values=" + ",".join(names),
        "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down,terminated",
    ]
    command = ["aws", "ec2", "describe-instances", "--filters", *filters, "--output", "json"]
    completed = subprocess.run(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        print(f"ERROR: AWS describe-instances failed: {(completed.stderr or completed.stdout).strip()}", file=sys.stderr)
        return None
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        print(f"ERROR: AWS describe-instances returned invalid JSON: {exc}", file=sys.stderr)
        return None
    instances = []
    for reservation in parsed.get("Reservations") or []:
        for instance in reservation.get("Instances") or []:
            tags = {tag.get("Key"): tag.get("Value") for tag in instance.get("Tags") or []}
            name = str(tags.get("Name") or instance.get("InstanceId"))
            instances.append({
                "name": name,
                "instance_id": str(instance.get("InstanceId")),
                "state": str((instance.get("State") or {}).get("Name") or "unknown"),
            })
    return instances


def _aws_describe_images(env: dict[str, str], source_name: str) -> list[str] | None:
    command = [
        "aws", "ec2", "describe-images",
        "--owners", "self",
        "--filters", f"Name=tag:SourceHost,Values={source_name}",
        "--output", "json",
    ]
    completed = subprocess.run(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        print(f"ERROR: AWS describe-images failed: {(completed.stderr or completed.stdout).strip()}", file=sys.stderr)
        return None
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        print(f"ERROR: AWS describe-images returned invalid JSON: {exc}", file=sys.stderr)
        return None
    names = []
    for image in parsed.get("Images") or []:
        tags = {tag.get("Key"): tag.get("Value") for tag in image.get("Tags") or []}
        names.append(str(tags.get("Name") or image.get("Name") or image.get("ImageId")))
    return sorted(names)


def _aws_image_ids(env: dict[str, str], image_name: str) -> list[str] | None:
    command = [
        "aws", "ec2", "describe-images",
        "--owners", "self",
        "--filters", f"Name=tag:Name,Values={image_name}",
        "--output", "json",
    ]
    completed = subprocess.run(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        print(f"ERROR: AWS describe-images failed: {(completed.stderr or completed.stdout).strip()}", file=sys.stderr)
        return None
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        print(f"ERROR: AWS describe-images returned invalid JSON: {exc}", file=sys.stderr)
        return None
    return [str(image.get("ImageId")) for image in parsed.get("Images") or [] if image.get("ImageId")]


def cmd_snapshot(args: argparse.Namespace) -> int:
    try:
        _, data = load_range(args.range)
    except RangeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    selected_hosts = _select_hosts(data, args.hosts)
    proxmox_hosts = [host for host in selected_hosts if host.get("owner") == "tofu:proxmox"]
    tart_hosts = [host for host in selected_hosts if host.get("owner") == "tart"]
    aws_targets = _aws_power_targets(data, args.hosts)
    unsupported = []
    if args.hosts:
        unsupported = [
            host for host in selected_hosts
            if host.get("owner") not in {"tofu:proxmox", "tart"}
        ]
    if unsupported:
        for host in unsupported:
            print(f"{host.get('name')}: snapshots are not implemented for owner {host.get('owner')}", file=sys.stderr)
        return 1
    if args.snapshot_command in {"create", "revert", "delete"} and not args.execute:
        pass

    if proxmox_hosts:
        rc = _run_proxmox_snapshot(data, args)
        if rc != 0:
            return rc
    if tart_hosts:
        env = _rangectl_env()
        rc = _run_tart_snapshot(data, env, args, tart_hosts)
        if rc != 0:
            return rc
    if aws_targets:
        rc = _run_aws_snapshot(data, args, aws_targets)
        if rc != 0:
            return rc
    if not proxmox_hosts and not tart_hosts and not aws_targets:
        print("No snapshot-manageable hosts selected")
    return 0


def _run_proxmox_snapshot(data: dict[str, Any], args: argparse.Namespace) -> int:
    env = _rangectl_env()
    _populate_tf_vars(env)
    try:
        client = ProxmoxClient.from_env(env)
        targets, _ = _resolve_proxmox_targets(client, data, args.hosts)
    except ProxmoxError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.snapshot_command == "list":
        for target in targets:
            snapshots = client.get(f"nodes/{target['node']}/{target['kind']}/{target['vmid']}/snapshot") or []
            names = ", ".join(sorted(str(item.get("name")) for item in snapshots if item.get("name")))
            print(f"{target['name']}\tproxmox\t{names}")
        return 0

    if args.snapshot_command == "create":
        payload = {"snapname": args.name}
        if args.description:
            payload["description"] = args.description
        api_path_suffix = "snapshot"
    elif args.snapshot_command == "revert":
        payload = {}
        api_path_suffix = f"snapshot/{args.name}/rollback"
    elif args.snapshot_command == "delete":
        payload = {}
        api_path_suffix = f"snapshot/{args.name}"
    else:
        print(f"ERROR: unsupported snapshot command {args.snapshot_command}", file=sys.stderr)
        return 1

    method = "DELETE" if args.snapshot_command == "delete" else "POST"
    for target in targets:
        path = f"nodes/{target['node']}/{target['kind']}/{target['vmid']}/{api_path_suffix}"
        if not args.execute:
            print(f"DRY-RUN {target['name']}: {method} {path}")
            continue
        print(f"{target['name']}: {method} {path}", flush=True)
        if method == "DELETE":
            client._request("DELETE", path)
        else:
            client.post(path, payload)
    if not args.execute:
        print("Use --execute to run snapshot changes.")
    return 0


def _run_tart_snapshot(data: dict[str, Any], env: dict[str, str], args: argparse.Namespace, hosts: list[dict[str, Any]]) -> int:
    missing = unresolved_tart_env(data, env)
    if missing:
        print("ERROR: missing Tart provider environment values:", file=sys.stderr)
        for value in missing:
            print(f"  - {value}", file=sys.stderr)
        print(f"Set them in {_rangectl_env_hint()}.", file=sys.stderr)
        return 1
    try:
        tart_hosts = tart_hosts_from_range(data, env)
        for host in hosts:
            tart_host_name = str(host.get("tart_host", host.get("provider_host")))
            runner = TartRunner(tart_hosts[tart_host_name], env)
            snapshot_name = f"{host['name']}-{args.name}" if args.snapshot_command != "list" else None
            if args.snapshot_command == "list":
                prefix = f"{host['name']}-"
                names = ", ".join(sorted(item.get("Name") for item in list_vms(runner) if str(item.get("Name", "")).startswith(prefix)))
                print(f"{host['name']}\ttart\t{names}")
                continue
            if args.snapshot_command == "create":
                command = runner.command(["clone", str(host["name"]), snapshot_name])
                if not args.execute:
                    print(f"DRY-RUN {host['name']}: {' '.join(command)}")
                    continue
                print(f"{host['name']}: {' '.join(command)}", flush=True)
                runner.run(["clone", str(host["name"]), snapshot_name])
                continue
            if args.snapshot_command == "delete":
                command = runner.command(["delete", snapshot_name])
                if not args.execute:
                    print(f"DRY-RUN {host['name']}: {' '.join(command)}")
                    continue
                print(f"{host['name']}: {' '.join(command)}", flush=True)
                runner.run(["delete", snapshot_name])
                continue
            if args.snapshot_command == "revert":
                if not args.execute:
                    print(f"DRY-RUN {host['name']}: stop guest, delete current guest if present, clone {snapshot_name} to {host['name']}")
                    continue
                stop_guest(runner, host)
                if vm_entry(runner, str(host["name"])) is not None:
                    runner.run(["delete", str(host["name"])])
                runner.run(["clone", snapshot_name, str(host["name"])])
                apply_guest(runner, host)
    except (KeyError, TartError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.snapshot_command != "list" and not args.execute:
        print("Use --execute to run snapshot changes.")
    return 0


def _run_aws_snapshot(data: dict[str, Any], args: argparse.Namespace, names: list[str]) -> int:
    if args.snapshot_command == "revert":
        print("ERROR: snapshot revert is not implemented for AWS-managed compute", file=sys.stderr)
        return 1
    if not which("aws"):
        print("ERROR: AWS snapshot commands require the aws CLI in PATH", file=sys.stderr)
        return 1
    env = _aws_env(data)
    instances = _aws_describe_instances(env, names)
    if instances is None:
        return 1
    by_name = {item["name"]: item for item in instances}
    for name in names:
        item = by_name.get(name)
        if not item:
            if args.snapshot_command == "list":
                print(f"{name}\taws\tmissing")
                continue
            print(f"ERROR: AWS instance not found by Name tag: {name}", file=sys.stderr)
            return 1
        image_name = f"rangectl-{range_name(data)}-{name}-{args.name}" if args.snapshot_command != "list" else None
        if args.snapshot_command == "list":
            images = _aws_describe_images(env, name)
            if images is None:
                return 1
            print(f"{name}\taws\t{', '.join(images)}")
            continue
        if args.snapshot_command == "create":
            cli = [
                "aws", "ec2", "create-image",
                "--instance-id", item["instance_id"],
                "--name", image_name,
                "--tag-specifications",
                f"ResourceType=image,Tags=[{{Key=Name,Value={image_name}}},{{Key=Range,Value={range_name(data)}}},{{Key=SourceHost,Value={name}}}]",
            ]
            if not args.execute:
                print(f"DRY-RUN {name}: {' '.join(cli)}")
                continue
            print(f"{name}: {' '.join(cli)}", flush=True)
            completed = subprocess.run(cli, env=env)
            if completed.returncode != 0:
                return completed.returncode
            continue
        if args.snapshot_command == "delete":
            image_ids = _aws_image_ids(env, image_name)
            if image_ids is None:
                return 1
            if not image_ids:
                print(f"{name}\taws\tmissing snapshot {args.name}")
                continue
            for image_id in image_ids:
                cli = ["aws", "ec2", "deregister-image", "--image-id", image_id]
                if not args.execute:
                    print(f"DRY-RUN {name}: {' '.join(cli)}")
                    continue
                print(f"{name}: {' '.join(cli)}", flush=True)
                completed = subprocess.run(cli, env=env)
                if completed.returncode != 0:
                    return completed.returncode
    if args.snapshot_command != "list" and not args.execute:
        print("Use --execute to run snapshot changes.")
    return 0


def _select_hosts(data: dict[str, Any], host_filter: str | None, owner: str | None = None) -> list[dict[str, Any]]:
    wanted = {item.strip() for item in host_filter.split(",") if item.strip()} if host_filter else None
    selected = []
    for host in data.get("hosts") or []:
        if owner and host.get("owner") != owner:
            continue
        if wanted and host.get("name") not in wanted:
            continue
        selected.append(host)
    return selected


def _resolve_proxmox_targets(
    client: ProxmoxClient,
    data: dict[str, Any],
    host_filter: str | None,
    allow_missing: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    selected_hosts = _select_hosts(data, host_filter, owner="tofu:proxmox")
    selected_names = {host["name"] for host in selected_hosts}
    host_by_name = {host["name"]: host for host in selected_hosts}
    resources = client.get("cluster/resources?type=vm") or []
    by_name: dict[str, list[dict[str, Any]]] = {}
    proxmox_name_to_range_name = {
        _proxmox_resource_name(host): str(host["name"])
        for host in selected_hosts
    }
    for item in resources:
        range_name_for_item = proxmox_name_to_range_name.get(str(item.get("name")))
        if range_name_for_item:
            by_name.setdefault(range_name_for_item, []).append(item)
    targets = []
    missing = []
    for name in sorted(selected_names):
        candidates = by_name.get(name) or []
        resource = _choose_proxmox_resource_for_host(client, host_by_name[name], candidates)
        if not resource:
            missing.append(name)
            continue
        resource_type = resource.get("type")
        kind = "lxc" if resource_type == "lxc" else "qemu"
        targets.append({
            "name": name,
            "node": resource.get("node"),
            "vmid": resource.get("vmid"),
            "kind": kind,
        })
    if missing and not allow_missing:
        raise ProxmoxError(f"Proxmox resources not found by name: {', '.join(missing)}")
    return targets, missing


def _choose_proxmox_resource_for_host(
    client: ProxmoxClient,
    host: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    host_ip = str(host.get("ip"))
    for candidate in candidates:
        resource_type = candidate.get("type")
        kind = "lxc" if resource_type == "lxc" else "qemu"
        try:
            config = client.get(f"nodes/{candidate['node']}/{kind}/{candidate['vmid']}/config") or {}
        except ProxmoxError:
            continue
        if any(host_ip in str(value) for value in config.values()):
            return candidate
    return candidates[0]


def _proxmox_resource_name(host: dict[str, Any]) -> str:
    proxmox = host.get("proxmox") or {}
    if isinstance(proxmox, dict) and proxmox.get("name"):
        return str(proxmox["name"])
    return str(host.get("proxmox_name") or host.get("name"))


def _populate_tf_vars(env: dict[str, str]) -> None:
    mappings = {
        "PROXMOX_NODE": "TF_VAR_pm_node",
        "PROXMOX_POOL": "TF_VAR_pm_pool",
        "PROXMOX_STORAGE": "TF_VAR_storage",
        "PROXMOX_PFSENSE_STORAGE": "TF_VAR_pfsense_storage",
        "PROXMOX_LXC_STORAGE": "TF_VAR_lxc_storage",
        "PROXMOX_FULL_CLONE": "TF_VAR_pm_full_clone",
        "PROXMOX_VE_INSECURE": "TF_VAR_pm_insecure",
        "PROXMOX_NETWORK_BRIDGE": "TF_VAR_network_bridge",
        "PROXMOX_NETWORK_SPAN_BRIDGE": "TF_VAR_network_span_bridge",
    }
    defaults = {
        "TF_VAR_pm_node": "pve1",
        "TF_VAR_pm_pool": "rangectl",
        "TF_VAR_storage": "local-lvm",
        "TF_VAR_pfsense_storage": "local-lvm",
        "TF_VAR_lxc_storage": "local-lvm",
    }
    for source, target in mappings.items():
        if target not in env and source in env:
            env[target] = env[source]
    for target, value in defaults.items():
        env.setdefault(target, value)


def _ssh_transport(env: dict[str, str], key: str | None = None) -> tuple[list[str], list[str], str]:
    insecure = env.get("RANGECTL_SSH_INSECURE", "").strip().lower() in {"1", "true", "yes", "on"}
    options: list[str] = []
    if insecure:
        options = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    elif known_hosts := env.get("RANGECTL_SSH_KNOWN_HOSTS"):
        options = ["-o", f"UserKnownHostsFile={known_hosts}"]
    ssh = ["ssh", *options]
    scp = ["scp", *options]
    rsync_parts = ["ssh", *[shlex.quote(item) for item in options]]
    if key:
        ssh.extend(["-i", key])
        scp.extend(["-i", key])
        rsync_parts.extend(["-i", shlex.quote(key)])
    return ssh, scp, " ".join(rsync_parts)


def cmd_templates(args: argparse.Namespace) -> int:
    if args.templates_command == "list":
        catalog = proxmox_template_catalog(None)
        if args.json:
            print(json.dumps(catalog, indent=2, sort_keys=True))
            return 0
        print("Proxmox aliases:")
        for alias, target in sorted(catalog["aliases"].items()):
            print(f"  {alias}: {target}")
        print("Proxmox VM templates:")
        for name, vmid in sorted(catalog["vm_templates"].items()):
            print(f"  {name}: {vmid}")
        print("Proxmox LXC templates:")
        for name, file_id in sorted(catalog["lxc_templates"].items()):
            print(f"  {name}: {file_id}")
        return 0
    if args.templates_command == "check":
        loaded = _load_range_or_error(args.range)
        if loaded is None:
            return 2
        _, data = loaded
        catalog = proxmox_template_catalog(data)
        aliases = catalog["aliases"]
        missing = []
        used = []
        for host in data.get("hosts") or []:
            if host.get("owner") != "tofu:proxmox":
                continue
            raw_template = str(host.get("template"))
            template = aliases.get(raw_template, raw_template)
            if host.get("type") == "lxc":
                found = template in catalog["lxc_templates"] or raw_template in catalog["lxc_templates"]
            else:
                found = template in catalog["vm_templates"]
            item = {
                "host": str(host.get("name")),
                "template": raw_template,
                "resolved": template,
                "found": found,
            }
            used.append(item)
            if not found:
                missing.append(item)
        if args.json:
            print(json.dumps({"range": range_name(data), "templates": used}, indent=2, sort_keys=True))
            return 1 if missing else 0
        print(f"Range: {range_name(data)}")
        if not used:
            print("No Proxmox templates referenced by this range.")
            return 0
        for item in used:
            status = CHECK_OK if item["found"] else CHECK_ERROR
            print(f"{status}\t{item['host']}\t{item['template']} -> {item['resolved']}")
        return 1 if missing else 0
    if args.templates_command == "inspect-url":
        try:
            inspection = inspect_template_source_url(args.url, timeout=args.timeout)
        except Exception as exc:
            print(f"Could not inspect template source URL: {exc}", file=sys.stderr)
            return 1
        if args.format == "json":
            print(json.dumps(inspection.as_dict(), indent=2, sort_keys=True))
        else:
            import yaml

            print(yaml.safe_dump(inspection.as_dict(), sort_keys=False).rstrip())
        return 0
    if args.templates_command == "doctor":
        env = _rangectl_env()
        status = template_build_tool_status(env)
        print(json.dumps(status, indent=2, sort_keys=True))
        missing = []
        if not status["packer"]:
            missing.append("packer")
        if not status["xorriso"] and not status["genisoimage"] and not status["mkisofs"]:
            missing.append("xorriso or genisoimage/mkisofs")
        if args.provider == "proxmox":
            for key in ("PROXMOX_VE_ENDPOINT", "PROXMOX_VE_USERNAME", "PROXMOX_VE_PASSWORD"):
                if not env.get(key):
                    missing.append(key)
        if missing:
            print("Missing template build prerequisites: " + ", ".join(missing), file=sys.stderr)
            return 1
        return 0
    if args.templates_command == "build":
        env = _rangectl_env()
        definition_path = Path(args.definition)
        try:
            definition = load_template_build_definition(definition_path)
            workdir = ROOT / "state" / "templates" / args.provider / args.name
            rendered = render_packer_windows_proxmox_build(definition, args.name, env, workdir)
        except Exception as exc:
            print(f"Could not render template build: {exc}", file=sys.stderr)
            return 1

        print(f"Rendered Packer build in {rendered['workdir']}")
        packer_bin = env.get("RANGECTL_PACKER_BIN", "packer")
        validate_rc = run_packer_validate(Path(rendered["workdir"]), packer_bin=packer_bin)
        if validate_rc != 0:
            return validate_rc
        if args.dry_run:
            return 0
        if args.replace_existing:
            try:
                _delete_proxmox_vm_if_present(int(rendered["vmid"]), env)
            except Exception as exc:
                print(f"Could not replace existing VMID {rendered['vmid']}: {exc}", file=sys.stderr)
                return 1
        build = (definition.get("builds") or {}).get(args.name) or {}
        runner = args.runner or build.get("runner") or (definition.get("defaults") or {}).get("runner") or "local"
        if runner == "proxmox-node":
            rc = _run_packer_build_on_proxmox_node(Path(rendered["workdir"]), definition, args.name, env, rendered)
        else:
            rc = _stage_autounattend_iso_on_proxmox(Path(rendered["workdir"]), definition, args.name, env, rendered)
            if rc != 0:
                return rc
            rc = run_packer_build(Path(rendered["workdir"]), packer_bin=packer_bin)
        if rc != 0:
            return rc
        try:
            _set_proxmox_vm_boot_order(int(rendered["vmid"]), str(rendered.get("final_boot_order") or "sata0;net0"), env)
            _set_proxmox_vm_disk_cache(
                int(rendered["vmid"]),
                str(rendered.get("final_disk_key") or ""),
                str(rendered.get("final_disk_cache") or ""),
                env,
            )
        except Exception as exc:
            print(f"Template build finished but final Proxmox config reset failed: {exc}", file=sys.stderr)
            return 1
        try:
            _verify_proxmox_template(int(rendered["vmid"]), str(rendered["vm_name"]), env)
        except Exception as exc:
            print(f"Template build finished but verification failed: {exc}", file=sys.stderr)
            return 1
        print(f"Template {rendered['vm_name']} ({rendered['vmid']}) is present and marked as a Proxmox template.")
        return 0
    print(f"Unsupported templates command {args.templates_command}", file=sys.stderr)
    return 1


def _delete_proxmox_vm_if_present(vmid: int, env: dict[str, str]) -> None:
    client = ProxmoxClient.from_env(env)
    node = env.get("PROXMOX_NODE") or env.get("TF_VAR_pm_node") or "pve1"
    try:
        config = client.get(f"/nodes/{node}/qemu/{vmid}/config")
    except ProxmoxError as exc:
        if "HTTP 500" in str(exc) or "does not exist" in str(exc):
            return
        raise
    if not config:
        return
    status = client.get(f"/nodes/{node}/qemu/{vmid}/status/current") or {}
    if status.get("status") == "running":
        task = client.post(f"/nodes/{node}/qemu/{vmid}/status/stop")
        _wait_proxmox_task(client, node, task)
    task = client.delete(f"/nodes/{node}/qemu/{vmid}", {"purge": 1, "destroy-unreferenced-disks": 1})
    _wait_proxmox_task(client, node, task)


def _run_packer_build_on_proxmox_node(
    workdir: Path,
    definition: dict[str, Any],
    build_name: str,
    env: dict[str, str],
    rendered: dict[str, Any] | None = None,
) -> int:
    defaults = definition.get("defaults") or {}
    host = (
        env.get("RANGECTL_PROXMOX_SSH_HOST")
        or env.get("PROXMOX_SSH_HOST")
        or env.get("PROXMOX_NODE")
        or "pve1"
    )
    user = env.get("RANGECTL_PROXMOX_SSH_USER") or env.get("PROXMOX_SSH_USER") or "root"
    key = env.get("RANGECTL_PROXMOX_SSH_KEY") or env.get("PROXMOX_SSH_KEY")
    ssh, scp, rsync_ssh = _ssh_transport(env, key)
    target = f"{user}@{host}"
    remote_dir = f"/tmp/rangectl-template-builds/{build_name}"

    def run(command: list[str]) -> int:
        return subprocess.run(command).returncode

    rc = run(ssh + [target, "mkdir -p /usr/local/bin /root/.config/packer/plugins/github.com/hashicorp /tmp/rangectl-template-builds"])
    if rc != 0:
        return rc
    rc = run(scp + [shutil.which(env.get("RANGECTL_PACKER_BIN", "packer")) or "/usr/bin/packer", f"{target}:/usr/local/bin/packer"])
    if rc != 0:
        return rc
    plugin_dir = Path.home() / ".config" / "packer" / "plugins" / "github.com" / "hashicorp" / "proxmox"
    if plugin_dir.exists():
        rc = run(["rsync", "-az", "-e", rsync_ssh, str(plugin_dir), f"{target}:/root/.config/packer/plugins/github.com/hashicorp/"])
        if rc != 0:
            return rc
    rc = run(["rsync", "-az", "--delete", "-e", rsync_ssh, f"{workdir}/", f"{target}:{remote_dir}/"])
    if rc != 0:
        return rc

    if rendered and rendered.get("autounattend_iso_name"):
        iso_storage = str(rendered.get("proxmox_iso_storage") or (definition.get("defaults") or {}).get("iso_storage") or "local")
        iso_name = str(rendered["autounattend_iso_name"])
        if iso_storage == "local":
            iso_dir = "/var/lib/vz/template/iso"
        else:
            iso_dir = f"/mnt/pve/{iso_storage}/template/iso"
        create_autounattend_iso = "\n".join(
            [
                "set -e",
                f"mkdir -p {shlex.quote(iso_dir)}",
                "if command -v genisoimage >/dev/null 2>&1; then",
                (
                    f"  genisoimage -iso-level 4 -J -r -V 'autounattend CD' "
                    f"-o {shlex.quote(str(Path(iso_dir) / iso_name))} "
                    f"{shlex.quote(str(Path(remote_dir) / 'Autounattend.xml'))}"
                ),
                "elif command -v xorriso >/dev/null 2>&1; then",
                (
                    f"  xorriso -as mkisofs -iso-level 4 -J -r -allow-lowercase -allow-multidot "
                    f"-relaxed-filenames -d -omit-version-number -V 'autounattend CD' "
                    f"-o {shlex.quote(str(Path(iso_dir) / iso_name))} "
                    f"{shlex.quote(str(Path(remote_dir) / 'Autounattend.xml'))}"
                ),
                "else",
                "  echo 'xorriso or genisoimage is required to build the autounattend ISO' >&2",
                "  exit 1",
                "fi",
            ]
        )
        rc = run(ssh + [target, create_autounattend_iso])
        if rc != 0:
            return rc

    vlan_iface = defaults.get("runner_vlan_interface")
    vlan_ip = defaults.get("runner_vlan_ip")
    if vlan_iface and vlan_ip:
        rc = run(ssh + [target, f"ip addr show {vlan_iface} | grep -q '{str(vlan_ip).split('/')[0]}' || ip addr add {vlan_ip} dev {vlan_iface}"])
        if rc != 0:
            return rc

    remote_command = (
        f"cd {remote_dir} && "
        f"PACKER_LOG=1 PACKER_LOG_PATH=/tmp/packer-{build_name}.log "
        f"/usr/local/bin/packer build -force -on-error=abort ."
    )
    process = subprocess.Popen(ssh + ["-tt", target, remote_command])
    if rendered and rendered.get("force_install_boot_order"):
        _force_packer_install_boot_order(
            process,
            int(rendered["vmid"]),
            str(rendered["install_boot_order"]),
            str(rendered.get("final_boot_order") or ""),
            int(rendered.get("final_boot_order_delay_seconds") or 0),
            env,
        )
    return process.wait()


def _stage_autounattend_iso_on_proxmox(
    workdir: Path,
    definition: dict[str, Any],
    build_name: str,
    env: dict[str, str],
    rendered: dict[str, Any] | None = None,
) -> int:
    if not rendered or not rendered.get("autounattend_iso_name"):
        return 0

    defaults = definition.get("defaults") or {}
    host = (
        env.get("RANGECTL_PROXMOX_SSH_HOST")
        or env.get("PROXMOX_SSH_HOST")
        or env.get("PROXMOX_NODE")
        or "pve1"
    )
    user = env.get("RANGECTL_PROXMOX_SSH_USER") or env.get("PROXMOX_SSH_USER") or "root"
    key = env.get("RANGECTL_PROXMOX_SSH_KEY") or env.get("PROXMOX_SSH_KEY")
    ssh, scp, _ = _ssh_transport(env, key)

    target = f"{user}@{host}"
    remote_dir = f"/tmp/rangectl-template-builds/{build_name}-autounattend"
    iso_storage = str(rendered.get("proxmox_iso_storage") or defaults.get("iso_storage") or "local")
    iso_name = str(rendered["autounattend_iso_name"])
    iso_dir = "/var/lib/vz/template/iso" if iso_storage == "local" else f"/mnt/pve/{iso_storage}/template/iso"
    autounattend = workdir / "Autounattend.xml"

    rc = subprocess.run(ssh + [target, f"mkdir -p {shlex.quote(remote_dir)} {shlex.quote(iso_dir)}"]).returncode
    if rc != 0:
        return rc
    rc = subprocess.run(scp + [str(autounattend), f"{target}:{remote_dir}/Autounattend.xml"]).returncode
    if rc != 0:
        return rc

    create_autounattend_iso = "\n".join(
        [
            "set -e",
            "if command -v genisoimage >/dev/null 2>&1; then",
            (
                f"  genisoimage -iso-level 4 -J -r -V 'autounattend CD' "
                f"-o {shlex.quote(str(Path(iso_dir) / iso_name))} "
                f"{shlex.quote(str(Path(remote_dir) / 'Autounattend.xml'))}"
            ),
            "elif command -v xorriso >/dev/null 2>&1; then",
            (
                f"  xorriso -as mkisofs -iso-level 4 -J -r -allow-lowercase -allow-multidot "
                f"-relaxed-filenames -d -omit-version-number -V 'autounattend CD' "
                f"-o {shlex.quote(str(Path(iso_dir) / iso_name))} "
                f"{shlex.quote(str(Path(remote_dir) / 'Autounattend.xml'))}"
            ),
            "else",
            "  echo 'xorriso or genisoimage is required to build the autounattend ISO' >&2",
            "  exit 1",
            "fi",
        ]
    )
    return subprocess.run(ssh + [target, create_autounattend_iso]).returncode


def _force_packer_install_boot_order(
    process: subprocess.Popen[Any],
    vmid: int,
    install_boot_order: str,
    final_boot_order: str,
    final_boot_order_delay_seconds: int,
    env: dict[str, str],
) -> None:
    if not install_boot_order:
        return
    client = ProxmoxClient.from_env(env)
    node = env.get("PROXMOX_NODE") or env.get("TF_VAR_pm_node") or "pve1"
    deadline = time.time() + 300
    desired = f"order={install_boot_order}"
    applied = False
    while process.poll() is None and time.time() < deadline:
        try:
            config = client.get(f"/nodes/{node}/qemu/{vmid}/config") or {}
        except ProxmoxError as exc:
            if "HTTP 500" not in str(exc) and "does not exist" not in str(exc):
                print(f"Warning: could not inspect VMID {vmid} for install boot order: {exc}", file=sys.stderr)
            time.sleep(2)
            continue
        if str(config.get("template")) == "1":
            return
        if str(config.get("boot")) != desired:
            client.put(f"/nodes/{node}/qemu/{vmid}/config", {"boot": desired})
            status = client.get(f"/nodes/{node}/qemu/{vmid}/status/current") or {}
            if status.get("status") == "running":
                task = client.post(f"/nodes/{node}/qemu/{vmid}/status/reset")
                _wait_proxmox_task(client, node, task, timeout=120)
            print(f"Forced VMID {vmid} install boot order to {install_boot_order}")
        applied = True
        break
    if not applied and process.poll() is None:
        print(f"Warning: VMID {vmid} did not appear in time to force install boot order", file=sys.stderr)
        return
    if (
        final_boot_order
        and final_boot_order != install_boot_order
        and final_boot_order_delay_seconds > 0
        and process.poll() is None
    ):
        deadline = time.time() + final_boot_order_delay_seconds
        while process.poll() is None and time.time() < deadline:
            time.sleep(min(5, max(0, deadline - time.time())))
        if process.poll() is None:
            try:
                config = client.get(f"/nodes/{node}/qemu/{vmid}/config") or {}
                if str(config.get("template")) != "1":
                    client.put(f"/nodes/{node}/qemu/{vmid}/config", {"boot": f"order={final_boot_order}"})
                    print(f"Switched VMID {vmid} boot order to {final_boot_order} for Windows setup reboots")
            except ProxmoxError as exc:
                print(f"Warning: could not switch VMID {vmid} to final boot order: {exc}", file=sys.stderr)


def _verify_proxmox_template(vmid: int, expected_name: str, env: dict[str, str]) -> None:
    client = ProxmoxClient.from_env(env)
    node = env.get("PROXMOX_NODE") or env.get("TF_VAR_pm_node") or "pve1"
    config = client.get(f"/nodes/{node}/qemu/{vmid}/config") or {}
    if str(config.get("name")) != expected_name:
        raise RuntimeError(f"VMID {vmid} has name {config.get('name')!r}, expected {expected_name!r}")
    if str(config.get("template")) != "1":
        raise RuntimeError(f"VMID {vmid} is not marked as a template")


def _set_proxmox_vm_boot_order(vmid: int, boot_order: str, env: dict[str, str]) -> None:
    if not boot_order:
        return
    client = ProxmoxClient.from_env(env)
    node = env.get("PROXMOX_NODE") or env.get("TF_VAR_pm_node") or "pve1"
    client.put(f"/nodes/{node}/qemu/{vmid}/config", {"boot": f"order={boot_order}"})


def _set_proxmox_vm_disk_cache(vmid: int, disk_key: str, cache: str, env: dict[str, str]) -> None:
    if not disk_key or not cache:
        return
    client = ProxmoxClient.from_env(env)
    node = env.get("PROXMOX_NODE") or env.get("TF_VAR_pm_node") or "pve1"
    config = client.get(f"/nodes/{node}/qemu/{vmid}/config") or {}
    disk = str(config.get(disk_key) or "")
    if not disk:
        raise RuntimeError(f"VMID {vmid} has no disk {disk_key}")
    parts = [part for part in disk.split(",") if not part.startswith("cache=")]
    parts.append(f"cache={cache}")
    client.put(f"/nodes/{node}/qemu/{vmid}/config", {disk_key: ",".join(parts)})


def _wait_proxmox_task(client: ProxmoxClient, node: str, upid: str | None, timeout: int = 600) -> None:
    if not upid:
        return
    deadline = time.time() + timeout
    encoded = upid.replace(":", "%3A")
    while time.time() < deadline:
        status = client.get(f"/nodes/{node}/tasks/{encoded}/status") or {}
        if status.get("status") == "stopped":
            if status.get("exitstatus") not in (None, "OK"):
                raise RuntimeError(f"Proxmox task failed: {status}")
            return
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for Proxmox task {upid}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rangectl")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_cmd = subparsers.add_parser("list", help="list range definitions")
    list_cmd.set_defaults(func=cmd_list)

    validate = subparsers.add_parser("validate", help="validate a range definition")
    validate.add_argument("range")
    validate.add_argument("--online", action="store_true", help="also validate provider env prerequisites")
    validate.set_defaults(func=cmd_validate)

    plan = subparsers.add_parser("plan", help="show planned work for a range")
    plan.add_argument("range")
    plan.add_argument("--phase", choices=["all", "render", "infra", "configure"], default="all")
    plan.set_defaults(func=cmd_plan)

    render = subparsers.add_parser("render", help="render generated state files")
    render.add_argument("range")
    render.set_defaults(func=cmd_render)

    up = subparsers.add_parser("up", help="create or update a full range: render, infra apply, configure")
    up.add_argument("range")
    up.add_argument("--allow-replace", action="store_true", help="allow infra apply plans that destroy or replace resources")
    up.add_argument("--configure-phase", help="single configure phase to run after infra apply")
    up.add_argument("--tags", help="ansible tags")
    up.add_argument("--limit", help="ansible host/group limit")
    up.add_argument("--extra-vars", action="append", help="extra variables passed to ansible-playbook during configure")
    up.add_argument("--dry-run-configure", action="store_true", help="print configure commands without running them")
    up.add_argument("--skip-provider", action="append", help="provider to skip for this run; may be repeated or comma-separated")
    up.set_defaults(func=cmd_up)

    destroy = subparsers.add_parser("destroy", help="destroy range infrastructure")
    destroy.add_argument("range")
    destroy.add_argument("--hosts", help="comma-separated host names for provider actions that support host targeting")
    destroy.add_argument("--auto-approve", action="store_true", help="required to destroy infrastructure")
    destroy.add_argument("--refresh", default=True, action=argparse.BooleanOptionalAction, help="refresh Terraform state before planning")
    destroy.add_argument("--skip-provider", action="append", help="provider to skip for this run; may be repeated or comma-separated")
    destroy.set_defaults(func=cmd_destroy)

    down = subparsers.add_parser("down", help="destroy range infrastructure; alias for destroy")
    down.add_argument("range")
    down.add_argument("--hosts", help="comma-separated host names for provider actions that support host targeting")
    down.add_argument("--auto-approve", action="store_true", help="required to destroy infrastructure")
    down.add_argument("--refresh", default=True, action=argparse.BooleanOptionalAction, help="refresh Terraform state before planning")
    down.add_argument("--skip-provider", action="append", help="provider to skip for this run; may be repeated or comma-separated")
    down.set_defaults(func=cmd_destroy)

    configure = subparsers.add_parser("configure", help="run configured Ansible phases")
    configure.add_argument("range")
    configure.add_argument("--phase", help="single phase name from the range file")
    configure.add_argument("--tags", help="ansible tags")
    configure.add_argument("--limit", help="ansible host/group limit")
    configure.add_argument("--extra-vars", action="append", help="extra variables passed to ansible-playbook")
    configure.add_argument("--dry-run", action="store_true", help="print ansible-playbook commands without running them")
    configure.set_defaults(func=cmd_configure)

    phases = subparsers.add_parser("phases", help="list configured Ansible phases for a range")
    phases.add_argument("range")
    phases.add_argument("--json", action="store_true", help="print machine-readable JSON")
    phases.set_defaults(func=cmd_phases)

    env_cmd = subparsers.add_parser("env", help="show required environment values for a range")
    env_cmd.add_argument("range")
    env_cmd.add_argument("--json", action="store_true", help="print machine-readable JSON")
    env_cmd.set_defaults(func=cmd_env)

    doctor = subparsers.add_parser("doctor", help="check whether this workstation can operate a range")
    doctor.add_argument("range")
    doctor.add_argument("--online", action="store_true", help="also probe configured providers")
    doctor.add_argument("--json", action="store_true", help="print machine-readable JSON")
    doctor.set_defaults(func=cmd_doctor)

    infra = subparsers.add_parser("infra", help="run provider infrastructure lifecycle")
    infra.add_argument("action", choices=["init", "plan", "apply", "destroy", "adopt"])
    infra.add_argument("range")
    infra.add_argument("--hosts", help="comma-separated host names for provider actions that support host targeting")
    infra.add_argument("--execute", action="store_true", help="execute adopt actions that default to preview")
    infra.add_argument("--auto-approve", action="store_true", help="required for apply/destroy")
    infra.add_argument("--allow-replace", action="store_true", help="allow apply plans that destroy or replace resources")
    infra.add_argument("--refresh", default=True, action=argparse.BooleanOptionalAction, help="refresh Terraform state before planning")
    infra.add_argument("--skip-provider", action="append", help="provider to skip for this run; may be repeated or comma-separated")
    infra.set_defaults(func=cmd_infra)

    templates = subparsers.add_parser("templates", help="template catalog and build helper commands")
    templates_sub = templates.add_subparsers(dest="templates_command", required=True)
    templates_list = templates_sub.add_parser("list", help="list resolved template catalog entries")
    templates_list.add_argument("--json", action="store_true", help="print machine-readable JSON")
    templates_list.set_defaults(func=cmd_templates)
    templates_check = templates_sub.add_parser("check", help="check template references for a range")
    templates_check.add_argument("range")
    templates_check.add_argument("--json", action="store_true", help="print machine-readable JSON")
    templates_check.set_defaults(func=cmd_templates)
    templates_inspect = templates_sub.add_parser("inspect-url", help="inspect a template source URL without downloading it")
    templates_inspect.add_argument("url")
    templates_inspect.add_argument("--timeout", type=int, default=30)
    templates_inspect.add_argument("--format", choices=["yaml", "json"], default="yaml")
    templates_inspect.set_defaults(func=cmd_templates)
    templates_doctor = templates_sub.add_parser("doctor", help="check template build prerequisites")
    templates_doctor.add_argument("provider", choices=["proxmox"])
    templates_doctor.set_defaults(func=cmd_templates)
    templates_build = templates_sub.add_parser("build", help="render and run a Packer template build")
    templates_build.add_argument("provider", choices=["proxmox"])
    templates_build.add_argument("name")
    templates_build.add_argument("--definition", required=True)
    templates_build.add_argument("--runner", choices=["local", "proxmox-node"], help="where to run Packer; defaults to definition defaults.runner or local")
    templates_build.add_argument("--replace-existing", action="store_true", help="delete an existing VM/template with the output VMID before building")
    templates_build.add_argument("--dry-run", action="store_true", help="render and validate Packer without running the build")
    templates_build.set_defaults(func=cmd_templates)

    power = subparsers.add_parser("power", help="provider power commands for range hosts")
    power.add_argument("action", choices=["on", "off", "reboot", "status"])
    power.add_argument("range")
    power.add_argument("--hosts", help="comma-separated host names")
    power.add_argument("--force", action="store_true", help="use hard stop for power off")
    power.add_argument("--execute", action="store_true", help="execute power changes; status always runs")
    power.set_defaults(func=cmd_power)

    snapshot = subparsers.add_parser("snapshot", help="snapshot commands for range hosts")
    snapshot_sub = snapshot.add_subparsers(dest="snapshot_command", required=True)
    snapshot_create = snapshot_sub.add_parser("create", help="create snapshots")
    snapshot_create.add_argument("range")
    snapshot_create.add_argument("name")
    snapshot_create.add_argument("--hosts", help="comma-separated host names")
    snapshot_create.add_argument("--description")
    snapshot_create.add_argument("--execute", action="store_true")
    snapshot_create.set_defaults(func=cmd_snapshot)
    snapshot_revert = snapshot_sub.add_parser("revert", help="revert snapshots")
    snapshot_revert.add_argument("range")
    snapshot_revert.add_argument("name")
    snapshot_revert.add_argument("--hosts", help="comma-separated host names")
    snapshot_revert.add_argument("--execute", action="store_true")
    snapshot_revert.set_defaults(func=cmd_snapshot)
    snapshot_delete = snapshot_sub.add_parser("delete", help="delete snapshots")
    snapshot_delete.add_argument("range")
    snapshot_delete.add_argument("name")
    snapshot_delete.add_argument("--hosts", help="comma-separated host names")
    snapshot_delete.add_argument("--execute", action="store_true")
    snapshot_delete.set_defaults(func=cmd_snapshot)
    snapshot_list = snapshot_sub.add_parser("list", help="list snapshots")
    snapshot_list.add_argument("range")
    snapshot_list.add_argument("--hosts", help="comma-separated host names")
    snapshot_list.set_defaults(func=cmd_snapshot)

    inventory = subparsers.add_parser("inventory", help="show generated Ansible inventory")
    inventory.add_argument("range")
    inventory.add_argument("--path", action="store_true", help="print inventory path only")
    inventory.add_argument("--render", action="store_true", help="render before showing inventory")
    inventory.set_defaults(func=cmd_inventory)

    status = subparsers.add_parser("status", help="show local rendered range status")
    status.add_argument("range")
    status.set_defaults(func=cmd_status)

    doc = subparsers.add_parser("doc", help="documentation commands")
    doc_sub = doc.add_subparsers(dest="doc_command", required=True)
    doc_create = doc_sub.add_parser("create", help="generate range documentation")
    doc_create.add_argument("range")
    doc_create.set_defaults(func=cmd_doc_create)

    activity = subparsers.add_parser("activity", help="compile and run background activity simulation")
    activity_sub = activity.add_subparsers(dest="activity_command", required=True)
    activity_validate = activity_sub.add_parser("validate", help="validate/discover activity personas for a range")
    activity_validate.add_argument("range")
    activity_validate.set_defaults(func=cmd_activity)
    activity_compile = activity_sub.add_parser("compile", help="write per-host activity agent configs")
    activity_compile.add_argument("range")
    activity_compile.add_argument("--out", help="output directory; defaults to state/<range>/activity")
    activity_compile.set_defaults(func=cmd_activity)
    activity_run = activity_sub.add_parser("run", help="run one compiled activity agent config")
    activity_run.add_argument("config")
    activity_run.add_argument("--once", action="store_true", help="run one action cycle then exit")
    activity_run.add_argument("--dry-run", action="store_true", help="log actions without opening apps or changing files")
    activity_run.set_defaults(func=cmd_activity)
    activity_trigger = activity_sub.add_parser("trigger", help="queue a local trigger action for compiled agents")
    activity_trigger.add_argument("range")
    activity_trigger.add_argument("--hosts", required=True, help="comma-separated activity host names")
    activity_trigger.add_argument("--type", required=True, help="action type, e.g. email.phishing or browser.form_submit")
    activity_trigger.add_argument("--arg", action="append", help="action arg as key=value; value may be JSON")
    activity_trigger.add_argument("--trackable-id")
    activity_trigger.set_defaults(func=cmd_activity)
    activity_collect = activity_sub.add_parser("collect", help="print local activity JSONL logs")
    activity_collect.add_argument("range")
    activity_collect.add_argument("--paths", action="store_true", help="print log paths instead of log contents")
    activity_collect.set_defaults(func=cmd_activity)

    return parser


def _should_record_run(args: argparse.Namespace) -> bool:
    command = getattr(args, "command", "")
    if command in {"up", "destroy", "down"}:
        return True
    if command == "configure":
        return not getattr(args, "dry_run", False)
    if command == "infra":
        return getattr(args, "action", "") in {"apply", "destroy", "adopt"}
    if command == "power":
        return bool(getattr(args, "execute", False))
    if command == "snapshot":
        return bool(getattr(args, "execute", False))
    if command == "activity":
        return getattr(args, "activity_command", "") in {"run", "trigger"}
    return False


def _record_run(args: argparse.Namespace, argv: list[str], returncode: int, started_at: float) -> None:
    range_arg = getattr(args, "range", None)
    if not range_arg:
        return
    loaded = _load_range_or_error(str(range_arg))
    if loaded is None:
        return
    _, data = loaded
    finished_at = time.time()
    run_dir = range_state_dir(data) / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    command = getattr(args, "command", "unknown")
    subcommand = (
        getattr(args, "action", None)
        or getattr(args, "snapshot_command", None)
        or getattr(args, "activity_command", None)
        or getattr(args, "templates_command", None)
    )
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(finished_at))
    suffix = f"{command}-{subcommand}" if subcommand else command
    path = run_dir / f"{timestamp}-{suffix}.json"
    payload = {
        "range": range_name(data),
        "command": command,
        "subcommand": subcommand,
        "argv": argv,
        "returncode": returncode,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(finished_at)),
        "duration_seconds": round(finished_at - started_at, 3),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    os.environ.update(_rangectl_env())
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    started_at = time.time()
    rc = args.func(args)
    if _should_record_run(args):
        try:
            _record_run(args, raw_argv, rc, started_at)
        except OSError as exc:
            print(f"WARNING: could not write run record: {exc}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
