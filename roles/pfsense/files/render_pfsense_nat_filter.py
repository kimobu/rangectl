#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import secrets
import sys
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import crypt


def _stable_int(name: str, digits: int = 10) -> int:
    h = hashlib.sha1(name.encode("utf-8")).hexdigest()
    return int(h[:digits], 16) % 2_000_000_000


def _stable_assoc_id(name: str) -> str:
    h = hashlib.sha1(name.encode("utf-8")).hexdigest()
    return f"rangectl_nat_{h[:12]}"


def _stable_refid(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:13]


def _add(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = text
    return el


def _ensure(parent: ET.Element, tag: str) -> ET.Element:
    el = parent.find(tag)
    if el is None:
        el = ET.SubElement(parent, tag)
    return el


def _remove_children_by_pred(parent: ET.Element, tag: str, pred) -> int:
    removed = 0
    for child in list(parent.findall(tag)):
        if pred(child):
            parent.remove(child)
            removed += 1
    return removed


def _interface_tag(index: int, interface: dict) -> str:
    descr = str(interface.get("descr", "")).strip().lower()
    if descr == "wan":
        return "wan"
    if index == 1:
        return "lan"
    return f"opt{index - 1}"


def _is_span_interface(interface: dict) -> bool:
    descr = str(interface.get("descr", "")).strip().lower()
    return "span" in descr or not interface.get("ip")


def _render_interfaces(root: ET.Element, interfaces: list[dict]) -> list[str]:
    if not interfaces:
        return []

    interfaces_el = _ensure(root, "interfaces")
    for child in list(interfaces_el):
        interfaces_el.remove(child)

    internal_tags: list[str] = []
    for idx, interface in enumerate(interfaces):
        tag = _interface_tag(idx, interface)
        entry = _add(interfaces_el, tag)
        _add(entry, "enable", "")
        _add(entry, "if", str(interface["if"]))

        ip = interface.get("ip")
        if ip:
            _add(entry, "ipaddr", str(ip))
            _add(entry, "subnet", str(interface.get("mask", 24)))
        else:
            _add(entry, "spoofmac", "")

        descr = _add(entry, "descr")
        descr.text = str(interface.get("descr") or tag.upper())

        if tag != "wan" and not _is_span_interface(interface):
            internal_tags.append(tag)

    return internal_tags


def _render_dhcp(root: ET.Element, interface_tags: list[str], dhcp_config: dict) -> None:
    for tag in ("dhcpd", "dhcpdv6"):
        dhcp = _ensure(root, tag)
        for child in list(dhcp):
            dhcp.remove(child)
        for iface_tag in interface_tags:
            iface = _add(dhcp, iface_tag)
            if tag == "dhcpdv6":
                continue
            iface_config = dhcp_config.get(iface_tag, {}) if isinstance(dhcp_config, dict) else {}
            if not iface_config or not iface_config.get("enabled"):
                continue
            _add(iface, "enable", "")
            lease_range = iface_config.get("range") or {}
            if lease_range.get("from") and lease_range.get("to"):
                range_el = _add(iface, "range")
                _add(range_el, "from", str(lease_range["from"]))
                _add(range_el, "to", str(lease_range["to"]))
            for key in (
                "failover_peerip",
                "defaultleasetime",
                "maxleasetime",
                "netmask",
                "gateway",
                "domain",
                "domainsearchlist",
                "ddnsdomain",
                "ddnsdomainprimary",
                "ddnsdomainsecondary",
                "ddnsdomainkeyname",
                "ddnsdomainkey",
                "mac_allow",
                "mac_deny",
                "tftp",
                "ldap",
                "nextserver",
                "filename",
                "filename32",
                "filename64",
                "filename32arm",
                "filename64arm",
                "rootpath",
                "numberoptions",
                "uefihttpboot",
            ):
                if key in iface_config:
                    _add(iface, key, str(iface_config.get(key) or ""))
            _add(iface, "ddnsdomainkeyalgorithm", str(iface_config.get("ddnsdomainkeyalgorithm") or "hmac-md5"))
            _add(iface, "ddnsclientupdates", str(iface_config.get("ddnsclientupdates") or "allow"))
            for dns_server in iface_config.get("dns_servers") or []:
                _add(iface, "dnsserver", str(dns_server))
            for static_map in iface_config.get("static_maps") or []:
                static_el = _add(iface, "staticmap")
                for key in ("mac", "cid", "ipaddr", "hostname", "descr", "filename", "rootpath", "defaultleasetime", "maxleasetime", "gateway", "domain"):
                    if static_map.get(key) is not None:
                        _add(static_el, key, str(static_map.get(key) or ""))
                for dns_server in static_map.get("dns_servers") or []:
                    _add(static_el, "dnsserver", str(dns_server))


def _render_gateways(root: ET.Element, gateways: list[dict]) -> int:
    if not gateways:
        return 0

    gateways_el = _ensure(root, "gateways")
    removed = _remove_children_by_pred(
        gateways_el,
        "gateway_item",
        lambda el: (
            el.findtext("name", default="").startswith("RANGECTL_")
            or el.findtext("descr", default="").startswith("rangectl ")
        ),
    )
    interfaces_el = _ensure(root, "interfaces")
    for gateway in gateways:
        if str(gateway.get("enabled", "true")).lower() == "false":
            continue
        name = str(gateway["name"])
        interface = str(gateway.get("interface") or "wan").lower()
        item = _add(gateways_el, "gateway_item")
        _add(item, "interface", interface)
        _add(item, "gateway", str(gateway["gateway"]))
        _add(item, "name", name)
        _add(item, "weight", str(gateway.get("weight") or 1))
        _add(item, "ipprotocol", str(gateway.get("ipprotocol") or "inet"))
        _add(item, "descr", str(gateway.get("descr") or f"rangectl {interface.upper()} gateway"))
        if gateway.get("default", True):
            _add(item, "defaultgw", "")
            if str(gateway.get("ipprotocol") or "inet") == "inet":
                _set_child_text(gateways_el, "defaultgw4", name)
            elif str(gateway.get("ipprotocol")) == "inet6":
                _set_child_text(gateways_el, "defaultgw6", name)
        iface_el = interfaces_el.find(interface)
        if iface_el is not None:
            _set_child_text(iface_el, "gateway", name)
    return removed


def _render_dns_overrides(root: ET.Element, overrides: list[dict]) -> int:
    unbound = _ensure(root, "unbound")
    removed = _remove_children_by_pred(
        unbound,
        "hosts",
        lambda el: (
            el.findtext("descr", default="").startswith("rangectl ")
            or any(
                el.findtext("host", default="") == str(item.get("host") or "").strip()
                and el.findtext("domain", default="") == str(item.get("domain") or "").strip()
                for item in overrides
            )
        ),
    )
    for item in overrides:
        if str(item.get("enabled", "true")).lower() == "false":
            continue
        host = str(item.get("host") or "").strip()
        domain = str(item.get("domain") or "").strip()
        ip = str(item.get("ip") or item.get("address") or "").strip()
        if not host or not domain or not ip:
            raise SystemExit(f"DNS override requires host, domain, and ip: {item}")
        entry = _add(unbound, "hosts")
        _add(entry, "host", host)
        _add(entry, "domain", domain)
        _add(entry, "ip", ip)
        _add(entry, "descr", str(item.get("descr") or f"rangectl DNS override {host}.{domain}"))
        aliases = _add(entry, "aliases")
        for alias in item.get("aliases") or []:
            alias_entry = _add(aliases, "item")
            _add(alias_entry, "host", str(alias.get("host") or ""))
            _add(alias_entry, "domain", str(alias.get("domain") or domain))
            _add(alias_entry, "description", str(alias.get("descr") or ""))
    return removed


def _ensure_admin_shell(root: ET.Element) -> None:
    system = _ensure(root, "system")
    for user in system.findall("user"):
        if user.findtext("name", default="").strip() == "admin":
            shell = _ensure(user, "shell")
            shell.text = "/bin/sh"
            return


def _ensure_ssh_enabled(root: ET.Element) -> None:
    system = _ensure(root, "system")
    _ensure(system, "ssh")
    _ensure(system, "enablesshd")


def _build_internal_allow_rule(iface_tag: str) -> ET.Element:
    flt = ET.Element("rule")
    _add(flt, "type", "pass")
    _add(flt, "interface", iface_tag)
    _add(flt, "ipprotocol", "inet")
    _add(flt, "statetype", "keep state")
    src = _add(flt, "source")
    _add(src, "network", iface_tag)
    dst = _add(flt, "destination")
    _add(dst, "any", "")
    name = f"rangectl_allow_{iface_tag}"
    descr = _add(flt, "descr")
    descr.text = f"rangectl allow outbound from {iface_tag}"
    _add(flt, "associated-rule-id", name)
    _add(flt, "tracker", str(_stable_int(name)))
    return flt


def _build_nat_rule(rule: dict) -> ET.Element:
    nat = ET.Element("rule")
    src = _add(nat, "source")
    _add(src, "any", "")

    dst = _add(nat, "destination")
    interface = str(rule["interface"]).lower()
    if interface == "wan":
        _add(dst, "network", "wanip")
    else:
        _add(dst, "address", str(rule["destination"]))
    _add(dst, "port", str(rule["external"]))

    _add(nat, "ipprotocol", "inet")
    _add(nat, "protocol", rule["proto"])
    _add(nat, "target", rule["target"])
    _add(nat, "local-port", str(rule["targetport"]))
    _add(nat, "interface", interface)
    descr = _add(nat, "descr")
    descr.text = rule.get("descr") or rule["name"]
    _add(nat, "associated-rule-id", _stable_assoc_id(rule["name"]))
    return nat


def _build_outbound_nat_rule(rule: dict) -> ET.Element:
    nat = ET.Element("rule")
    _add(nat, "sourceport", str(rule.get("sourceport") or ""))
    src = _add(nat, "source")
    _add(src, "network", str(rule["source"]))
    dst = _add(nat, "destination")
    _add(dst, "address", str(rule.get("destination") or "any"))
    _add(nat, "dstport", str(rule.get("dstport") or ""))
    _add(nat, "target", str(rule.get("target") or ""))
    _add(nat, "natport", str(rule.get("natport") or ""))
    _add(nat, "interface", str(rule.get("interface") or "wan").lower())
    _add(nat, "poolopts", str(rule.get("poolopts") or ""))
    _add(nat, "ipprotocol", str(rule.get("ipprotocol") or "inet"))
    _add(nat, "protocol", str(rule.get("protocol") or "any"))
    descr = _add(nat, "descr")
    descr.text = str(rule.get("descr") or rule.get("name") or "rangectl outbound NAT")
    _add(nat, "associated-rule-id", str(rule.get("id") or f"rangectl_outbound_{_stable_refid(descr.text or 'nat')}"))
    return nat


def _build_filter_rule(rule: dict) -> ET.Element:
    flt = ET.Element("rule")
    src = _add(flt, "source")
    _add(src, "any", "")
    _add(flt, "interface", str(rule["interface"]).lower())
    _add(flt, "ipprotocol", "inet")
    _add(flt, "protocol", rule["proto"])
    dst = _add(flt, "destination")
    _add(dst, "address", rule["target"])
    _add(dst, "port", str(rule["targetport"]))
    descr = _add(flt, "descr")
    descr.text = rule.get("descr") or rule["name"]
    _add(flt, "associated-rule-id", _stable_assoc_id(rule["name"]))
    _add(flt, "tracker", str(_stable_int(rule["name"])))
    created = _add(flt, "created")
    _add(created, "time", "1700000000")
    user = _add(created, "username")
    user.text = "NAT Port Forward"
    return flt


def _build_pass_rule(rule: dict) -> ET.Element:
    flt = ET.Element("rule")
    _add(flt, "type", "pass")
    _add(flt, "interface", str(rule.get("interface") or "wan").lower())
    _add(flt, "ipprotocol", str(rule.get("ipprotocol") or "inet"))
    _add(flt, "statetype", str(rule.get("statetype") or "keep state"))
    protocol = str(rule.get("proto") or rule.get("protocol") or "any")
    if protocol != "any":
        _add(flt, "protocol", protocol)

    src = _add(flt, "source")
    if rule.get("source_network"):
        _add(src, "network", str(rule["source_network"]))
    elif rule.get("source"):
        _add(src, "address", str(rule["source"]))
    else:
        _add(src, "any", "")

    dst = _add(flt, "destination")
    if rule.get("destination_network"):
        _add(dst, "network", str(rule["destination_network"]))
    elif rule.get("destination"):
        _add(dst, "address", str(rule["destination"]))
    else:
        _add(dst, "any", "")
    if rule.get("destination_port"):
        _add(dst, "port", str(rule["destination_port"]))

    name = str(rule.get("name") or rule.get("descr") or "managed-pass")
    descr = _add(flt, "descr")
    descr.text = str(rule.get("descr") or name)
    assoc = str(rule.get("id") or f"rangectl_filter_{_stable_refid(name)}")
    _add(flt, "associated-rule-id", assoc)
    _add(flt, "tracker", str(_stable_int(assoc)))
    return flt


def _pem_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() + "\n"


def _ensure_tls_auth_key(path: Path) -> None:
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


def _sha512_hash(password: str, username: str) -> str:
    salt = hashlib.sha256(username.encode("utf-8")).hexdigest()[:16]
    hashed = crypt.crypt(password, f"$6${salt}$")
    if not hashed:
        raise SystemExit("failed to generate pfSense local-user password hash")
    return hashed


def _set_child_text(parent: ET.Element, tag: str, text: str) -> None:
    existing = parent.find(tag)
    if existing is None:
        existing = ET.SubElement(parent, tag)
    existing.text = text


def _next_uid(system: ET.Element) -> int:
    used = []
    for user in system.findall("user"):
        try:
            used.append(int(user.findtext("uid", default="0")))
        except ValueError:
            continue
    try:
        configured_next = int(system.findtext("nextuid", default="2000"))
    except ValueError:
        configured_next = 2000
    return max([configured_next, *used, 1999]) + 1


def _render_local_user(root: ET.Element, username: str, password: str, descr: str) -> None:
    system = _ensure(root, "system")
    _remove_children_by_pred(system, "user", lambda el: el.findtext("name", default="") == username)
    uid = _next_uid(system)
    user = _add(system, "user")
    _add(user, "name", username)
    _add(user, "descr", descr)
    _add(user, "scope", "user")
    _add(user, "sha512-hash", _sha512_hash(password, username))
    _add(user, "uid", str(uid))
    _set_child_text(system, "nextuid", str(uid + 1))


def _render_ca(root: ET.Element, refid: str, descr: str, ca_crt: Path, ca_key: Path) -> None:
    _remove_children_by_pred(root, "ca", lambda el: el.findtext("refid", default="") == refid or el.findtext("descr", default="") == descr)
    ca = _add(root, "ca")
    _add(ca, "refid", refid)
    _add(ca, "descr", descr)
    _add(ca, "trust", "enabled")
    _add(ca, "randomserial", "disabled")
    _add(ca, "crt", _pem_b64(ca_crt))
    _add(ca, "prv", _pem_b64(ca_key))
    _add(ca, "serial", "0")


def _render_cert(root: ET.Element, refid: str, caref: str, descr: str, cert_crt: Path, cert_key: Path) -> None:
    _remove_children_by_pred(root, "cert", lambda el: el.findtext("refid", default="") == refid or el.findtext("descr", default="") == descr)
    cert = _add(root, "cert")
    _add(cert, "refid", refid)
    _add(cert, "descr", descr)
    _add(cert, "type", "server")
    _add(cert, "caref", caref)
    _add(cert, "crt", _pem_b64(cert_crt))
    _add(cert, "prv", _pem_b64(cert_key))


def _build_openvpn_server(openvpn: dict, ca_refid: str, cert_refid: str, tls_auth_key: Path) -> ET.Element:
    server = openvpn.get("server") if isinstance(openvpn.get("server"), dict) else {}
    routes = openvpn.get("routes") if isinstance(openvpn.get("routes"), list) else []
    route_text = ", ".join(str(route) for route in routes)
    proto = str(server.get("proto") or "udp").upper()
    if proto == "UDP":
        proto = "UDP4"
    elif proto == "TCP":
        proto = "TCP4"

    srv = ET.Element("openvpn-server")
    fields = [
        ("vpnid", str(openvpn.get("vpnid") or 1)),
        ("mode", "server_tls_user"),
        ("authmode", "Local Database"),
        ("protocol", proto),
        ("dev_mode", "tun"),
        ("interface", str(server.get("interface") or "wan")),
        ("ipaddr", ""),
        ("local_port", str(server.get("port") or 1194)),
        ("description", str(openvpn.get("description") or openvpn.get("name") or "Lab support VPN")),
        ("custom_options", ""),
        ("tls", base64.b64encode(_read_text(tls_auth_key).encode("utf-8")).decode("ascii")),
        ("tls_type", "auth"),
        ("tlsauth_keydir", "default"),
        ("caref", ca_refid),
        ("crlref", ""),
        ("ocspurl", ""),
        ("certref", cert_refid),
        ("dh_length", "2048"),
        ("ecdh_curve", "none"),
        ("cert_depth", "1"),
        ("strictusercn", ""),
        ("remote_cert_tls", "yes"),
        ("data_ciphers_fallback", "AES-256-CBC"),
        ("digest", "SHA256"),
        ("engine", "none"),
        ("tunnel_network", str(server.get("tunnel_cidr") or "10.250.40.0/24")),
        ("tunnel_networkv6", ""),
        ("remote_network", ""),
        ("remote_networkv6", ""),
        ("gwredir", ""),
        ("gwredir6", ""),
        ("local_network", route_text),
        ("local_networkv6", ""),
        ("maxclients", str(openvpn.get("maxclients") or 5)),
        ("allow_compression", "no"),
        ("compression", ""),
        ("compression_push", ""),
        ("passtos", ""),
        ("client2client", ""),
        ("dynamic_ip", "yes"),
        ("topology", "subnet"),
        ("serverbridge_dhcp", ""),
        ("serverbridge_interface", "none"),
        ("serverbridge_routegateway", ""),
        ("serverbridge_dhcp_start", ""),
        ("serverbridge_dhcp_end", ""),
        ("dns_server1", str(openvpn.get("dns_server") or "192.168.10.10")),
        ("dns_server2", ""),
        ("dns_server3", ""),
        ("dns_server4", ""),
        ("username_as_common_name", "disabled"),
        ("exit_notify", "1"),
        ("sndrcvbuf", ""),
        ("netbios_enable", ""),
        ("netbios_ntype", "0"),
        ("netbios_scope", ""),
        ("create_gw", "v4only"),
        ("verbosity_level", "1"),
        ("duplicate_cn", ""),
        ("data_ciphers", "AES-256-GCM,AES-128-GCM,CHACHA20-POLY1305"),
        ("ncp_enable", "enabled"),
        ("ping_method", "keepalive"),
        ("keepalive_interval", "10"),
        ("keepalive_timeout", "60"),
        ("ping_seconds", "10"),
        ("ping_push", ""),
        ("ping_action", "ping_restart"),
        ("ping_action_seconds", "60"),
        ("ping_action_push", ""),
        ("inactive_seconds", "300"),
    ]
    for tag, text in fields:
        _add(srv, tag, text)
    return srv


def _build_openvpn_wan_rule(openvpn: dict) -> ET.Element:
    server = openvpn.get("server") if isinstance(openvpn.get("server"), dict) else {}
    rule = ET.Element("rule")
    _add(rule, "type", "pass")
    _add(rule, "interface", str(server.get("interface") or "wan"))
    _add(rule, "ipprotocol", "inet")
    _add(rule, "protocol", str(server.get("proto") or "udp").lower())
    src = _add(rule, "source")
    _add(src, "any", "")
    dst = _add(rule, "destination")
    _add(dst, "network", "(self)")
    _add(dst, "port", str(server.get("port") or 1194))
    _add(rule, "descr", "rangectl OpenVPN listener")
    _add(rule, "associated-rule-id", "rangectl_openvpn_listener")
    _add(rule, "tracker", str(_stable_int("rangectl_openvpn_listener")))
    return rule


def _build_openvpn_route_rule(route: str) -> ET.Element:
    rule = ET.Element("rule")
    _add(rule, "type", "pass")
    _add(rule, "interface", "openvpn")
    _add(rule, "ipprotocol", "inet")
    src = _add(rule, "source")
    _add(src, "network", "openvpn")
    dst = _add(rule, "destination")
    _add(dst, "address", route)
    _add(rule, "descr", f"rangectl OpenVPN allow {route}")
    _add(rule, "associated-rule-id", f"rangectl_openvpn_route_{hashlib.sha1(route.encode('utf-8')).hexdigest()[:12]}")
    _add(rule, "tracker", str(_stable_int(f"rangectl_openvpn_route_{route}")))
    return rule


def _render_openvpn(root: ET.Element, flt: ET.Element, openvpn: dict) -> bool:
    if not isinstance(openvpn, dict) or not openvpn.get("enabled"):
        return False

    artifact_dir = Path(str(openvpn.get("artifact_dir") or "state/default/vpn"))
    pki_dir = artifact_dir / "pki"
    private_dir = pki_dir / "private"
    state_path = artifact_dir / "openvpn-state.json"
    artifact_slug = str(openvpn.get("artifact_slug") or artifact_dir.parent.name or "default")
    ca_crt = pki_dir / f"{artifact_slug}-openvpn-ca.crt"
    ca_key = private_dir / f"{artifact_slug}-openvpn-ca.key"
    server_crt = pki_dir / f"{artifact_slug}-openvpn-server.crt"
    server_key = private_dir / f"{artifact_slug}-openvpn-server.key"
    tls_auth_key = pki_dir / f"{artifact_slug}-openvpn-ta.key"
    required = [state_path, ca_crt, ca_key, server_crt, server_key]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit("OpenVPN rendering requires generated artifacts: " + ", ".join(missing))
    _ensure_tls_auth_key(tls_auth_key)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    clients = state.get("clients") if isinstance(state.get("clients"), dict) else {}
    client_name = str(openvpn.get("client_name") or "lab-vpn-client")
    client = clients.get(client_name) if isinstance(clients.get(client_name), dict) else {}
    username = str(client.get("username") or openvpn.get("username") or client_name)
    password = str(client.get("password") or "")
    if not password:
        raise SystemExit(f"OpenVPN client {client_name!r} is missing a generated password in {state_path}")

    ca_refid = _stable_refid("rangectl-openvpn-ca")
    cert_refid = _stable_refid("rangectl-openvpn-server")
    _render_ca(root, ca_refid, "rangectl OpenVPN CA", ca_crt, ca_key)
    _render_cert(root, cert_refid, ca_refid, "rangectl-openvpn-server", server_crt, server_key)
    _render_local_user(root, username, password, "rangectl support VPN")

    ovpn = _ensure(root, "openvpn")
    server = openvpn.get("server") if isinstance(openvpn.get("server"), dict) else {}
    target_port = str(server.get("port") or 1194)
    target_interface = str(server.get("interface") or "wan")
    _remove_children_by_pred(
        ovpn,
        "openvpn-server",
        lambda el: (
            el.findtext("description", default="").startswith("rangectl")
            or (el.findtext("local_port", default="") == target_port and el.findtext("interface", default="") == target_interface)
        ),
    )
    ovpn.append(_build_openvpn_server(openvpn, ca_refid, cert_refid, tls_auth_key))

    _remove_children_by_pred(
        flt,
        "rule",
        lambda el: el.findtext("associated-rule-id", default="").startswith("rangectl_openvpn_")
        or el.findtext("descr", default="").startswith("rangectl OpenVPN"),
    )
    flt.append(_build_openvpn_wan_rule(openvpn))
    routes = openvpn.get("routes") if isinstance(openvpn.get("routes"), list) else []
    for route in routes:
        flt.append(_build_openvpn_route_rule(str(route)))
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", required=True)
    ap.add_argument("--out", dest="out_path", required=True)
    ap.add_argument("--rules-json", dest="rules_json_path", required=True)
    args = ap.parse_args()

    with open(args.rules_json_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    interfaces = payload.get("pfsense_interfaces", [])
    gateways = payload.get("pfsense_gateways", [])
    openvpn = payload.get("pfsense_openvpn", {})
    inbound = payload.get("pfsense_inbound_nat", [])
    outbound = payload.get("pfsense_outbound_nat", [])
    filter_rules = payload.get("pfsense_filter_rules", [])
    dhcp_config = payload.get("pfsense_dhcp", {})
    dns_overrides = payload.get("pfsense_dns_overrides", [])
    managed = [
        r
        for r in inbound
        if (
            str(r.get("interface", "")).lower() == "wan"
            or (str(r.get("interface", "")).lower() != "wan" and r.get("destination"))
        )
        and str(r.get("enabled", "true")).lower() != "false"
    ]

    tree = ET.parse(args.in_path)
    root = tree.getroot()

    nat = _ensure(root, "nat")
    flt = _ensure(root, "filter")
    _ensure_admin_shell(root)
    _ensure_ssh_enabled(root)
    internal_interface_tags = _render_interfaces(root, interfaces)
    if internal_interface_tags:
        _render_dhcp(root, internal_interface_tags, dhcp_config)
    removed_gateways = _render_gateways(root, gateways)
    removed_dns_overrides = _render_dns_overrides(root, dns_overrides)

    removed_wan_nat = _remove_children_by_pred(
        nat,
        "rule",
        lambda el: (el.findtext("interface", default="").strip().lower() == "wan"),
    )
    removed_managed_nat = _remove_children_by_pred(
        nat,
        "rule",
        lambda el: el.findtext("associated-rule-id", default="").startswith("rangectl_nat_"),
    )
    managed_redirect_keys = {
        (
            str(r.get("interface", "")).lower(),
            str(r.get("destination") or "").strip(),
            str(r.get("external") or "").strip(),
            str(r.get("proto") or "").strip(),
        )
        for r in managed
        if str(r.get("interface", "")).lower() != "wan"
    }
    removed_legacy_managed_nat = _remove_children_by_pred(
        nat,
        "rule",
        lambda el: (
            (
                el.findtext("interface", default="").strip().lower(),
                el.findtext("destination/address", default="").strip(),
                el.findtext("destination/port", default="").strip(),
                el.findtext("protocol", default="").strip(),
            )
            in managed_redirect_keys
        ),
    )
    removed_filter = _remove_children_by_pred(
        flt,
        "rule",
        lambda el: (
            (
                el.findtext("interface", default="").strip().lower() == "wan"
                and el.findtext("associated-rule-id", default="").startswith("nat_")
            )
            or el.findtext("associated-rule-id", default="").startswith("rangectl_nat_")
            or el.findtext("associated-rule-id", default="").startswith("rangectl_filter_")
        ),
    )
    removed_allow = _remove_children_by_pred(
        flt,
        "rule",
        lambda el: el.findtext("associated-rule-id", default="").startswith("rangectl_allow_"),
    )

    missing_destination = [
        r.get("name")
        for r in managed
        if str(r.get("interface", "")).lower() != "wan" and not r.get("destination")
    ]
    if missing_destination:
        raise SystemExit(
            "LAN NAT rules require destination: "
            + ", ".join(str(item) for item in missing_destination)
        )

    # Ensure outbound + separator exist
    outbound_el = _ensure(nat, "outbound")
    mode_el = _ensure(outbound_el, "mode")
    if outbound:
        mode_el.text = "hybrid"
    elif not mode_el.text:
        mode_el.text = "automatic"
    removed_outbound_nat = _remove_children_by_pred(
        outbound_el,
        "rule",
        lambda el: (
            el.findtext("associated-rule-id", default="").startswith("rangectl_outbound_")
            or el.findtext("descr", default="").startswith("rangectl outbound NAT")
        ),
    )
    _ensure(nat, "separator")

    for r in outbound:
        if str(r.get("enabled", "true")).lower() != "false":
            outbound_el.append(_build_outbound_nat_rule(r))
    for r in managed:
        nat.append(_build_nat_rule(r))
        flt.append(_build_filter_rule(r))
    for r in filter_rules:
        if str(r.get("enabled", "true")).lower() != "false":
            flt.append(_build_pass_rule(r))
    for iface_tag in internal_interface_tags:
        flt.append(_build_internal_allow_rule(iface_tag))
    rendered_openvpn = _render_openvpn(root, flt, openvpn)

    tree.write(args.out_path, encoding="utf-8", xml_declaration=True)

    sys.stderr.write(
        f"managed_rules={len(managed)} removed_wan_nat={removed_wan_nat} "
        f"removed_managed_nat={removed_managed_nat} removed_legacy_managed_nat={removed_legacy_managed_nat} removed_filter_assoc={removed_filter} "
        f"removed_allow={removed_allow} removed_outbound_nat={removed_outbound_nat} "
        f"removed_gateways={removed_gateways} removed_dns_overrides={removed_dns_overrides} interfaces={len(interfaces)} "
        f"openvpn={int(rendered_openvpn)}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
