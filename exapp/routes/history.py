"""Read-only History API for Nextcloud AI Organizer.

Place at exapp/routes/history.py and include the router in exapp/main.py.
This endpoint reads SQLite only. It never contacts Nextcloud or changes file state.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Literal

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _columns(conn, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _history_query(conn):
    file_columns = _columns(conn, "files")
    suggestion_columns = _columns(conn, "suggestions")

    deleted_at = "f.deleted_at" if "deleted_at" in file_columns else "NULL"
    deletion_note = "f.deletion_note" if "deletion_note" in file_columns else "NULL"
    original_path = (
        "s.original_path" if "original_path" in suggestion_columns
        else "f.original_path" if "original_path" in file_columns
        else "NULL"
    )

    # A missing file that was still awaiting review belongs in History even if
    # an older archive implementation did not change the suggestion's status.
    deletion_condition = (
        f"({deleted_at} IS NOT NULL AND s.status IN ('pending', 'accepted'))"
    )
    history_status = (
        f"CASE WHEN {deletion_condition} THEN 'deleted' ELSE s.status END"
    )
    event_at = (
        "CASE "
        f"WHEN {deletion_condition} OR s.status = 'deleted' "
        f"THEN COALESCE({deleted_at}, s.updated_at, s.created_at) "
        "WHEN s.status = 'applied' "
        "THEN COALESCE(s.applied_at, s.updated_at, s.created_at) "
        "ELSE COALESCE(s.updated_at, s.created_at) END"
    )

    # Do not filter to only the latest suggestion: previous completed decisions
    # should remain visible after the same file is analyzed again.
    return f"""
        WITH history AS (
            SELECT
                s.id AS suggestion_id,
                s.file_id AS database_file_id,
                f.nextcloud_file_id AS file_id,
                f.path AS last_known_path,
                {original_path} AS original_path,
                f.etag AS etag,
                f.mime_type AS mime_type,
                f.size AS size,
                s.suggested_filename,
                s.suggested_folder,
                s.tags_json,
                s.category,
                s.paperless_candidate,
                s.confidence,
                s.reason,
                {history_status} AS status,
                s.created_at,
                s.updated_at,
                s.applied_at,
                {deleted_at} AS deleted_at,
                {deletion_note} AS deletion_note,
                {event_at} AS event_at
            FROM suggestions AS s
            JOIN files AS f ON f.id = s.file_id
            WHERE s.status IN ('applied', 'rejected', 'deleted')
               OR {deletion_condition}
        )
    """


@router.get("/history")
def list_history(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: Literal["all", "applied", "rejected", "deleted"] = "all",
):
    """Return a paginated, read-only history; no live DAV lookup is needed."""
    database = request.app.state.organizer.database
    with database._connect() as conn:
        sql = _history_query(conn)
        condition = "WHERE status = ?" if status != "all" else ""
        arguments = (status,) if status != "all" else ()
        count = conn.execute(
            sql + f" SELECT COUNT(*) FROM history {condition}", arguments
        ).fetchone()[0]
        rows = conn.execute(
            sql + f"""
                SELECT * FROM history {condition}
                ORDER BY event_at DESC, suggestion_id DESC
                LIMIT ? OFFSET ?
            """,
            (*arguments, limit, offset),
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        try:
            tags = json.loads(item.pop("tags_json") or "[]")
            item["tags"] = tags if isinstance(tags, list) else []
        except (ValueError, TypeError):
            item["tags"] = []
        item["paperless_candidate"] = bool(item["paperless_candidate"])
        item["confidence"] = float(item["confidence"] or 0)
        item["file_id"] = str(item["file_id"] or "")
        item["name"] = PurePosixPath(item["last_known_path"] or "").name
        item["deleted"] = item["deleted_at"] is not None or item["status"] == "deleted"
        # A historical move may have overwritten files.path. Do not falsely
        # label it an original path when no original snapshot was saved.
        items.append(item)

    return {"items": items, "count": count, "limit": limit,
            "offset": offset, "status": status}
