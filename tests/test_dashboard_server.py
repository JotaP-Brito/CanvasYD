import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bridge.dashboard_server import DashboardService, TodoStore, parse_datetime


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


if __name__ == "__main__":
    unittest.main()

