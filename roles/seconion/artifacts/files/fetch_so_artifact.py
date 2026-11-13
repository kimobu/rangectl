#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import ssl
import tempfile
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _open(opener: urllib.request.OpenerDirector, request: urllib.request.Request, timeout: int):
    return opener.open(request, timeout=timeout)


def _json_get(opener: urllib.request.OpenerDirector, url: str, timeout: int) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with _open(opener, request, timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _login(opener: urllib.request.OpenerDirector, base_url: str, username: str, password: str, timeout: int) -> None:
    login_start = urllib.parse.urljoin(base_url, "/auth/self-service/login/browser")
    request = urllib.request.Request(login_start, headers={"Accept": "text/html"})
    with _open(opener, request, timeout) as response:
        final_url = response.geturl()

    flow = urllib.parse.parse_qs(urllib.parse.urlparse(final_url).query).get("flow", [""])[0]
    if not flow:
        raise SystemExit(f"Security Onion login flow did not return a flow id: {final_url}")

    flow_url = urllib.parse.urljoin(base_url, f"/auth/self-service/login/flows?id={urllib.parse.quote(flow)}")
    flow_data = _json_get(opener, flow_url, timeout)
    action = flow_data.get("ui", {}).get("action")
    csrf_token = ""
    for node in flow_data.get("ui", {}).get("nodes", []):
        attrs = node.get("attributes", {})
        if attrs.get("name") == "csrf_token":
            csrf_token = attrs.get("value") or ""
            break
    if not action or not csrf_token:
        raise SystemExit("Security Onion login flow did not expose action/csrf_token")

    body = urllib.parse.urlencode(
        {
            "identifier": username,
            "password": password,
            "method": "password",
            "csrf_token": csrf_token,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        action,
        data=body,
        headers={
            "Accept": "text/html,application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": base_url.rstrip("/"),
            "Referer": urllib.parse.urljoin(base_url, f"/login/?flow={urllib.parse.quote(flow)}"),
        },
        method="POST",
    )
    with _open(opener, request, timeout) as response:
        response.read(1024)


def _validate_artifact(path: Path, min_size: int) -> None:
    if path.stat().st_size < min_size:
        raise SystemExit(f"Downloaded artifact is too small: {path.stat().st_size} bytes")
    head = path.read_bytes()[:512].lstrip()
    lowered = head.lower()
    if lowered.startswith(b"<!doctype html") or lowered.startswith(b"<html") or b"<title>security onion</title>" in lowered:
        raise SystemExit("Downloaded artifact looks like HTML, not an Elastic Agent installer")
    if b"login" in lowered and b"security onion" in lowered:
        raise SystemExit("Downloaded artifact looks like a login page, not an Elastic Agent installer")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a Security Onion package artifact with optional browser-flow auth.")
    parser.add_argument("--url", default="")
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--env-file", default="~/.seconion.env")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--validate-certs", action="store_true")
    parser.add_argument("--min-size", type=int, default=1_000_000)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    if args.validate_only:
        _validate_artifact(args.dest, args.min_size)
        print(f"validated {args.dest}")
        return 0
    if not args.url:
        raise SystemExit("--url is required unless --validate-only is set")

    _load_env_file(Path(args.env_file).expanduser())
    username = os.environ.get("SECONION_PACKAGE_USERNAME") or os.environ.get("SECURITYONION_USERNAME")
    password = os.environ.get("SECONION_PACKAGE_PASSWORD") or os.environ.get("SECURITYONION_PASSWORD")

    cookie_jar = CookieJar()
    handlers: list[urllib.request.BaseHandler] = [urllib.request.HTTPCookieProcessor(cookie_jar)]
    if not args.validate_certs:
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    opener = urllib.request.build_opener(*handlers)

    parsed = urllib.parse.urlparse(args.url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    if username and password:
        _login(opener, base_url, username, password, args.timeout)

    args.dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{args.dest.name}.", suffix=".tmp", dir=str(args.dest.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        request = urllib.request.Request(args.url, headers={"Accept": "application/octet-stream"})
        with _open(opener, request, args.timeout) as response, temp_path.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        _validate_artifact(temp_path, args.min_size)
        temp_path.chmod(0o755)
        temp_path.replace(args.dest)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    print(f"fetched {args.dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
