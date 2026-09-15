"""Azure Functions HTTP entry points for the CYD dashboard bridge."""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
import urllib.error
from collections import deque
from http import HTTPStatus
from typing import Any
from zoneinfo import ZoneInfo

import azure.functions as func
from azure.core.exceptions import AzureError

try:
    from .dashboard_server import (
        CanvasClient,
        DashboardService,
        DemoCanvasClient,
        render_home,
        setting,
    )
    from .table_todo_store import TableTodoStore
except ImportError:  # Azure Functions loads bridge/ as the project root.
    from dashboard_server import (
        CanvasClient,
        DashboardService,
        DemoCanvasClient,
        render_home,
        setting,
    )
    from table_todo_store import TableTodoStore


app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
_service: DashboardService | None = None
_auth_failures: deque[float] = deque()
_auth_lock = threading.Lock()
AUTH_FAILURE_LIMIT = 12
AUTH_FAILURE_WINDOW_SECONDS = 60


def _security_headers(content_security_policy: str) -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": content_security_policy,
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Referrer-Policy": "no-referrer",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


def _json_response(
    value: Any,
    status: HTTPStatus = HTTPStatus.OK,
    extra_headers: dict[str, str] | None = None,
) -> func.HttpResponse:
    headers = _security_headers("default-src 'none'; frame-ancestors 'none'")
    headers.update(extra_headers or {})
    return func.HttpResponse(
        json.dumps(value, ensure_ascii=True),
        status_code=int(status),
        mimetype="application/json",
        headers=headers,
    )


def _authorization_failure(req: func.HttpRequest) -> func.HttpResponse | None:
    expected = setting("DASHBOARD_API_KEY")
    supplied = req.headers.get("X-API-Key", "")
    if bool(expected and supplied) and hmac.compare_digest(expected, supplied):
        return None

    now = time.monotonic()
    with _auth_lock:
        while _auth_failures and now - _auth_failures[0] > AUTH_FAILURE_WINDOW_SECONDS:
            _auth_failures.popleft()
        if len(_auth_failures) >= AUTH_FAILURE_LIMIT:
            return _json_response(
                {"error": "too many authentication failures"},
                HTTPStatus.TOO_MANY_REQUESTS,
                {"Retry-After": str(AUTH_FAILURE_WINDOW_SECONDS)},
            )
        _auth_failures.append(now)
    return _json_response({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)


def _get_service() -> DashboardService:
    global _service
    if _service is None:
        demo_mode = setting("DEMO_MODE", "0").lower() in {"1", "true", "yes"}
        canvas: CanvasClient = DemoCanvasClient() if demo_mode else CanvasClient()
        if not canvas.configured:
            raise RuntimeError(
                "Canvas is not configured. Set CANVAS_BASE_URL and CANVAS_TOKEN, "
                "or set DEMO_MODE=1."
            )
        _service = DashboardService(
            TableTodoStore(),
            canvas,
            ZoneInfo(setting("DASHBOARD_TIMEZONE", "America/Denver")),
        )
    return _service


@app.function_name(name="Home")
@app.route(route="home", methods=["GET"])
def home(req: func.HttpRequest) -> func.HttpResponse:
    del req
    html, policy = render_home()
    return func.HttpResponse(
        html,
        mimetype="text/html",
        headers=_security_headers(policy),
    )


@app.function_name(name="Health")
@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    del req
    return _json_response({"ok": True})


@app.function_name(name="Dashboard")
@app.route(route="dashboard", methods=["GET"])
def dashboard(req: func.HttpRequest) -> func.HttpResponse:
    auth_failure = _authorization_failure(req)
    if auth_failure is not None:
        return auth_failure
    try:
        return _json_response(_get_service().build())
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        logging.warning("Canvas request failed: %s", type(error).__name__)
        return _json_response(
            {"error": "Canvas service unavailable"}, HTTPStatus.BAD_GATEWAY
        )
    except (AzureError, RuntimeError) as error:
        logging.error(
            "Dashboard configuration or storage failure: %s",
            type(error).__name__,
        )
        return _json_response(
            {"error": "Dashboard service unavailable"},
            HTTPStatus.SERVICE_UNAVAILABLE,
        )


@app.function_name(name="Todos")
@app.route(route="todos", methods=["GET", "POST"])
def todos(req: func.HttpRequest) -> func.HttpResponse:
    auth_failure = _authorization_failure(req)
    if auth_failure is not None:
        return auth_failure
    try:
        store = _get_service().todos
        if req.method == "GET":
            return _json_response(store.list())

        payload = req.get_json()
        todo = store.add(str(payload.get("title", "")), payload.get("due_at"))
        return _json_response(todo, HTTPStatus.CREATED)
    except (ValueError, TypeError) as error:
        return _json_response({"error": str(error)}, HTTPStatus.BAD_REQUEST)
    except (AzureError, RuntimeError) as error:
        logging.error("Todo storage failure: %s", type(error).__name__)
        return _json_response(
            {"error": "Todo service unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE
        )


@app.function_name(name="ToggleTodo")
@app.route(route="todos/{todo_id}/toggle", methods=["POST"])
def toggle_todo(req: func.HttpRequest) -> func.HttpResponse:
    auth_failure = _authorization_failure(req)
    if auth_failure is not None:
        return auth_failure
    try:
        todo = _get_service().todos.toggle(req.route_params.get("todo_id", ""))
        if todo is None:
            return _json_response(
                {"error": "todo not found"}, HTTPStatus.NOT_FOUND
            )
        return _json_response(todo)
    except (AzureError, RuntimeError) as error:
        logging.error("Todo storage failure: %s", type(error).__name__)
        return _json_response(
            {"error": "Todo service unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE
        )
