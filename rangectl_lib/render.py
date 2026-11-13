from __future__ import annotations

import ipaddress
import json
import re
import secrets
import shutil
import subprocess
import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from rangectl_lib.model import ROOT, RANGES_DIR, RANGE_PATH_KEY, flatten_spaces, range_base_dir, range_name, range_state_dir
from rangectl_lib.templates import proxmox_template_catalog


class NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def ensure_dirs(data: dict[str, Any]) -> dict[str, Path]:
    base = range_state_dir(data)
    dirs = {
        "base": base,
        "render": base / "render",
        "inventory": base / "inventory",
        "docs": Path("docs") / "generated",
        "tofu": base / "render" / "tofu",
        "proxmox": base / "render" / "tofu" / "proxmox",
        "aws": base / "render" / "tofu" / "aws",
        "azure": base / "render" / "tofu" / "azure",
        "tart": base / "render" / "tart",
        "vpn": base / "vpn",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
        if path != dirs["docs"]:
            path.chmod(0o700)
    return dirs


def render_all(data: dict[str, Any]) -> list[Path]:
    dirs = ensure_dirs(data)
    written = [
        render_inventory(data, dirs["inventory"]),
        *render_ad_compat_data(data, dirs["base"]),
        render_range_state(data, dirs["base"]),
        render_docs(data, dirs["docs"]),
    ]
    from rangectl_lib.activity.compiler import compile_activity

    written.extend(compile_activity(data, out_dir=dirs["base"] / "activity"))
    written.extend(render_vpn_artifacts(data, dirs["vpn"]))
    written.extend(render_tofu(data, dirs))
    written.extend(render_tart(data, dirs["tart"]))
    return written


def ad_compat_data_dir(data: dict[str, Any], state_dir: Path | None = None) -> Path:
    ansible_vars = (data.get("ansible") or {}).get("vars") or {}
    domain_name = str(ansible_vars.get("domain_name") or range_name(data))
    return (state_dir or range_state_dir(data)) / "ad" / domain_name / "data"


def render_ad_compat_data(data: dict[str, Any], out_dir: Path) -> list[Path]:
    active_directory = data.get("active_directory")
    if not isinstance(active_directory, dict):
        return []
    compatibility = active_directory.get("compatibility")
    if isinstance(compatibility, dict) and compatibility.get("enabled") is False:
        return []
    lab = active_directory.get("lab")
    if not isinstance(lab, dict):
        return []

    data_dir = ad_compat_data_dir(data, out_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "config.json"
    path.write_text(json.dumps({"lab": lab}, indent=4, sort_keys=False) + "\n", encoding="utf-8")
    return [path]


def render_vpn_artifacts(data: dict[str, Any], out_dir: Path) -> list[Path]:
    vpn_config = data.get("vpn") or {}
    openvpn = vpn_config.get("openvpn") if isinstance(vpn_config, dict) else {}
    if not isinstance(openvpn, dict) or not openvpn.get("enabled"):
        return []

    server = openvpn.get("server") if isinstance(openvpn.get("server"), dict) else {}
    clients = openvpn.get("clients") if isinstance(openvpn.get("clients"), list) else []
    if not clients:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    clients_dir = out_dir / "clients"
    clients_dir.mkdir(parents=True, exist_ok=True)
    pki_dir = out_dir / "pki"
    private_dir = pki_dir / "private"
    private_dir.mkdir(parents=True, exist_ok=True)

    state_path = out_dir / "openvpn-state.json"
    state: dict[str, Any] = {}
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except json.JSONDecodeError:
            state = {}

    written: list[Path] = []
    openssl = shutil.which("openssl")
    slug = _safe_slug(range_name(data))
    ca_crt = pki_dir / f"{slug}-openvpn-ca.crt"
    ca_key = private_dir / f"{slug}-openvpn-ca.key"
    server_crt = pki_dir / f"{slug}-openvpn-server.crt"
    server_key = private_dir / f"{slug}-openvpn-server.key"
    tls_auth_key = pki_dir / f"{slug}-openvpn-ta.key"

    if openssl:
        _ensure_openvpn_pki(
            openssl,
            pki_dir,
            private_dir,
            ca_crt,
            ca_key,
            server_crt,
            server_key,
            ca_common_name=str(openvpn.get("ca_common_name") or f"{range_name(data)} OpenVPN CA"),
            server_common_name=str(server.get("common_name") or f"{range_name(data)} OpenVPN server"),
        )
        written.extend([ca_crt, ca_key, server_crt, server_key])
    _ensure_openvpn_tls_auth_key(tls_auth_key)
    written.append(tls_auth_key)

    generated_clients = state.setdefault("clients", {})
    for client in clients:
        if not isinstance(client, dict):
            continue
        name = str(client.get("name") or client.get("username") or "openvpn-client")
        username = str(client.get("username") or name)
        profile_name = str(client.get("profile") or f"{name}.ovpn")
        auth_file_name = str(client.get("auth_file") or f"{name}.txt")
        client_state = generated_clients.setdefault(name, {})
        if "password" not in client_state:
            client_state["password"] = secrets.token_urlsafe(18)
        client_state["username"] = username
        client_state["profile"] = profile_name
        client_state["auth_file"] = auth_file_name

        client_crt = pki_dir / f"{name}.crt"
        client_key = private_dir / f"{name}.key"
        if openssl:
            _ensure_openvpn_client_cert(openssl, pki_dir, private_dir, ca_crt, ca_key, client_crt, client_key, name)
            written.extend([client_crt, client_key])

        auth_path = clients_dir / auth_file_name
        auth_path.write_text(f"{username}\n{client_state['password']}\n", encoding="utf-8")
        auth_path.chmod(0o600)
        written.append(auth_path)

        profile_path = clients_dir / profile_name
        profile_path.write_text(
            _openvpn_profile(
                server=server,
                ca_crt=ca_crt if openssl else None,
                client_crt=client_crt if openssl else None,
                client_key=client_key if openssl else None,
                tls_auth_key=tls_auth_key,
                auth_file_name=auth_file_name,
            ),
            encoding="utf-8",
        )
        written.append(profile_path)

    state["server"] = {
        "remote": str(server.get("remote") or "10.60.0.10"),
        "port": int(server.get("port") or 1194),
        "proto": str(server.get("proto") or "udp"),
        "tunnel_cidr": str(server.get("tunnel_cidr") or "10.250.40.0/24"),
        "routes": openvpn.get("routes") or [],
    }
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    state_path.chmod(0o600)
    written.append(state_path)

    readme = out_dir / "README.md"
    readme.write_text(_openvpn_artifact_readme(data, openvpn, clients_dir), encoding="utf-8")
    written.append(readme)
    return written


def _run_openssl(openssl: str, args: list[str], cwd: Path) -> None:
    subprocess.run([openssl, *args], cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _ensure_openvpn_pki(
    openssl: str,
    pki_dir: Path,
    private_dir: Path,
    ca_crt: Path,
    ca_key: Path,
    server_crt: Path,
    server_key: Path,
    *,
    ca_common_name: str,
    server_common_name: str,
) -> None:
    if not ca_key.exists():
        _run_openssl(openssl, ["genrsa", "-out", str(ca_key), "4096"], pki_dir)
    ca_key.chmod(0o600)
    if not ca_crt.exists():
        _run_openssl(
            openssl,
            [
                "req",
                "-x509",
                "-new",
                "-nodes",
                "-key",
                str(ca_key),
                "-sha256",
                "-days",
                "3650",
                "-subj",
                f"/CN={ca_common_name}",
                "-out",
                str(ca_crt),
            ],
            pki_dir,
        )
    if not server_key.exists():
        _run_openssl(openssl, ["genrsa", "-out", str(server_key), "2048"], pki_dir)
    server_key.chmod(0o600)
    if not server_crt.exists():
        csr = pki_dir / f"{_safe_slug(server_common_name)}.csr"
        _run_openssl(openssl, ["req", "-new", "-key", str(server_key), "-subj", f"/CN={server_common_name}", "-out", str(csr)], pki_dir)
        _run_openssl(
            openssl,
            [
                "x509",
                "-req",
                "-in",
                str(csr),
                "-CA",
                str(ca_crt),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-out",
                str(server_crt),
                "-days",
                "825",
                "-sha256",
            ],
            pki_dir,
        )
        csr.unlink(missing_ok=True)


def _ensure_openvpn_client_cert(
    openssl: str,
    pki_dir: Path,
    private_dir: Path,
    ca_crt: Path,
    ca_key: Path,
    client_crt: Path,
    client_key: Path,
    name: str,
) -> None:
    if not client_key.exists():
        _run_openssl(openssl, ["genrsa", "-out", str(client_key), "2048"], pki_dir)
    client_key.chmod(0o600)
    if not client_crt.exists():
        csr = pki_dir / f"{name}.csr"
        _run_openssl(openssl, ["req", "-new", "-key", str(client_key), "-subj", f"/CN={name}", "-out", str(csr)], pki_dir)
        _run_openssl(
            openssl,
            [
                "x509",
                "-req",
                "-in",
                str(csr),
                "-CA",
                str(ca_crt),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-out",
                str(client_crt),
                "-days",
                "825",
                "-sha256",
            ],
            pki_dir,
        )
        csr.unlink(missing_ok=True)


def _ensure_openvpn_tls_auth_key(path: Path) -> None:
    if path.exists():
        return
    key_hex = secrets.token_hex(256)
    lines = [key_hex[idx : idx + 32] for idx in range(0, len(key_hex), 32)]
    path.write_text(
        "\n".join(
            [
                "#",
                "# 2048 bit OpenVPN static key",
                "#",
                "-----BEGIN OpenVPN Static key V1-----",
                *lines,
                "-----END OpenVPN Static key V1-----",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _read_pem(path: Path | None) -> str:
    if path is None or not path.exists():
        return "PLACEHOLDER: run rangectl render on a host with openssl to generate certificate material"
    return path.read_text(encoding="utf-8").strip()


def _openvpn_profile(
    *,
    server: dict[str, Any],
    ca_crt: Path | None,
    client_crt: Path | None,
    client_key: Path | None,
    tls_auth_key: Path | None,
    auth_file_name: str,
) -> str:
    remote = str(server.get("remote") or "10.60.0.1")
    port = int(server.get("port") or 1194)
    proto = str(server.get("proto") or "udp")
    return f"""client
dev tun
proto {proto}
remote {remote} {port}
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
auth-user-pass {auth_file_name}
key-direction 1
verb 3

<ca>
{_read_pem(ca_crt)}
</ca>
<cert>
{_read_pem(client_crt)}
</cert>
<key>
{_read_pem(client_key)}
</key>
<tls-auth>
{_read_pem(tls_auth_key)}
</tls-auth>
"""


def _openvpn_artifact_readme(data: dict[str, Any], openvpn: dict[str, Any], clients_dir: Path) -> str:
    clients = openvpn.get("clients") if isinstance(openvpn.get("clients"), list) else []
    lines = [
        f"# {range_name(data)} OpenVPN Artifacts",
        "",
        "Generated OpenVPN client artifacts for this range.",
        "These files are ignored by git and should be handled as lab secrets.",
        "",
        f"Client artifact directory: `{clients_dir}`",
        "",
        "## Scenario Placement",
        "",
    ]
    for client in clients:
        if not isinstance(client, dict):
            continue
        plant = client.get("plant") if isinstance(client.get("plant"), dict) else {}
        lines.append(
            f"- `{client.get('profile')}` and `{client.get('auth_file')}` should be planted on "
            f"`{plant.get('host', 'files')}` under `{plant.get('path', 'C:\\\\share\\\\VPN')}`."
        )
    lines.extend(
        [
            "",
            "Store the profile and auth file according to the range story and access model.",
            "",
        ]
    )
    return "\n".join(lines)


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-").lower()
    return slug or "rangectl"


def render_inventory(data: dict[str, Any], out_dir: Path) -> Path:
    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"hosts": {}})
    ansible_config = data.get("ansible") or {}

    for host in data.get("hosts") or []:
        if not isinstance(host, dict):
            continue
        host_name = host["name"]
        ansible = host.get("ansible") or {}
        host_vars = {
            "ansible_host": host.get("ip"),
            "hostname": host.get("hostname"),
            "rangectl_owner": host.get("owner"),
            "rangectl_type": host.get("type"),
            "rangectl_space": host.get("space"),
        }
        host_vars.update(ansible.get("vars") or {})

        for group in ansible.get("groups") or []:
            groups[str(group)]["hosts"][host_name] = host_vars

        groups["all"]["hosts"][host_name] = host_vars

    for group_name in ansible_config.get("empty_groups") or []:
        groups[str(group_name)]

    for group_name, group_vars in (ansible_config.get("group_vars") or {}).items():
        groups[str(group_name)]["vars"] = group_vars or {}

    all_group = groups.pop("all", {"hosts": {}})
    all_vars = dict(ansible_config.get("vars") or {})
    base_dir = range_base_dir(data)
    state_dir = range_state_dir(data).resolve()
    all_vars.setdefault("rangectl_range_file", str(Path(str(data.get(RANGE_PATH_KEY) or "")).resolve()))
    all_vars.setdefault("rangectl_range_base", str(base_dir))
    all_vars.setdefault("rangectl_state_dir", str(state_dir))
    all_vars.setdefault("rangectl_ad_data_path", str(ad_compat_data_dir(data, state_dir)))
    domain_name = str(all_vars.get("domain_name") or range_name(data))
    all_vars.setdefault("rangectl_ad_scripts_path", str(base_dir / "ad" / domain_name / "scripts"))
    all_vars.setdefault("rangectl_ad_files_path", str(base_dir / "ad" / domain_name / "files"))
    if all_vars:
        all_group["vars"] = all_vars

    inventory = {
        "all": all_group,
    }
    if groups:
        inventory["all"]["children"] = dict(sorted(groups.items()))

    path = out_dir / "hosts.yml"
    path.write_text(yaml.dump(inventory, Dumper=NoAliasDumper, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    return path


def render_range_state(data: dict[str, Any], out_dir: Path) -> Path:
    hosts = [
        {
            "name": host.get("name"),
            "hostname": host.get("hostname"),
            "owner": host.get("owner"),
            "type": host.get("type"),
            "space": host.get("space"),
            "ip": host.get("ip"),
        }
        for host in data.get("hosts") or []
        if isinstance(host, dict)
    ]
    state = {
        "name": range_name(data),
        "hosts": hosts,
        "spaces": flatten_spaces(data),
    }
    path = out_dir / "range-state.json"
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and previous.get("runtime"):
                state["runtime"] = previous["runtime"]
        except json.JSONDecodeError:
            pass
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def render_tofu(data: dict[str, Any], dirs: dict[str, Path]) -> list[Path]:
    written: list[Path] = []
    stacks = {
        "proxmox": [host for host in data.get("hosts") or [] if host.get("owner") == "tofu:proxmox"],
        "aws": [host for host in data.get("hosts") or [] if host.get("owner") == "tofu:aws"],
        "azure": [host for host in data.get("hosts") or [] if host.get("owner") == "tofu:azure"],
    }

    for provider, hosts in stacks.items():
        stack_dir = dirs[provider]
        if provider == "proxmox":
            proxmox_files = render_proxmox_stack(data, stack_dir)
            written.extend(proxmox_files)
        elif provider == "aws":
            aws_files = render_aws_stack(data, stack_dir, hosts)
            written.extend(aws_files)
        else:
            tfvars = {
                "range_name": range_name(data),
                "provider_config": data.get("providers", {}).get(provider, {}),
                "spaces": flatten_spaces(data),
                "hosts": hosts,
            }
            tfvars_path = stack_dir / "terraform.tfvars.json"
            tfvars_path.write_text(json.dumps(tfvars, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tfvars_path.chmod(0o600)
            written.append(tfvars_path)

            main_path = stack_dir / "main.tf"
            main_path.write_text(_tofu_stub(provider), encoding="utf-8")
            written.append(main_path)

            versions_path = stack_dir / "versions.tf"
            versions_path.write_text(_versions_stub(provider), encoding="utf-8")
            written.append(versions_path)
    return written


def render_aws_stack(data: dict[str, Any], stack_dir: Path, hosts: list[dict[str, Any]]) -> list[Path]:
    cloud = data.get("cloud") or {}
    aws_scenario = cloud.get("aws") if isinstance(cloud, dict) else {}
    if not isinstance(aws_scenario, dict):
        aws_scenario = {}

    lambda_dir = stack_dir / "lambda"
    lambda_dir.mkdir(parents=True, exist_ok=True)
    lambda_source = lambda_dir / "rangectl_helper.py"
    lambda_source.write_text(AWS_LAMBDA_HELPER_PY, encoding="utf-8")

    tfvars = stack_dir / "terraform.tfvars.json"
    tfvars.write_text(
        json.dumps(
            {
                "range_name": range_name(data),
                "provider_config": data.get("providers", {}).get("aws", {}),
                "scenario": aws_scenario,
                "hosts": hosts,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    tfvars.chmod(0o600)

    variables = stack_dir / "variables.tf"
    variables.write_text(AWS_VARIABLES_TF, encoding="utf-8")

    main = stack_dir / "main.tf"
    main.write_text(AWS_MAIN_TF, encoding="utf-8")

    versions = stack_dir / "versions.tf"
    versions.write_text(_versions_stub("aws"), encoding="utf-8")

    return [tfvars, variables, main, versions, lambda_source]


def render_proxmox_stack(data: dict[str, Any], stack_dir: Path) -> list[Path]:
    proxmox_hosts = [host for host in data.get("hosts") or [] if host.get("owner") == "tofu:proxmox"]
    spaces = flatten_spaces(data)
    vm_hosts = [host for host in proxmox_hosts if host.get("type") == "vm"]
    lxc_hosts = [host for host in proxmox_hosts if host.get("type") == "lxc"]
    routers = [host for host in proxmox_hosts if host.get("type") == "router"]

    tfvars = stack_dir / "terraform.tfvars.json"
    tfvars.write_text(
        json.dumps(
            {
                "range_name": range_name(data),
                "lxc_passwords": {
                    str(host["name"]): _lxc_root_password(host)
                    for host in lxc_hosts
                    if host.get("name")
                },
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    tfvars.chmod(0o600)

    variables = stack_dir / "variables.tf"
    variables.write_text(PROXMOX_VARIABLES_TF, encoding="utf-8")

    main = stack_dir / "main.tf"
    main.write_text(PROXMOX_MAIN_TF, encoding="utf-8")

    versions = stack_dir / "versions.tf"
    versions.write_text(_versions_stub("proxmox"), encoding="utf-8")

    locals_path = stack_dir / "locals.tf"
    locals_path.write_text(
        _render_proxmox_locals(data, spaces, vm_hosts, lxc_hosts, routers),
        encoding="utf-8",
    )

    return [tfvars, variables, main, versions, locals_path]


def _render_proxmox_locals(
    data: dict[str, Any],
    spaces: dict[str, dict[str, Any]],
    vm_hosts: list[dict[str, Any]],
    lxc_hosts: list[dict[str, Any]],
    routers: list[dict[str, Any]],
) -> str:
    template_catalog = proxmox_template_catalog(data)
    aliases = template_catalog["aliases"]
    vm_template_ids = template_catalog["vm_templates"]
    lxc_templates = template_catalog["lxc_templates"]
    vm_config = {
        host["name"]: _proxmox_vm_config(host, spaces, aliases)
        for host in vm_hosts
    }
    bind_dns_by_space = _proxmox_lxc_bind_dns_by_space(lxc_hosts, spaces)
    lxc_config = {
        host["name"]: _proxmox_lxc_config(host, spaces, lxc_templates, bind_dns_by_space)
        for host in lxc_hosts
    }
    router_config = {
        host["name"]: _proxmox_router_config(host, spaces, aliases)
        for host in routers
    }
    return (
        "locals {\n"
        f"  vm_template_id = {to_hcl(vm_template_ids, 1)}\n\n"
        f"  vm_config = {to_hcl(vm_config, 1)}\n\n"
        f"  lxc_config = {to_hcl(lxc_config, 1)}\n\n"
        f"  router_config = {to_hcl(router_config, 1)}\n"
        "}\n"
    )


def _proxmox_vm_config(host: dict[str, Any], spaces: dict[str, dict[str, Any]], templates: dict[str, str]) -> dict[str, Any]:
    resources = host.get("resources") or {}
    ip = str(host.get("ip"))
    space = spaces[str(host.get("space"))]
    template = templates.get(str(host.get("template")), str(host.get("template")))
    os_name = str(host.get("os") or "").lower()
    template_name = str(host.get("template") or "")
    is_linux = "ubuntu" in os_name or template_name.startswith("ubuntu")
    is_macos = "macos" in os_name or template_name.startswith("macos-")
    is_windows11 = "windows11" in os_name or "windows 11" in os_name or template_name.startswith("win11")
    proxmox = host.get("proxmox") or {}
    config = {
        "name": _proxmox_resource_name(host),
        "desc": f"{host.get('hostname')} - {host.get('os', 'vm')} - {ip}",
        "cores": resources.get("cpu", 2),
        "memory": resources.get("memory_mb", 4096),
        "clone": template,
        "dns": str(space.get("dns") or space.get("gateway")),
        "ip": f"{ip}/{ipaddress.ip_network(str(space['cidr']), strict=False).prefixlen}",
        "gateway": space["gateway"],
        "vlan": space["vlan"],
        "cpu_type": "host",
        "os_type": "other" if is_macos else ("l26" if is_linux else ("win11" if is_windows11 else "win10")),
        "agent_enabled": not (is_macos or is_linux),
        "initialization_enabled": not is_macos,
        "clone_datastore": "${var.storage}",
    }
    if isinstance(proxmox, dict) and proxmox.get("clone_datastore"):
        config["clone_datastore"] = str(proxmox["clone_datastore"])
    if isinstance(proxmox, dict) and "agent_enabled" in proxmox:
        config["agent_enabled"] = bool(proxmox["agent_enabled"])
    if is_macos:
        config.update(
            {
                "bios": "ovmf",
                "machine": "q35",
                "scsi_hardware": "virtio-scsi-pci",
                "cpu_type": "host",
                "mac_address": _proxmox_macos_mac(host),
            }
        )
    elif is_windows11:
        config.update(
            {
                "bios": "ovmf",
                "machine": "pc-q35-11.0",
                "scsi_hardware": "virtio-scsi-single",
            }
        )
    if isinstance(proxmox, dict):
        for key, value in proxmox.items():
            if key in {"bios", "machine", "scsi_hardware", "cpu_type", "os_type"}:
                config[key] = str(value)
    if disk_gb := resources.get("disk_gb"):
        config["disk"] = {"interface": "scsi0" if is_linux else "virtio0", "size": disk_gb}
    if is_linux:
        ansible_vars = (host.get("ansible") or {}).get("vars") or {}
        config["user_account"] = {
            "username": ansible_vars.get("ansible_user", "ubuntu"),
            "password": ansible_vars.get("ansible_password"),
            "keys": "${[trimspace(tls_private_key.lxc_ssh.public_key_openssh)]}",
        }
    if host.get("name") == "mail":
        config["disk"] = {"interface": "sata0", "size": 120, "file_format": "raw"}
        config["cdrom"] = {
            "file_id": "pve-truenas:iso/SW_DVD9_Exchange_Svr_2019_CU_9_MultiLang_Std_Ent_.iso_MLF_X22-61078.ISO"
        }
    return config


def _proxmox_macos_mac(host: dict[str, Any]) -> str:
    proxmox = host.get("proxmox") or {}
    if isinstance(proxmox, dict) and proxmox.get("mac_address"):
        return str(proxmox["mac_address"])
    digest = hashlib.sha256(str(host.get("name") or host.get("hostname") or "macos").encode("utf-8")).digest()
    return f"00:1B:63:{digest[0]:02X}:{digest[1]:02X}:{digest[2]:02X}"


def _proxmox_lxc_bind_dns_by_space(
    lxc_hosts: list[dict[str, Any]],
    spaces: dict[str, dict[str, Any]],
) -> dict[str, str]:
    bind_dns_by_space: dict[str, str] = {}
    for host in lxc_hosts:
        if host.get("name") == "bind" and host.get("space") in spaces and host.get("ip"):
            bind_dns_by_space[str(host["space"])] = str(host["ip"])
    return bind_dns_by_space


def _proxmox_lxc_dns(host: dict[str, Any], space: dict[str, Any], bind_dns_by_space: dict[str, str]) -> str:
    proxmox = host.get("proxmox") or {}
    if isinstance(proxmox, dict) and proxmox.get("dns"):
        return str(proxmox["dns"])
    if host.get("dns"):
        return str(host["dns"])
    if host.get("name") == "bind":
        return str(space.get("dns") or space.get("gateway"))
    return str(bind_dns_by_space.get(str(host.get("space"))) or space.get("dns") or space.get("gateway"))


def _proxmox_lxc_config(
    host: dict[str, Any],
    spaces: dict[str, dict[str, Any]],
    templates: dict[str, str],
    bind_dns_by_space: dict[str, str],
) -> dict[str, Any]:
    resources = host.get("resources") or {}
    ip = str(host.get("ip"))
    space = spaces[str(host.get("space"))]
    config = {
        "name": _proxmox_resource_name(host),
        "desc": f"{host.get('hostname')} - {host.get('os', 'lxc')} - {ip}",
        "cores": resources.get("cpu", 2),
        "memory": resources.get("memory_mb", 1024),
        "ostemplate": templates.get(str(host.get("template")), str(host.get("template"))),
        "dns": _proxmox_lxc_dns(host, space, bind_dns_by_space),
        "ip": f"{ip}/{ipaddress.ip_network(str(space['cidr']), strict=False).prefixlen}",
        "gateway": space["gateway"],
        "storage": "${var.lxc_storage}",
        "rootfs_size": resources.get("rootfs_gb", 8),
        "root_password": f"${{var.lxc_passwords[\"{host.get('name')}\"]}}",
        "nesting": True,
        "vlan": space["vlan"],
    }
    if host.get("name") == "sliver":
        config["secondary_interface"] = {"disable_vlan": True}
    return config


def _lxc_root_password(host: dict[str, Any]) -> str:
    ansible_vars = (host.get("ansible") or {}).get("vars") or {}
    password = ansible_vars.get("ansible_password")
    if password:
        return str(password)
    base = str(host.get("hostname") or host.get("name") or "lxc")
    safe = "".join(char for char in base if char.isalnum()) or "Lxc"
    return f"{safe}-Password!"


def _proxmox_router_config(host: dict[str, Any], spaces: dict[str, dict[str, Any]], templates: dict[str, str]) -> dict[str, Any]:
    resources = host.get("resources") or {}
    networks = []
    for network in host.get("networks") or []:
        space = spaces[str(network["space"])]
        networks.append({"space": network["space"], "vlan": space["vlan"], "ip": network["ip"]})
    return {
        "name": _proxmox_resource_name(host),
        "desc": f"{host.get('hostname')} router - {host.get('ip')}",
        "cores": resources.get("cpu", 2),
        "memory": resources.get("memory_mb", 2048),
        "clone": templates.get(str(host.get("template")), str(host.get("template"))),
        "storage": "${var.pfsense_storage}",
        "networks": networks,
    }


def _proxmox_resource_name(host: dict[str, Any]) -> Any:
    proxmox = host.get("proxmox") or {}
    if isinstance(proxmox, dict) and proxmox.get("name"):
        return proxmox["name"]
    return host.get("proxmox_name") or host.get("hostname") or host.get("name")


def to_hcl(value: Any, indent: int = 0) -> str:
    pad = "  " * indent
    next_pad = "  " * (indent + 1)
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        for key, item in value.items():
            key_s = str(key)
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key_s):
                hcl_key = key_s
            else:
                hcl_key = json.dumps(key_s)
            lines.append(f"{next_pad}{hcl_key} = {to_hcl(item, indent + 1)}")
        lines.append(f"{pad}}}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = ["["]
        for item in value:
            lines.append(f"{next_pad}{to_hcl(item, indent + 1)},")
        lines.append(f"{pad}]")
        return "\n".join(lines)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return value[2:-1]
    if value is None:
        return "null"
    return json.dumps(value)


def render_tart(data: dict[str, Any], out_dir: Path) -> list[Path]:
    hosts = [host for host in data.get("hosts") or [] if host.get("owner") == "tart"]
    path = out_dir / "manifest.yml"
    path.write_text(yaml.safe_dump({"range": range_name(data), "hosts": hosts}, sort_keys=True), encoding="utf-8")
    return [path]


def render_docs(data: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    doc_path = Path(data.get("docs", {}).get("output") or out_dir / f"{range_name(data)}.md")
    if not doc_path.is_absolute():
        base_dir = range_base_dir(data)
        if base_dir.resolve() == RANGES_DIR.resolve():
            doc_path = ROOT / doc_path
        else:
            doc_path = base_dir / doc_path
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(generate_markdown(data), encoding="utf-8")
    return doc_path


def generate_markdown(data: dict[str, Any]) -> str:
    name = range_name(data)
    lines = [
        f"# Range: {name}",
        "",
        data.get("metadata", {}).get("description", ""),
        "",
        "## Spaces",
        "",
        "| Category | Name | CIDR | Gateway | VLAN |",
        "| --- | --- | --- | --- | --- |",
    ]
    for category, entries in (data.get("spaces") or {}).items():
        for entry in entries or []:
            lines.append(
                f"| {category} | {entry.get('name')} | {entry.get('cidr')} | {entry.get('gateway')} | {entry.get('vlan')} |"
            )

    scenario = data.get("scenario") or {}
    reminders = scenario.get("reminders") if isinstance(scenario, dict) else []
    if reminders:
        lines.extend([
            "",
            "## Scenario Notes",
            "",
        ])
        storyline = scenario.get("storyline")
        if storyline:
            lines.extend([f"Storyline: `{storyline}`", ""])
        for reminder in reminders:
            lines.append(f"- {reminder}")

    lines.extend([
        "",
        "## Hosts",
        "",
        "| Name | Hostname | Owner | Type | Space | IP | Groups |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ])
    for host in data.get("hosts") or []:
        groups = ", ".join((host.get("ansible") or {}).get("groups") or [])
        lines.append(
            f"| {host.get('name')} | {host.get('hostname')} | {host.get('owner')} | {host.get('type')} | {host.get('space', '')} | {host.get('ip', '')} | {groups} |"
        )

    docs = data.get("docs") or {}
    extra_markdown = docs.get("extra_markdown")
    if isinstance(extra_markdown, str) and extra_markdown.strip():
        lines.extend(["", extra_markdown.strip()])

    lines.extend([
        "",
        "## Ownership",
        "",
        "| Owner | Hosts |",
        "| --- | --- |",
    ])
    by_owner: dict[str, list[str]] = defaultdict(list)
    for host in data.get("hosts") or []:
        by_owner[str(host.get("owner"))].append(str(host.get("name")))
    for owner, hosts in sorted(by_owner.items()):
        lines.append(f"| {owner} | {', '.join(sorted(hosts))} |")

    lines.extend([
        "",
        "## Network Diagram",
        "",
        "```mermaid",
        "flowchart LR",
    ])
    for category, entries in (data.get("spaces") or {}).items():
        for entry in entries or []:
            space = entry.get("name")
            lines.append(f'  subgraph {space}["{category}: {space} {entry.get("cidr")}"]')
            for host in data.get("hosts") or []:
                if host.get("space") == space:
                    node = str(host.get("name")).replace("-", "_")
                    lines.append(f'    {node}["{host.get("name")}<br/>{host.get("ip", "")}"]')
            lines.append("  end")
    lines.extend(["```", ""])
    return "\n".join(lines)


def _tofu_stub(provider: str) -> str:
    return f'''# Generated by rangectl.
# This stack is intentionally a stub until the {provider} module is implemented.

variable "range_name" {{
  type = string
}}

variable "provider_config" {{
  type = any
}}

variable "spaces" {{
  type = any
}}

variable "hosts" {{
  type = any
}}

output "range_name" {{
  value = var.range_name
}}

output "host_names" {{
  value = [for host in var.hosts : host.name]
}}
'''


def _versions_stub(provider: str) -> str:
    if provider == "proxmox":
        return '''terraform {
  required_version = ">= 1.6.0"
  required_providers {
    proxmox = {
      source = "bpg/proxmox"
    }
    tls = {
      source = "hashicorp/tls"
    }
    local = {
      source = "hashicorp/local"
    }
  }
}
'''

    if provider == "aws":
        return '''terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
    tls = {
      source = "hashicorp/tls"
    }
    local = {
      source = "hashicorp/local"
    }
    archive = {
      source = "hashicorp/archive"
    }
  }
}
'''

    provider_name = {
        "proxmox": "proxmox",
        "aws": "aws",
        "azure": "azurerm",
    }.get(provider, provider)
    source = {
        "proxmox": "bpg/proxmox",
        "aws": "hashicorp/aws",
        "azurerm": "hashicorp/azurerm",
    }.get(provider_name, "")
    provider_block = ""
    if source:
        provider_block = f'''
    {provider_name} = {{
      source = "{source}"
    }}
'''
    return f'''terraform {{
  required_version = ">= 1.6.0"
  required_providers {{{provider_block}  }}
}}
'''


PROXMOX_VARIABLES_TF = '''variable "pm_node" {
  type = string
}

variable "pm_pool" {
  type    = string
  default = "rangectl"
}

variable "pm_full_clone" {
  type    = bool
  default = true
}

variable "pm_insecure" {
  type    = bool
  default = false
}

variable "storage" {
  type = string
}

variable "pfsense_storage" {
  type = string
}

variable "lxc_storage" {
  type    = string
  default = "local-lvm"
}

variable "network_bridge" {
  type    = string
  default = "vmbr0"
}

variable "network_span_bridge" {
  type    = string
  default = "vmbr1"
}

variable "network_model" {
  type    = string
  default = "virtio"
}

variable "lxc_passwords" {
  type      = map(string)
  sensitive = true
  default   = {}
}

variable "range_name" {
  type = string
}
'''


AWS_VARIABLES_TF = '''variable "range_name" {
  type = string
}

variable "provider_config" {
  type    = any
  default = {}
}

variable "scenario" {
  type    = any
  default = {}
}

variable "hosts" {
  type    = any
  default = []
}
'''


AWS_MAIN_TF = '''provider "aws" {
  region = try(var.provider_config.region, null)

  default_tags {
    tags = local.default_tags
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  scenario_name       = try(var.scenario.scenario, "cloud-lab")
  normalized_scenario = replace(local.scenario_name, "_", "-")
  name_prefix         = "${var.range_name}-${local.normalized_scenario}"
  default_tags = merge(
    {
      Project   = var.range_name
      ManagedBy = "rangectl"
      Range     = var.range_name
      Scenario  = local.scenario_name
    },
    try(var.provider_config.default_tags, {})
  )

  vpc_cidr           = try(var.scenario.vpc.cidr, "10.80.0.0/24")
  public_subnet_cidr = try(var.scenario.vpc.public_subnet_cidr, "10.80.0.0/25")
  availability_zone  = try(var.scenario.vpc.availability_zone, "${data.aws_region.current.id}a")
  admin_cidrs        = try(var.scenario.allowed_admin_cidrs, [])

  bucket_prefix = try(var.scenario.bucket.name_prefix, "${local.name_prefix}-files")
  bucket_name   = lower(substr("${local.bucket_prefix}-${data.aws_caller_identity.current.account_id}", 0, 63))
  s3_objects = try(var.scenario.s3_objects, {
    "README.txt" = {
      content = "Range cloud sync notes. Look for deployment details in app/deployment.txt.\\n"
    }
    "app/deployment.txt" = {
      content = "Cloud app host is managed by EC2 instance profile credentials. Check instance metadata from the app host.\\n"
    }
  })

  workstation_user_name       = try(var.scenario.workstation_user.name, "${var.range_name}-workstation-user")
  app_role_name               = try(var.scenario.instance.role_name, "${local.name_prefix}-app-role")
  app_profile_name            = try(var.scenario.instance.profile_name, "${local.name_prefix}-app-profile")
  instance_name               = try(var.scenario.instance.name, "${local.name_prefix}-app01")
  instance_type               = try(var.scenario.instance.instance_type, "t3.micro")
  secret_name                 = try(var.scenario.secret.name, "/${var.range_name}/cloud-app")
  secret_value                = try(var.scenario.secret.value, "rangectl-cloud-placeholder")
  lambda_name                 = try(var.scenario.lambda.name, "${local.name_prefix}-helper")
  kubernetes_secret_name      = try(var.scenario.kubernetes_secret.name, "${var.range_name}-aws-workstation-creds")
  kubernetes_secret_namespace = try(var.scenario.kubernetes_secret.namespace, "default")
}

resource "aws_vpc" "scenario" {
  cidr_block           = local.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${local.name_prefix}-vpc"
  }
}

resource "aws_internet_gateway" "scenario" {
  vpc_id = aws_vpc.scenario.id

  tags = {
    Name = "${local.name_prefix}-igw"
  }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.scenario.id
  cidr_block              = local.public_subnet_cidr
  availability_zone       = local.availability_zone
  map_public_ip_on_launch = true

  tags = {
    Name = "${local.name_prefix}-public"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.scenario.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.scenario.id
  }

  tags = {
    Name = "${local.name_prefix}-public"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "app" {
  name        = "${local.name_prefix}-app"
  description = "Range cloud app host"
  vpc_id      = aws_vpc.scenario.id

  dynamic "ingress" {
    for_each = local.admin_cidrs
    content {
      description = "lab admin SSH"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = [ingress.value]
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-app"
  }
}

resource "aws_s3_bucket" "files" {
  bucket        = local.bucket_name
  force_destroy = true

  tags = {
    Name = local.bucket_name
  }
}

resource "aws_s3_bucket_public_access_block" "files" {
  bucket                  = aws_s3_bucket.files.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "seed" {
  for_each = local.s3_objects

  bucket  = aws_s3_bucket.files.id
  key     = each.key
  content = try(each.value.content, "")
  tags    = try(each.value.tags, {})
}

resource "aws_iam_user" "workstation" {
  name = local.workstation_user_name

  tags = {
    Name = local.workstation_user_name
  }
}

resource "aws_iam_access_key" "workstation" {
  user = aws_iam_user.workstation.name
}

resource "local_sensitive_file" "kubernetes_aws_credentials_secret" {
  filename = "${path.module}/aws-workstation-creds-secret.yaml"
  content = yamlencode({
    apiVersion = "v1"
    kind       = "Secret"
    metadata = {
      name      = local.kubernetes_secret_name
      namespace = local.kubernetes_secret_namespace
      labels = {
        "app.kubernetes.io/managed-by" = "rangectl"
        "rangectl.dev/scenario"        = local.scenario_name
      }
    }
    type = "Opaque"
    stringData = {
      AWS_ACCESS_KEY_ID     = aws_iam_access_key.workstation.id
      AWS_SECRET_ACCESS_KEY = aws_iam_access_key.workstation.secret
      AWS_DEFAULT_REGION    = data.aws_region.current.id
      AWS_S3_BUCKET         = aws_s3_bucket.files.bucket
    }
  })
  file_permission = "0600"
}

resource "aws_iam_user_policy" "workstation_s3_read" {
  name = "${local.workstation_user_name}-s3-read"
  user = aws_iam_user.workstation.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.files.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.files.arn}/*"
      }
    ]
  })
}

resource "aws_secretsmanager_secret" "cloud_app" {
  name                    = local.secret_name
  recovery_window_in_days = 0

  tags = {
    Name = local.secret_name
  }
}

resource "aws_secretsmanager_secret_version" "cloud_app" {
  secret_id     = aws_secretsmanager_secret.cloud_app.id
  secret_string = local.secret_value
}

resource "aws_iam_role" "app" {
  name = local.app_role_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = local.app_role_name
  }
}

resource "aws_iam_role_policy" "app_secret_read" {
  name = "${local.app_role_name}-secret-read"
  role = aws_iam_role.app.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ]
        Resource = aws_secretsmanager_secret.cloud_app.arn
      }
    ]
  })
}

resource "aws_iam_instance_profile" "app" {
  name = local.app_profile_name
  role = aws_iam_role.app.name

  tags = {
    Name = local.app_profile_name
  }
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "tls_private_key" "app" {
  algorithm = "ED25519"
}

resource "aws_key_pair" "app" {
  key_name   = "${local.name_prefix}-app"
  public_key = tls_private_key.app.public_key_openssh

  tags = {
    Name = "${local.name_prefix}-app"
  }
}

resource "local_sensitive_file" "app_private_key" {
  filename        = "${path.module}/ssh_keys/${local.name_prefix}-app"
  content         = tls_private_key.app.private_key_openssh
  file_permission = "0600"
}

resource "aws_instance" "app" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = local.instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.app.id]
  key_name                    = aws_key_pair.app.key_name
  iam_instance_profile        = aws_iam_instance_profile.app.name
  associate_public_ip_address = true

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  user_data = <<-EOF
#!/bin/bash
set -eux
apt-get update
apt-get install -y awscli jq
mkdir -p /opt/rangectl
cat >/opt/rangectl/README.txt <<'EON'
This host has an EC2 instance profile. Use IMDSv2 to inspect temporary role credentials.
The next scenario secret is in AWS Secrets Manager.
EON
EOF

  tags = {
    Name = local.instance_name
  }
}

resource "aws_iam_role" "lambda" {
  name = "${local.lambda_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = "${local.lambda_name}-role"
  }
}

resource "aws_iam_role_policy" "lambda_logs" {
  name = "${local.lambda_name}-logs"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "*"
      }
    ]
  })
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda/rangectl_helper.py"
  output_path = "${path.module}/lambda/rangectl_helper.zip"
}

resource "aws_lambda_function" "helper" {
  function_name    = local.lambda_name
  role             = aws_iam_role.lambda.arn
  handler          = "rangectl_helper.handler"
  runtime          = "python3.12"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  environment {
    variables = {
      SECRET_ARN = aws_secretsmanager_secret.cloud_app.arn
      BUCKET     = aws_s3_bucket.files.bucket
    }
  }

  tags = {
    Name = local.lambda_name
  }
}

output "bucket_name" {
  value = aws_s3_bucket.files.bucket
}

output "instance_public_ip" {
  value = aws_instance.app.public_ip
}

output "instance_private_key_path" {
  value = local_sensitive_file.app_private_key.filename
}

output "secret_arn" {
  value = aws_secretsmanager_secret.cloud_app.arn
}

output "workstation_access_key_id" {
  value = aws_iam_access_key.workstation.id
}

output "workstation_secret_access_key" {
  value     = aws_iam_access_key.workstation.secret
  sensitive = true
}

output "kubernetes_aws_credentials_secret_path" {
  value = local_sensitive_file.kubernetes_aws_credentials_secret.filename
}
'''


AWS_LAMBDA_HELPER_PY = '''import json
import os


def handler(event, context):
    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "bucket": os.environ.get("BUCKET"),
                "secret_arn": os.environ.get("SECRET_ARN"),
                "message": "rangectl helper scaffold",
            }
        ),
    }
'''


PROXMOX_MAIN_TF = '''provider "proxmox" {
  insecure = var.pm_insecure
}

resource "tls_private_key" "lxc_ssh" {
  algorithm = "ED25519"
}

resource "local_sensitive_file" "lxc_private_key" {
  filename        = "${path.module}/ssh_keys/lxc_ssh"
  content         = tls_private_key.lxc_ssh.private_key_openssh
  file_permission = "0600"
}

resource "local_file" "lxc_public_key" {
  filename        = "${path.module}/ssh_keys/lxc_ssh.pub"
  content         = tls_private_key.lxc_ssh.public_key_openssh
  file_permission = "0644"
}

resource "proxmox_virtual_environment_vm" "vm" {
  for_each = local.vm_config

  name        = each.value.name
  description = each.value.desc
  node_name   = var.pm_node
  pool_id     = var.pm_pool
  stop_on_destroy = true
  bios        = try(each.value.bios, null)
  machine     = try(each.value.machine, null)
  scsi_hardware = try(each.value.scsi_hardware, null)

  operating_system {
    type = coalesce(try(each.value.os_type, null), "win10")
  }

  cpu {
    cores   = each.value.cores
    sockets = 1
    type    = coalesce(try(each.value.cpu_type, null), "host")
  }

  memory {
    dedicated = each.value.memory
  }

  clone {
    vm_id   = lookup(local.vm_template_id, each.value.clone, -1)
    full    = var.pm_full_clone
    retries = 2
    datastore_id = try(each.value.clone_datastore, null)
  }

  dynamic "disk" {
    for_each = try(each.value.disk, null) == null ? [] : [each.value.disk]
    content {
      datastore_id = try(disk.value.datastore_id, var.storage)
      interface    = disk.value.interface
      size         = disk.value.size
      file_format  = try(disk.value.file_format, null)
    }
  }

  dynamic "cdrom" {
    for_each = try(each.value.cdrom, null) == null ? [] : [each.value.cdrom]
    content {
      file_id = cdrom.value.file_id
    }
  }

  dynamic "efi_disk" {
    for_each = try(each.value.efi_disk, null) == null ? [] : [each.value.efi_disk]
    content {
      datastore_id      = efi_disk.value.datastore_id
      file_format       = try(efi_disk.value.file_format, null)
      pre_enrolled_keys = try(efi_disk.value.pre_enrolled_keys, null)
      type              = try(efi_disk.value.type, null)
    }
  }

  dynamic "tpm_state" {
    for_each = try(each.value.tpm_state, null) == null ? [] : [each.value.tpm_state]
    content {
      datastore_id = tpm_state.value.datastore_id
      version      = try(tpm_state.value.version, null)
    }
  }

  agent {
    enabled = coalesce(try(each.value.agent_enabled, null), true)
  }

  network_device {
    bridge      = var.network_bridge
    mac_address = try(each.value.mac_address, null)
    model       = var.network_model
    vlan_id     = each.value.vlan
  }

  dynamic "initialization" {
    for_each = try(each.value.initialization_enabled, true) ? [each.value] : []
    content {
      datastore_id = coalesce(try(initialization.value.clone_datastore, null), var.storage)
      dns {
        servers = [initialization.value.dns]
      }
      dynamic "user_account" {
        for_each = try(initialization.value.user_account, null) == null ? [] : [initialization.value.user_account]
        content {
          keys     = try(user_account.value.keys, null)
          username = user_account.value.username
          password = try(user_account.value.password, null)
        }
      }
      ip_config {
        ipv4 {
          address = initialization.value.ip
          gateway = initialization.value.gateway
        }
      }
    }
  }
}

resource "proxmox_virtual_environment_vm" "router" {
  for_each = local.router_config

  name        = each.value.name
  description = each.value.desc
  node_name   = var.pm_node
  pool_id     = var.pm_pool
  on_boot     = true

  clone {
    vm_id        = lookup(local.vm_template_id, each.value.clone, -1)
    full         = true
    datastore_id = try(each.value.storage, null)
  }

  operating_system {
    type = "other"
  }

  cpu {
    cores   = each.value.cores
    sockets = 1
    type    = "host"
  }

  memory {
    dedicated = each.value.memory
  }

  dynamic "network_device" {
    for_each = each.value.networks
    content {
      bridge  = var.network_bridge
      model   = var.network_model
      vlan_id = network_device.value.vlan
    }
  }

  network_device {
    bridge = var.network_span_bridge
    model  = var.network_model
  }

  lifecycle {
    ignore_changes = [
      clone,
    ]
  }
}

resource "proxmox_virtual_environment_container" "lxc" {
  for_each = local.lxc_config

  node_name     = var.pm_node
  pool_id       = var.pm_pool
  description   = each.value.desc
  unprivileged  = true
  start_on_boot = true

  operating_system {
    template_file_id = each.value.ostemplate
    type             = "ubuntu"
  }

  cpu {
    cores = each.value.cores
  }

  memory {
    dedicated = each.value.memory
  }

  disk {
    datastore_id = each.value.storage
    size         = each.value.rootfs_size
  }

  initialization {
    hostname = each.value.name
    dns {
      servers = [each.value.dns]
    }
    user_account {
      password = each.value.root_password
      keys     = [trimspace(tls_private_key.lxc_ssh.public_key_openssh)]
    }
    ip_config {
      ipv4 {
        address = each.value.ip
        gateway = each.value.gateway
      }
    }
  }

  network_interface {
    name    = "eth0"
    bridge  = var.network_bridge
    vlan_id = each.value.vlan
  }

  dynamic "network_interface" {
    for_each = try(each.value.secondary_interface, null) == null ? [] : [each.value.secondary_interface]
    content {
      name   = "eth1"
      bridge = try(network_interface.value.bridge, var.network_bridge)
      vlan_id = (
        coalesce(try(network_interface.value.disable_vlan, null), false)
        ? null
        : try(network_interface.value.vlan, each.value.vlan)
      )
    }
  }

  features {
    nesting = each.value.nesting
  }

  lifecycle {
    ignore_changes = all
  }
}

output "range_name" {
  value = var.range_name
}

output "vm_names" {
  value = [for _, host in local.vm_config : host.name]
}

output "lxc_names" {
  value = [for _, host in local.lxc_config : host.name]
}

output "router_names" {
  value = [for _, host in local.router_config : host.name]
}
'''
