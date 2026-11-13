from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rangectl_lib.model import range_name, range_state_dir


class TartError(Exception):
    pass


@dataclass(frozen=True)
class TartHost:
    name: str
    ssh_host: str | None
    ssh_user: str
    ssh_key: str | None
    ssh_port: str | None
    tart_bin: str


class TartRunner:
    def __init__(self, host: TartHost, env: dict[str, str]) -> None:
        self.host = host
        self.env = env

    def command(self, args: list[str]) -> list[str]:
        tart_command = [self.host.tart_bin, *args]
        if not self.host.ssh_host:
            return tart_command

        command = ["ssh"]
        if self.host.ssh_port:
            command.extend(["-p", self.host.ssh_port])
        if self.host.ssh_key:
            command.extend(["-i", self.host.ssh_key])
        command.append(f"{self.host.ssh_user}@{self.host.ssh_host}")
        command.append(shlex.join(tart_command))
        return command

    def shell_command(self, script: str) -> list[str]:
        if not self.host.ssh_host:
            return ["sh", "-lc", script]

        command = ["ssh"]
        if self.host.ssh_port:
            command.extend(["-p", self.host.ssh_port])
        if self.host.ssh_key:
            command.extend(["-i", self.host.ssh_key])
        command.append(f"{self.host.ssh_user}@{self.host.ssh_host}")
        command.append(script)
        return command

    def run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        command = self.command(args)
        completed = subprocess.run(command, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and completed.returncode != 0:
            raise TartError(_format_failure(command, completed))
        return completed

    def run_shell(self, script: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = self.shell_command(script)
        completed = subprocess.run(command, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and completed.returncode != 0:
            raise TartError(_format_failure(command, completed))
        return completed


def _format_failure(command: list[str], completed: subprocess.CompletedProcess[str]) -> str:
    stderr = completed.stderr.strip()
    stdout = completed.stdout.strip()
    detail = stderr or stdout or f"exit {completed.returncode}"
    return f"{shlex.join(command)} failed: {detail}"


def tart_hosts_from_range(data: dict[str, Any], env: dict[str, str]) -> dict[str, TartHost]:
    hosts: dict[str, TartHost] = {}
    for host in ((data.get("providers") or {}).get("tart") or {}).get("hosts") or []:
        if not isinstance(host, dict) or not host.get("name"):
            continue
        name = str(host["name"])
        hosts[name] = TartHost(
            name=name,
            ssh_host=_provider_value(host, "ssh_host", "ssh_host_env", env),
            ssh_user=_provider_value(host, "ssh_user", "ssh_user_env", env) or "admin",
            ssh_key=_provider_value(host, "ssh_key", "ssh_key_env", env),
            ssh_port=_provider_value(host, "ssh_port", "ssh_port_env", env),
            tart_bin=_provider_value(host, "tart_bin", "tart_bin_env", env) or "tart",
        )
    return hosts


def _provider_value(provider: dict[str, Any], key: str, env_key: str, env: dict[str, str]) -> str | None:
    if provider.get(env_key):
        return env.get(str(provider[env_key]))
    value = provider.get(key)
    return str(value) if value is not None else None


def unresolved_tart_env(data: dict[str, Any], env: dict[str, str]) -> list[str]:
    missing: set[str] = set()
    for host in ((data.get("providers") or {}).get("tart") or {}).get("hosts") or []:
        if not isinstance(host, dict):
            continue
        for key in ("ssh_host_env", "ssh_user_env", "ssh_key_env", "ssh_port_env", "tart_bin_env"):
            env_name = host.get(key)
            if env_name and str(env_name) not in env:
                missing.add(str(env_name))
    return sorted(missing)


def tart_command_for_host(data: dict[str, Any], host: dict[str, Any], action: str, env: dict[str, str]) -> list[str]:
    tart_hosts = tart_hosts_from_range(data, env)
    runner = TartRunner(tart_hosts[str(host.get("tart_host", host.get("provider_host")))], env)
    return runner.command(_single_tart_args(host, action))


def _single_tart_args(host: dict[str, Any], action: str) -> list[str]:
    name = str(host.get("name"))
    source = str(host.get("source"))
    resources = host.get("resources") or {}
    if action == "pull":
        return ["pull", source]
    if action == "clone":
        return ["clone", source, name]
    if action == "set":
        command = ["set", name]
        if resources.get("cpu"):
            command.extend(["--cpu", str(resources["cpu"])])
        if resources.get("memory_mb"):
            command.extend(["--memory", str(resources["memory_mb"])])
        if resources.get("disk_gb"):
            command.extend(["--disk-size", str(resources["disk_gb"])])
        return command
    if action == "start":
        return ["run", "--no-graphics", *_network_args(host), name]
    if action == "stop":
        return ["stop", name]
    if action == "status":
        return ["ip", name]
    if action == "validate":
        return ["--version"]
    if action == "apply":
        return ["get", name, "--format", "json"]
    raise ValueError(f"unknown Tart action {action}")


def validate_tart_host(runner: TartRunner, probe_vm: str | None = None) -> dict[str, Any]:
    version = runner.run(["--version"]).stdout.strip()
    interfaces = runner.run(["run", "--net-bridged", "list", probe_vm or "__rangectl_probe__"], check=False)
    return {
        "tart_version": version,
        "bridge_interfaces": _parse_bridge_interfaces(interfaces.stderr + interfaces.stdout),
    }


def _parse_bridge_interfaces(output: str) -> list[str]:
    marker = "available interfaces:"
    if marker not in output:
        return []
    interfaces = output.split(marker, 1)[1].strip()
    match = re.search(r"\[[^\]]+\]", interfaces)
    if match:
        interfaces = match.group(0)
    try:
        parsed = json.loads(interfaces)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except json.JSONDecodeError:
        pass
    return [interfaces]


def list_vms(runner: TartRunner) -> list[dict[str, Any]]:
    completed = runner.run(["list", "--format", "json"])
    parsed = json.loads(completed.stdout)
    return parsed if isinstance(parsed, list) else []


def vm_entry(runner: TartRunner, name: str) -> dict[str, Any] | None:
    for item in list_vms(runner):
        if item.get("Name") == name:
            return item
    return None


def get_vm(runner: TartRunner, name: str) -> dict[str, Any] | None:
    completed = runner.run(["get", name, "--format", "json"], check=False)
    if completed.returncode != 0:
        return None
    parsed = json.loads(completed.stdout)
    return parsed if isinstance(parsed, dict) else None


def read_vm_config(runner: TartRunner, name: str) -> dict[str, Any]:
    path = f"~/.tart/vms/{shlex.quote(name)}/config.json"
    completed = runner.run_shell(f"cat {path}", check=False)
    if completed.returncode != 0 or not completed.stdout.strip():
        return {}
    parsed = json.loads(completed.stdout)
    return parsed if isinstance(parsed, dict) else {}


def apply_guest(runner: TartRunner, host: dict[str, Any]) -> None:
    name = str(host["name"])
    source = str(host["source"])
    if _is_remote_source(source):
        runner.run(["pull", source])
    if vm_entry(runner, name) is None:
        runner.run(["clone", source, name])
    runner.run(_single_tart_args(host, "set"))


def _is_remote_source(source: str) -> bool:
    return "/" in source or ":" in source


def start_guest(runner: TartRunner, host: dict[str, Any]) -> None:
    name = str(host["name"])
    info = get_vm(runner, name)
    if info and info.get("Running"):
        return
    args = [runner.host.tart_bin, "run", "--no-graphics", *_network_args(host), name]
    log_path = f"~/.tart/rangectl-{shlex.quote(name)}.log"
    script = f"nohup {shlex.join(args)} > {log_path} 2>&1 < /dev/null &"
    runner.run_shell(script)


def stop_guest(runner: TartRunner, host: dict[str, Any]) -> None:
    name = str(host["name"])
    info = get_vm(runner, name)
    if info and info.get("Running"):
        runner.run(["stop", name])


def _network_args(host: dict[str, Any]) -> list[str]:
    network = host.get("network") or {}
    mode = network.get("mode")
    if mode == "bridged":
        return ["--net-bridged", str(network.get("interface"))]
    if mode == "host":
        return ["--net-host"]
    if mode == "softnet":
        args = ["--net-softnet"]
        if network.get("allow"):
            args.append(f"--net-softnet-allow={network['allow']}")
        if network.get("block"):
            args.append(f"--net-softnet-block={network['block']}")
        if network.get("expose"):
            args.append(f"--net-softnet-expose={network['expose']}")
        return args
    return []


def guest_status(runner: TartRunner, host: dict[str, Any], wait: int = 0, resolver: str | None = None) -> dict[str, Any]:
    name = str(host["name"])
    entry = vm_entry(runner, name)
    config = get_vm(runner, name) if entry else None
    raw_config = read_vm_config(runner, name) if entry else {}
    status = {
        "name": name,
        "tart_host": host.get("tart_host", host.get("provider_host")),
        "exists": entry is not None,
        "state": entry.get("State") if entry else "missing",
        "running": bool(entry.get("Running")) if entry else False,
        "desired_ip": host.get("ip"),
        "discovered_ip": None,
        "mac_address": raw_config.get("macAddress"),
        "config": config or {},
    }
    if entry and entry.get("Running"):
        ip_resolver = resolver or (host.get("network") or {}).get("ip_resolver") or "dhcp"
        completed = runner.run(["ip", name, "--wait", str(wait), "--resolver", str(ip_resolver)], check=False)
        if completed.returncode == 0:
            status["discovered_ip"] = completed.stdout.strip()
            status["ip_resolver"] = ip_resolver
        else:
            status["ip_error"] = (completed.stderr or completed.stdout).strip()
            status["ip_resolver"] = ip_resolver
    return status


def update_tart_runtime_state(data: dict[str, Any], statuses: list[dict[str, Any]]) -> Path:
    state_dir = range_state_dir(data)
    state_path = state_dir / "range-state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    else:
        state = {"name": range_name(data), "hosts": [], "spaces": {}}
    runtime = state.setdefault("runtime", {})
    tart = runtime.setdefault("tart", {})
    tart["hosts"] = {status["name"]: status for status in statuses}
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tart_state_path = state_dir / "render" / "tart" / "status.json"
    tart_state_path.parent.mkdir(parents=True, exist_ok=True)
    tart_state_path.write_text(json.dumps({"hosts": statuses}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return tart_state_path
