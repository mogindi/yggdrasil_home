"""Out-of-tree PasteDeploy middleware for Yggdrasil RGPU flavors and VMs."""

from __future__ import annotations

import base64
import configparser
import hmac
import io
import json
import re
from typing import Any, Callable
from urllib.parse import unquote

from .heat import HeatAuthorizationError, HeatAuthorizationUnavailable, HeatReader


# Nova's v2.1 API uses unscoped resource URLs. Keep the optional project
# segment for deployments and proxies that still expose the legacy form.
SERVER_PATH = re.compile(r"/(?:v2(?:\.1)?)/(?:[^/]+/)?servers(?:/([^/?]+))?")
FLAVOR_PATH = re.compile(r"/(?:v2(?:\.1)?)/(?:[^/]+/)?flavors(?:/([^/?]+))?")


def _csv(value: str) -> set[str]:
    return {part.strip() for part in value.split(",") if part.strip()}


def _body(environ: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    raw = environ.get("wsgi.input", io.BytesIO()).read(length) if length else b""
    environ["wsgi.input"] = io.BytesIO(raw)
    if raw:
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = {}
    else:
        payload = {}
    return raw, payload if isinstance(payload, dict) else {}


def _json_error(status: str, message: str) -> tuple[str, list[tuple[str, str]], bytes]:
    code = int(status.split(" ", 1)[0])
    key = "forbidden" if code == 403 else "itemNotFound" if code == 404 else "error"
    body = json.dumps({key: {"message": message, "code": code}}).encode()
    return status, [("Content-Type", "application/json"), ("Content-Length", str(len(body)))], body


class RgpuGuardMiddleware:
    def __init__(self, app: Callable[..., Any], config: dict[str, str]):
        self.app = app
        self.enabled = config.get("enabled", "false").lower() in {"true", "yes", "1"}
        self.flavor_ids = _csv(config.get("rgpu_flavor_ids", ""))
        self.flavor_names = {name.lower() for name in _csv(config.get("rgpu_flavor_names", ""))}
        self.fcos_image_id = config.get("fcos_image_id", "").strip()
        self.fcos_image_name = config.get("fcos_image_name", "").strip()
        self.trusted_header = config.get(
            "trusted_heat_header", "X-Yggdrasil-Heat-Authorization"
        ).strip()
        self.trusted_secret = config.get("trusted_heat_secret", "").strip()
        self.owner_metadata_key = config.get(
            "owner_metadata_key", "yggdrasil_rgpu_owner_stack_id"
        ).strip()
        self.managed_server_name_suffix = config.get(
            "managed_server_name_suffix", "-remote-model-vm"
        ).strip()
        self.heat = None
        if self.enabled:
            try:
                password = base64.b64decode(
                    config.get("reader_password_b64", ""), validate=True
                ).decode()
            except (ValueError, UnicodeDecodeError) as error:
                raise RuntimeError("Invalid RGPU reader password configuration") from error
            self.heat = HeatReader(
                auth_url=config.get("keystone_url", "").strip(),
                heat_url=config.get("heat_url", "").strip(),
                username=config.get("reader_username", "").strip(),
                password=password,
                user_domain=config.get("reader_user_domain", "Default").strip(),
                project_domain=config.get("reader_project_domain", "Default").strip(),
                timeout=float(config.get("heat_timeout", "5")),
            approved_template_directory=config.get(
                "approved_template_directory",
                "/etc/nova/yggdrasil-approved-templates",
            ).strip(),
            )
            if not (self.flavor_ids and self.flavor_names and self.fcos_image_id and self.trusted_secret):
                raise RuntimeError("Incomplete RGPU guard configuration")

    @staticmethod
    def _context(environ: dict[str, Any]) -> Any:
        return environ.get("nova.context")

    def _is_platform_admin(self, environ: dict[str, Any]) -> bool:
        context = self._context(environ)
        if context is None:
            return False
        scope = str(getattr(context, "system_scope", "") or "").lower()
        roles = {str(role).lower() for role in (getattr(context, "roles", None) or [])}
        return scope == "all" and "admin" in roles

    def _is_trusted_heat(self, environ: dict[str, Any]) -> bool:
        if not self.trusted_secret:
            return False
        header = "HTTP_" + self.trusted_header.upper().replace("-", "_")
        supplied = str(environ.get(header, ""))
        return bool(supplied) and hmac.compare_digest(supplied, self.trusted_secret)

    @staticmethod
    def _project_id(environ: dict[str, Any]) -> str:
        context = environ.get("nova.context")
        return str(getattr(context, "project_id", "") or "")

    def _is_rgpu_flavor(self, value: Any) -> bool:
        if isinstance(value, dict):
            return any(
                self._is_rgpu_flavor(candidate)
                for candidate in (value.get("id"), value.get("name"))
                if candidate is not None
            )
        text = str(value or "").strip()
        return text in self.flavor_ids or text.lower() in self.flavor_names

    def _call_inner(
        self, environ: dict[str, Any]
    ) -> tuple[str, list[tuple[str, str]], bytes]:
        response: dict[str, Any] = {}

        def capture(status: str, headers: list[tuple[str, str]], _exc_info=None):
            response["status"] = status
            response["headers"] = headers

        iterable = self.app(environ, capture)
        try:
            body = b"".join(iterable)
        finally:
            close = getattr(iterable, "close", None)
            if close:
                close()
        return response["status"], response["headers"], body

    @staticmethod
    def _send(
        start_response: Callable[..., Any],
        result: tuple[str, list[tuple[str, str]], bytes],
    ):
        status, headers, body = result
        headers = [(key, value) for key, value in headers if key.lower() != "content-length"]
        headers.append(("Content-Length", str(len(body))))
        start_response(status, headers)
        return [body]

    def _lookup_server(self, environ: dict[str, Any], server_id: str) -> dict[str, Any] | None:
        clone = environ.copy()
        clone["REQUEST_METHOD"] = "GET"
        clone["PATH_INFO"] = re.sub(
            r"/servers/[^/]+(?:/.*)?$", "/servers/" + server_id, environ.get("PATH_INFO", "")
        )
        clone["QUERY_STRING"] = ""
        clone["CONTENT_LENGTH"] = "0"
        clone["wsgi.input"] = io.BytesIO()
        status, _headers, raw = self._call_inner(clone)
        if not status.startswith("200"):
            return None
        try:
            server = json.loads(raw).get("server")
        except (ValueError, AttributeError):
            return None
        return server if isinstance(server, dict) else None

    def _is_managed_server(self, server: dict[str, Any]) -> bool:
        metadata = server.get("metadata") or {}
        if self.owner_metadata_key in metadata:
            return True
        flavor = server.get("flavor") or {}
        if self._is_rgpu_flavor(flavor):
            return True
        name = str(server.get("name", ""))
        return bool(self.managed_server_name_suffix) and name.endswith(
            self.managed_server_name_suffix
        )

    def _validate_create(self, environ: dict[str, Any], payload: dict[str, Any]) -> bool:
        if not self._is_trusted_heat(environ):
            return False
        server = payload.get("server") or {}
        hints = payload.get("os:scheduler_hints") or payload.get("scheduler_hints") or {}
        if not isinstance(server, dict) or not isinstance(hints, dict) or self.heat is None:
            return False
        try:
            return self.heat.validate_create(
                project_id=self._project_id(environ),
                hints=hints,
                server=server,
                fcos_image_id=self.fcos_image_id,
                fcos_image_name=self.fcos_image_name,
                rgpu_flavor_ids=self.flavor_ids | self.flavor_names,
            )
        except HeatAuthorizationUnavailable:
            raise
        except HeatAuthorizationError:
            return False

    def _validate_existing(self, environ: dict[str, Any], server_id: str) -> bool:
        if not self._is_trusted_heat(environ) or self.heat is None:
            return False
        server = self._lookup_server(environ, server_id)
        if not server or not self._is_managed_server(server):
            return False
        metadata = server.get("metadata") or {}
        owner_id = str(metadata.get(self.owner_metadata_key, ""))
        try:
            return self.heat.validate_existing(
                project_id=self._project_id(environ),
                root_id=owner_id,
                server_id=server_id,
            )
        except HeatAuthorizationUnavailable:
            raise
        except HeatAuthorizationError:
            return False

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]):
        if not self.enabled or self._is_platform_admin(environ):
            return self.app(environ, start_response)

        path = unquote(environ.get("PATH_INFO", ""))
        method = environ.get("REQUEST_METHOD", "GET").upper()
        trusted_heat = self._is_trusted_heat(environ)

        if not trusted_heat:
            flavor_match = FLAVOR_PATH.search(path)
            if flavor_match and method == "GET":
                flavor_id = unquote(flavor_match.group(1) or "")
                if not flavor_id or flavor_id == "detail":
                    result = self._call_inner(environ)
                    try:
                        payload = json.loads(result[2])
                    except (ValueError, UnicodeDecodeError):
                        payload = None
                    if isinstance(payload, dict) and isinstance(payload.get("flavors"), list):
                        payload["flavors"] = [
                            flavor
                            for flavor in payload["flavors"]
                            if not self._is_rgpu_flavor(flavor)
                        ]
                        result = (result[0], result[1], json.dumps(payload).encode())
                    return self._send(start_response, result)
                if self._is_rgpu_flavor(flavor_id):
                    return self._send(
                        start_response,
                        _json_error("404 Not Found", "Flavor not found"),
                    )

        server_match = SERVER_PATH.search(path)
        if server_match:
            server_id = unquote(server_match.group(1) or "")
            if (server_id == "detail" or not server_id) and method == "GET":
                result = self._call_inner(environ)
                try:
                    payload = json.loads(result[2])
                except (ValueError, UnicodeDecodeError):
                    payload = None
                if isinstance(payload, dict) and isinstance(payload.get("servers"), list):
                    payload["servers"] = [
                        server
                        for server in payload["servers"]
                        if not self._is_managed_server(server)
                    ]
                    result = (result[0], result[1], json.dumps(payload).encode())
                return self._send(start_response, result)

            if server_id and method == "GET":
                server_path_end = server_match.end() if server_match else len(path)
                if path[server_path_end:].strip("/"):
                    server = self._lookup_server(environ, server_id)
                    if server and self._is_managed_server(server):
                        return self._send(
                            start_response,
                            _json_error("404 Not Found", "Server not found"),
                        )
                    return self.app(environ, start_response)
                result = self._call_inner(environ)
                try:
                    payload = json.loads(result[2])
                    server = payload.get("server") if isinstance(payload, dict) else None
                except (ValueError, UnicodeDecodeError):
                    server = None
                if isinstance(server, dict) and self._is_managed_server(server):
                    result = _json_error("404 Not Found", "Server not found")
                return self._send(start_response, result)

            if server_id and method != "GET" and not trusted_heat:
                server = self._lookup_server(environ, server_id)
                if server and self._is_managed_server(server):
                    return self._send(
                        start_response,
                        _json_error("404 Not Found", "Server not found"),
                    )

        if method == "POST" and path.rstrip("/").endswith("/servers"):
            _raw, payload = _body(environ)
            server = payload.get("server") or {}
            flavor = server.get("flavorRef", server.get("flavor_ref", "")) if isinstance(server, dict) else ""
            if self._is_rgpu_flavor(flavor):
                try:
                    allowed = self._validate_create(environ, payload)
                except HeatAuthorizationUnavailable:
                    return self._send(
                        start_response,
                        _json_error("503 Service Unavailable", "Heat authorization is unavailable"),
                    )
                if not allowed:
                    return self._send(
                        start_response,
                        _json_error("403 Forbidden", "RGPU flavors are reserved for approved remote-model stacks"),
                    )

        if method == "POST" and path.rstrip("/").endswith("/action") and server_match:
            server_id = unquote(server_match.group(1) or "")
            _raw, payload = _body(environ)
            action = payload.get("resize") or payload.get("rebuild")
            target_flavor = action.get("flavorRef", action.get("flavor_ref", "")) if isinstance(action, dict) else ""
            current = self._lookup_server(environ, server_id)
            current_managed = bool(current and self._is_managed_server(current))
            guarded = current_managed or self._is_rgpu_flavor(target_flavor)
            if guarded:
                try:
                    allowed = trusted_heat and self._validate_existing(environ, server_id)
                except HeatAuthorizationUnavailable:
                    return self._send(
                        start_response,
                        _json_error("503 Service Unavailable", "Heat authorization is unavailable"),
                    )
                if not allowed:
                    return self._send(
                        start_response,
                        _json_error("403 Forbidden", "RGPU-managed servers may only be changed by their approved Heat stack"),
                    )

        return self.app(environ, start_response)


def filter_factory(global_conf: dict[str, str], **local_conf: str):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read("/etc/nova/nova.conf")
    section = "yggdrasil_rgpu_guard"
    config = dict(parser.items(section)) if parser.has_section(section) else {}
    config.update(local_conf)

    def factory(app):
        return RgpuGuardMiddleware(app, config)

    return factory
