import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bridge.dashboard_server import (
    CanvasClient,
    DashboardServer,
    DashboardService,
    TodoStore,
    parse_datetime,
    render_home,
)


class FakeCanvas:
    configured = True

    def get_items(self):
        return [{"id": "canvas-1", "source": "canvas", "title": "Essay", "context": "English",
                 "due_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(), "url": ""}]


class DashboardTests(unittest.TestCase):
    def test_parse_datetime_accepts_canvas_zulu_timestamp(self):
        value = parse_datetime("2030-01-01T12:00:00Z")
        self.assertEqual(value.utcoffset(), timedelta(0))

    def test_store_and_dashboard_merge_personal_and_canvas(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TodoStore(Path(folder) / "todos.json")
            todo = store.add("Buy notebooks", None)
            dashboard = DashboardService(store, FakeCanvas(), ZoneInfo("UTC")).build()
            self.assertEqual(dashboard["counts"], {"canvas": 1, "personal": 1})
            self.assertEqual({item["title"] for item in dashboard["items"]}, {"Essay", "Buy notebooks"})
            self.assertTrue(store.toggle(todo["id"])["completed"])

    def test_completed_personal_tasks_are_hidden(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TodoStore(Path(folder) / "todos.json")
            todo = store.add("Finished task", None)
            store.toggle(todo["id"])
            dashboard = DashboardService(store, FakeCanvas(), ZoneInfo("UTC")).build()
            self.assertEqual(dashboard["counts"]["personal"], 0)

    def test_todo_routes_require_the_dashboard_api_key(self):
        with tempfile.TemporaryDirectory() as folder:
            service = DashboardService(
                TodoStore(Path(folder) / "todos.json"), FakeCanvas(), ZoneInfo("UTC")
            )
            server = DashboardServer(("127.0.0.1", 0), service, "test-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_address[1]}/api/todos"
            try:
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(url, timeout=2)
                self.assertEqual(rejected.exception.code, 401)

                request = urllib.request.Request(
                    url, headers={"X-API-Key": "test-secret"}
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_local_server_fails_closed_without_an_api_key(self):
        with tempfile.TemporaryDirectory() as folder:
            service = DashboardService(
                TodoStore(Path(folder) / "todos.json"), FakeCanvas(), ZoneInfo("UTC")
            )
            server = DashboardServer(("127.0.0.1", 0), service, "")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_address[1]}/api/todos"
            try:
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(url, timeout=2)
                self.assertEqual(rejected.exception.code, 401)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_canvas_requires_https_and_same_origin_pagination(self):
        with patch.dict(
            os.environ,
            {"CANVAS_BASE_URL": "http://canvas.example", "CANVAS_TOKEN": "token"},
        ):
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                CanvasClient()

        with patch.dict(
            os.environ,
            {"CANVAS_BASE_URL": "https://canvas.example", "CANVAS_TOKEN": "token"},
        ):
            client = CanvasClient()
            client._require_same_origin("https://canvas.example/api/v1/planner/items")
            with self.assertRaisesRegex(ValueError, "change API origin"):
                client._require_same_origin("https://attacker.example/next")

    def test_home_uses_a_per_response_csp_nonce(self):
        first_html, first_policy = render_home()
        second_html, second_policy = render_home()
        self.assertNotIn("__CSP_NONCE__", first_html)
        self.assertIn("script-src 'nonce-", first_policy)
        self.assertNotEqual(first_policy, second_policy)
        self.assertNotEqual(first_html, second_html)


if __name__ == "__main__":
    unittest.main()
