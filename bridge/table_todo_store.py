"""Azure Table Storage implementation of the dashboard's personal-task store."""

from __future__ import annotations

import os
import uuid
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode
from azure.identity import DefaultAzureCredential


def _setting(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class TableTodoStore:
    """Persist personal tasks in Azure Table Storage.

    A connection string is convenient for Azurite/local development. In Azure,
    prefer TODO_STORAGE_ENDPOINT plus a managed identity with the Storage Table
    Data Contributor role.
    """

    PARTITION_KEY = "personal"

    def __init__(self) -> None:
        table_name = _setting("TODO_TABLE_NAME", "Todos")
        connection_string = _setting("TODO_STORAGE_CONNECTION_STRING")
        endpoint = _setting("TODO_STORAGE_ENDPOINT")

        if connection_string:
            self.table = TableClient.from_connection_string(
                connection_string, table_name=table_name
            )
        elif endpoint:
            client_id = _setting("AZURE_CLIENT_ID")
            credential = DefaultAzureCredential(
                managed_identity_client_id=client_id or None
            )
            self.table = TableClient(
                endpoint=endpoint, table_name=table_name, credential=credential
            )
        else:
            raise RuntimeError(
                "Set TODO_STORAGE_ENDPOINT for managed identity, or "
                "TODO_STORAGE_CONNECTION_STRING for local development."
            )

        try:
            self.table.create_table()
        except ResourceExistsError:
            pass

    @staticmethod
    def _to_todo(entity: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(entity["RowKey"]),
            "title": str(entity.get("Title", "Untitled task")),
            "due_at": str(entity.get("DueAt") or "") or None,
            "completed": bool(entity.get("Completed", False)),
            "created_at": str(entity.get("CreatedAt", "")),
        }

    def list(self) -> list[dict[str, Any]]:
        entities = self.table.query_entities(
            query_filter="PartitionKey eq @partition",
            parameters={"partition": self.PARTITION_KEY},
        )
        todos = [self._to_todo(entity) for entity in entities]
        return sorted(todos, key=lambda todo: todo.get("created_at", ""))

    def add(self, title: str, due_at: str | None) -> dict[str, Any]:
        # Import here to keep validation identical in the local and cloud stores.
        try:
            from .dashboard_server import now_utc, parse_datetime
        except ImportError:  # Azure Functions loads bridge/ as the project root.
            from dashboard_server import now_utc, parse_datetime

        title = title.strip()
        if not title:
            raise ValueError("title is required")
        if len(title) > 160:
            raise ValueError("title must be 160 characters or fewer")
        if due_at and not parse_datetime(due_at):
            raise ValueError("due_at must be an ISO 8601 date/time")

        todo = {
            "id": str(uuid.uuid4()),
            "title": title,
            "due_at": due_at or None,
            "completed": False,
            "created_at": now_utc().isoformat(),
        }
        self.table.create_entity({
            "PartitionKey": self.PARTITION_KEY,
            "RowKey": todo["id"],
            "Title": todo["title"],
            "DueAt": todo["due_at"] or "",
            "Completed": todo["completed"],
            "CreatedAt": todo["created_at"],
        })
        return todo

    def toggle(self, todo_id: str) -> dict[str, Any] | None:
        try:
            entity = self.table.get_entity(
                partition_key=self.PARTITION_KEY, row_key=todo_id
            )
        except ResourceNotFoundError:
            return None

        entity["Completed"] = not bool(entity.get("Completed", False))
        self.table.update_entity(entity=entity, mode=UpdateMode.REPLACE)
        return self._to_todo(entity)
