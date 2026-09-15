#!/usr/bin/env python3
"""Local Canvas + personal to-do bridge for the CYD dashboard."""

from __future__ import annotations

import json
import hmac
import logging
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
TODOS_PATH = ROOT / "todos.json"
TODO_ROUTE = re.compile(r"^/api/todos/([a-zA-Z0-9-]+)/toggle$")


def render_home() -> tuple[str, str]:
    """Render the web UI with a per-response Content Security Policy nonce."""
    nonce = secrets.token_urlsafe(18)
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    html = html.replace("__CSP_NONCE__", nonce)
    policy = (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    )
    return html, policy


def _url_origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urllib.parse.urlsplit(url)
        if not parsed.scheme or not parsed.hostname:
            return None
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        return parsed.scheme.lower(), parsed.hostname.lower(), port
    except ValueError:
        return None


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_origin: tuple[str, str, int] | None) -> None:
        super().__init__()
        self.allowed_origin = allowed_origin

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        target = urllib.parse.urljoin(req.full_url, newurl)
        if _url_origin(target) != self.allowed_origin:
            raise urllib.error.HTTPError(
                req.full_url, code, "Canvas redirect changed API origin", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, target)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(ROOT / ".env")


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


class TodoStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return self._read()

    def _write(self, todos: list[dict[str, Any]]) -> None:
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(todos, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def add(self, title: str, due_at: str | None) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise ValueError("title is required")
        if len(title) > 160:
            raise ValueError("title must be 160 characters or fewer")
        if due_at and not parse_datetime(due_at):
            raise ValueError("due_at must be an ISO 8601 date/time")
        todo = {"id": str(uuid.uuid4()), "title": title, "due_at": due_at or None,
                "completed": False, "created_at": now_utc().isoformat()}
        with self.lock:
            todos = self._read()
            todos.append(todo)
            self._write(todos)
        return todo

    def toggle(self, todo_id: str) -> dict[str, Any] | None:
        with self.lock:
            todos = self._read()
            for todo in todos:
                if todo.get("id") == todo_id:
                    todo["completed"] = not bool(todo.get("completed"))
                    self._write(todos)
                    return todo
        return None


class CanvasClient:
    def __init__(self) -> None:
        self.base_url = setting("CANVAS_BASE_URL").rstrip("/")
        self.token = setting("CANVAS_TOKEN")
        self.cache_seconds = int(setting("CANVAS_CACHE_SECONDS", "300"))
        self.lookahead_days = int(setting("CANVAS_LOOKAHEAD_DAYS", "45"))
        self.cached_at = 0.0
        self.cached_items: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.base_origin = self._origin(self.base_url) if self.base_url else None
        if self.base_url and (
            self.base_origin is None or self.base_origin[0] != "https"
        ):
            raise ValueError("CANVAS_BASE_URL must be a valid HTTPS URL")
        self.opener = urllib.request.build_opener(
            _SameOriginRedirectHandler(self.base_origin)
        )

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int] | None:
        return _url_origin(url)

    def _require_same_origin(self, url: str) -> None:
        if self._origin(url) != self.base_origin:
            raise ValueError("Canvas response attempted to change API origin")

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    @staticmethod
    def _next_link(header: str) -> str:
        for part in header.split(","):
            match = re.match(r'\s*<([^>]+)>;\s*rel="([^"]+)"', part)
            if match and match.group(2) == "next":
                return match.group(1)
        return ""

    def _get_all(self, path: str, params: dict[str, str]) -> list[Any]:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        results: list[Any] = []
        while url:
            self._require_same_origin(url)
            request = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json+canvas-string-ids",
                "User-Agent": "CYD-Focus-Dashboard/1.0",
            })
            with self.opener.open(request, timeout=12) as response:
                payload = json.load(response)
                results.extend(payload if isinstance(payload, list) else [payload])
                url = self._next_link(response.headers.get("Link", ""))
        return results

    def get_items(self) -> list[dict[str, Any]]:
        if not self.configured:
            return []
        with self.lock:
            if time.monotonic() - self.cached_at < self.cache_seconds:
                return list(self.cached_items)
            courses = self._get_all("/api/v1/courses", {"enrollment_state": "active", "per_page": "100"})
            names = {str(c.get("id")): str(c.get("course_code") or c.get("name") or "Course")
                     for c in courses if c.get("id") is not None}
            current = now_utc()
            raw_items = self._get_all("/api/v1/planner/items", {
                "start_date": (current - timedelta(days=1)).isoformat(),
                "end_date": (current + timedelta(days=self.lookahead_days)).isoformat(),
                "filter": "incomplete_items", "per_page": "100",
            })
            normalized = []
            for item in raw_items:
                plan = item.get("plannable") or {}
                title = plan.get("title") or plan.get("name") or item.get("plannable_type", "Canvas item").replace("_", " ").title()
                due_at = plan.get("due_at") or plan.get("todo_date") or plan.get("end_at") or plan.get("start_at")
                course_id = str(item.get("course_id") or plan.get("course_id") or "")
                context = item.get("context_name") or plan.get("context_name") or names.get(course_id) or "Canvas"
                normalized.append({
                    "id": f"canvas-{item.get('plannable_type', 'item')}-{item.get('plannable_id', '')}",
                    "source": "canvas", "title": str(title), "context": str(context),
                    "due_at": due_at, "url": item.get("html_url", ""),
                })
            self.cached_items, self.cached_at = normalized, time.monotonic()
            return list(normalized)


