from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import hmac
import io
import os
import secrets
import sys
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .scenario_state import _safe_document_name

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_APPEND_BYTES = 1024 * 1024


class ScenarioStateHTTPServer(ThreadingHTTPServer):
    state_root: Path
    bearer_token: str


class ScenarioStateHandler(BaseHTTPRequestHandler):
    server: ScenarioStateHTTPServer

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/healthz":
            self._reply(HTTPStatus.OK, b'{"status":"ok"}\n', "application/json")
            return
        path = self._authorized_document_path()
        if path is None:
            return
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            self._reply(HTTPStatus.NOT_FOUND, b"not found\n")
            return
        except OSError:
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, b"read failed\n")
            return
        self._reply(HTTPStatus.OK, content, _content_type(path.name))

    def do_PUT(self) -> None:
        path = self._authorized_document_path()
        if path is None:
            return
        content = self._read_body(MAX_DOCUMENT_BYTES)
        if content is None:
            return
        content = self._decode_content(content, MAX_DOCUMENT_BYTES)
        if content is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except UnboundLocalError:
                pass
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, b"write failed\n")
            return
        self._reply(HTTPStatus.NO_CONTENT, b"")

    def do_POST(self) -> None:
        path = self._authorized_document_path()
        if path is None:
            return
        if not path.name.endswith(".jsonl"):
            self._reply(HTTPStatus.METHOD_NOT_ALLOWED, b"append requires a .jsonl document\n")
            return
        content = self._read_body(MAX_APPEND_BYTES)
        if content is None:
            return
        content = self._decode_content(content, MAX_APPEND_BYTES)
        if content is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("ab") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            path.chmod(0o600)
        except OSError:
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, b"append failed\n")
            return
        self._reply(HTTPStatus.NO_CONTENT, b"")

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _authorized_document_path(self) -> Path | None:
        parts = [unquote(part) for part in urlsplit(self.path).path.split("/") if part]
        if len(parts) != 5 or parts[:2] != ["v1", "state"]:
            self._reply(HTTPStatus.NOT_FOUND, b"not found\n")
            return None
        range_name, host, name = parts[2:]
        if not _safe_segment(range_name) or not _safe_segment(host):
            self._reply(HTTPStatus.BAD_REQUEST, b"invalid range or host\n")
            return None
        supplied = self.headers.get("Authorization", "")
        administrative = f"Bearer {self.server.bearer_token}"
        endpoint = f"Bearer {derive_scoped_token(self.server.bearer_token, range_name, host)}"
        if not (
            secrets.compare_digest(supplied, administrative)
            or secrets.compare_digest(supplied, endpoint)
        ):
            self._reply(HTTPStatus.UNAUTHORIZED, b"unauthorized\n")
            return None
        try:
            document = _safe_document_name(name)
        except ValueError:
            self._reply(HTTPStatus.BAD_REQUEST, b"invalid document name\n")
            return None
        return self.server.state_root / range_name / host / document

    def _read_body(self, maximum: int) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reply(HTTPStatus.BAD_REQUEST, b"invalid content length\n")
            return None
        if length < 0 or length > maximum:
            self._reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"document too large\n")
            return None
        content = self.rfile.read(length)
        if len(content) != length:
            self._reply(HTTPStatus.BAD_REQUEST, b"incomplete request body\n")
            return None
        return content

    def _decode_content(self, content: bytes, maximum: int) -> bytes | None:
        encoding = self.headers.get("Content-Encoding", "").strip().lower()
        if not encoding:
            return content
        if encoding != "gzip":
            self._reply(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, b"unsupported content encoding\n")
            return None
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as handle:
                decoded = handle.read(maximum + 1)
        except (EOFError, OSError):
            self._reply(HTTPStatus.BAD_REQUEST, b"invalid gzip content\n")
            return None
        if len(decoded) > maximum:
            self._reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"document too large\n")
            return None
        return decoded

    def _reply(self, status: HTTPStatus, content: bytes, content_type: str = "text/plain") -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if content:
            self.wfile.write(content)


def create_server(listen: str, port: int, root: Path, token: str) -> ScenarioStateHTTPServer:
    if len(token) < 32:
        raise ValueError("scenario state server token must contain at least 32 characters")
    root.mkdir(parents=True, exist_ok=True)
    server = ScenarioStateHTTPServer((listen, port), ScenarioStateHandler)
    server.state_root = root.resolve()
    server.bearer_token = token
    return server


def derive_scoped_token(master_token: str, range_name: str, host: str) -> str:
    if not _safe_segment(range_name) or not _safe_segment(host):
        raise ValueError("invalid range or host for scoped token")
    scope = f"{range_name}:{host}".encode("utf-8")
    return hmac.new(master_token.encode("utf-8"), scope, hashlib.sha256).hexdigest()


def _safe_segment(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and all(ch.isalnum() or ch in {"-", "_", "."} for ch in value)


def _content_type(name: str) -> str:
    return "application/x-ndjson" if name.endswith(".jsonl") else "application/json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rangectl-activity-state-server")
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        token = args.token_file.read_text(encoding="utf-8").strip()
        server = create_server(args.listen, args.port, args.root, token)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
