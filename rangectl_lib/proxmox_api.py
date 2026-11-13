from __future__ import annotations

import json
import os
import ssl
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request


class ProxmoxError(Exception):
    pass


@dataclass
class ProxmoxClient:
    endpoint: str
    username: str | None = None
    password: str | None = None
    api_token: str | None = None
    verify_tls: bool = True
    ca_file: str | None = None

    def __post_init__(self) -> None:
        self.endpoint = self.endpoint.rstrip("/")
        self._ticket: str | None = None
        self._csrf: str | None = None
        self._ssl_context = (
            ssl.create_default_context(cafile=self.ca_file)
            if self.verify_tls
            else ssl._create_unverified_context()
        )

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "ProxmoxClient":
        endpoint = env.get("PROXMOX_VE_ENDPOINT")
        if not endpoint:
            raise ProxmoxError("PROXMOX_VE_ENDPOINT is not set")
        api_token = env.get("PROXMOX_VE_API_TOKEN")
        username = env.get("PROXMOX_VE_USERNAME")
        password = env.get("PROXMOX_VE_PASSWORD")
        if not api_token and not (username and password):
            raise ProxmoxError("Proxmox auth needs PROXMOX_VE_API_TOKEN or PROXMOX_VE_USERNAME/PROXMOX_VE_PASSWORD")
        insecure = env.get("PROXMOX_VE_INSECURE", "").strip().lower() in {"1", "true", "yes", "on"}
        ca_file = env.get("PROXMOX_VE_CA_FILE")
        return cls(
            endpoint=endpoint,
            username=username,
            password=password,
            api_token=api_token,
            verify_tls=not insecure,
            ca_file=os.path.expanduser(ca_file) if ca_file else None,
        )

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, payload)

    def put(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        return self._request("PUT", path, payload)

    def delete(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        return self._request("DELETE", path, payload)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if self.api_token is None and self._ticket is None:
            self._login()
        url = f"{self.endpoint}/api2/json/{path.lstrip('/')}"
        data = None
        headers: dict[str, str] = {"Accept": "application/json"}
        if payload is not None and method == "DELETE":
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{parse.urlencode(payload)}"
        elif payload is not None:
            data = parse.urlencode(payload).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self.api_token:
            headers["Authorization"] = f"PVEAPIToken={self.api_token}"
        elif self._ticket:
            headers["Cookie"] = f"PVEAuthCookie={self._ticket}"
            if method != "GET" and self._csrf:
                headers["CSRFPreventionToken"] = self._csrf
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, context=self._ssl_context, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ProxmoxError(f"{method} {path} failed: HTTP {exc.code} {exc.reason}: {detail}") from exc
        except Exception as exc:
            raise ProxmoxError(f"{method} {path} failed: {exc}") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProxmoxError(f"{method} {path} returned invalid JSON") from exc
        return parsed.get("data")

    def _login(self) -> None:
        if not self.username or not self.password:
            raise ProxmoxError("username/password auth requested without both username and password")
        url = f"{self.endpoint}/api2/json/access/ticket"
        payload = parse.urlencode({"username": self.username, "password": self.password}).encode("utf-8")
        req = request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, context=self._ssl_context, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except Exception as exc:
            raise ProxmoxError(f"Proxmox login failed: {exc}") from exc
        parsed = json.loads(raw).get("data") or {}
        self._ticket = parsed.get("ticket")
        self._csrf = parsed.get("CSRFPreventionToken")
        if not self._ticket:
            raise ProxmoxError("Proxmox login did not return an auth ticket")