class DemoCanvasClient(CanvasClient):
    @property
    def configured(self) -> bool:
        return True

    def get_items(self) -> list[dict[str, Any]]:
        current = now_utc()
        return [
            {"id": "demo-math", "source": "canvas", "title": "Problem Set 4",
             "context": "MATH 221", "due_at": (current + timedelta(hours=5)).isoformat(), "url": ""},
            {"id": "demo-history", "source": "canvas", "title": "Read chapter 7",
             "context": "HIST 110", "due_at": (current + timedelta(days=2)).isoformat(), "url": ""},
        ]


class DashboardService:
    def __init__(self, todos: TodoStore, canvas: CanvasClient, tz: ZoneInfo) -> None:
        self.todos, self.canvas, self.tz = todos, canvas, tz

    def due_label(self, due_at: str | None) -> str:
        due = parse_datetime(due_at)
        if not due:
            return "No due date"
        local_due, local_now = due.astimezone(self.tz), now_utc().astimezone(self.tz)
        delta = (local_due.date() - local_now.date()).days
        clock = local_due.strftime("%I:%M %p").lstrip("0")
        if delta < 0: return "OVERDUE"
        if delta == 0: return f"Today {clock}"
        if delta == 1: return f"Tomorrow {clock}"
        if delta < 7: return f"{local_due.strftime('%a')} {clock}"
        return f"{local_due.strftime('%b')} {local_due.day}"

    def build(self) -> dict[str, Any]:
        current, items = now_utc(), self.canvas.get_items()
        for todo in self.todos.list():
            if not todo.get("completed"):
                items.append({"id": todo.get("id"), "source": "personal", "title": todo.get("title", "Untitled task"),
                              "context": "Personal", "due_at": todo.get("due_at"), "url": ""})
        far_future = datetime.max.replace(tzinfo=timezone.utc)
        items.sort(key=lambda x: (0 if parse_datetime(x.get("due_at")) else 1,
                                  parse_datetime(x.get("due_at")) or far_future, x["title"].lower()))
        for item in items:
            due = parse_datetime(item.get("due_at"))
            item["due_label"] = self.due_label(item.get("due_at"))
            item["urgent"] = bool(due and due <= current + timedelta(hours=24))
        return {
            "generated_at": current.isoformat(),
            "updated_label": current.astimezone(self.tz).strftime("%I:%M %p").lstrip("0"),
            "counts": {"canvas": sum(x["source"] == "canvas" for x in items),
                       "personal": sum(x["source"] == "personal" for x in items)},
            "items": items[:20],
        }


class RequestHandler(SimpleHTTPRequestHandler):
    server_version = "CYDDashboard/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    @property
    def app(self) -> "DashboardServer":
        return self.server  # type: ignore[return-value]

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-API-Key", "")
        return bool(self.app.api_key and supplied) and hmac.compare_digest(
            supplied, self.app.api_key
        )

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        super().end_headers()

    def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 16_384:
            raise ValueError("request body must be 16 KB or less")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in {"/", "/index.html"}:
            body_text, policy = render_home()
            body = body_text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", policy)
            self.end_headers()
            self.wfile.write(body)
        elif path == "/health":
            self._json({"ok": True})
        elif path == "/api/dashboard":
            if not self._authorized():
                self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return
            try:
                self._json(self.app.service.build())
            except (urllib.error.URLError, TimeoutError, ValueError) as error:
                logging.warning("Canvas request failed: %s", type(error).__name__)
                self._json({"error": "Canvas service unavailable"}, HTTPStatus.BAD_GATEWAY)
        elif path == "/api/todos":
            if not self._authorized():
                self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return
            self._json(self.app.service.todos.list())
        else:
            super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if not self._authorized():
            self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            payload = self._read_json()
            if path == "/api/todos":
                todo = self.app.service.todos.add(str(payload.get("title", "")), payload.get("due_at"))
                self._json(todo, HTTPStatus.CREATED)
                return
            match = TODO_ROUTE.match(path)
            if match:
                todo = self.app.service.todos.toggle(match.group(1))
                self._json(todo if todo else {"error": "todo not found"},
                           HTTPStatus.OK if todo else HTTPStatus.NOT_FOUND)
                return
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (json.JSONDecodeError, ValueError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: DashboardService, api_key: str) -> None:
        super().__init__(address, RequestHandler)
        self.service, self.api_key = service, api_key


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main() -> None:
    host, port = setting("DASHBOARD_HOST", "0.0.0.0"), int(setting("DASHBOARD_PORT", "8787"))
    demo_mode = setting("DEMO_MODE", "0").lower() in {"1", "true", "yes"}
    canvas: CanvasClient = DemoCanvasClient() if demo_mode else CanvasClient()
    if not canvas.configured:
        raise SystemExit("Canvas is not configured. Fill bridge/.env, or set DEMO_MODE=1.")
    service = DashboardService(TodoStore(TODOS_PATH), canvas, ZoneInfo(setting("DASHBOARD_TIMEZONE", "America/Denver")))
    server = DashboardServer((host, port), service, setting("DASHBOARD_API_KEY"))
    print(f"CYD dashboard bridge: http://{lan_ip()}:{port}")
    print(f"Device endpoint:       http://{lan_ip()}:{port}/api/dashboard")
    print(f"Mode:                  {'DEMO' if demo_mode else 'CANVAS'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
