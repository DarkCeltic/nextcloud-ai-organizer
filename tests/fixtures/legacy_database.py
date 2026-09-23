#!/usr/bin/env python3

import json
import logging
import sqlite3
from pathlib import Path
from typing import Dict, Optional

import yaml


class Database:
    """
    SQLite storage for discovered files and AI suggestions.
    """

    def __init__(self, config_file: str = "config.yaml"):
        self.log = logging.getLogger("database")
        self.config = self._load_config(config_file)

        db_config = self.config.get("database", {})
        self.path = Path(
            db_config.get("path", "/data/python_organizer_local_llm.db")
        )

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nextcloud_file_id TEXT,
                    path TEXT NOT NULL UNIQUE,
                    etag TEXT,
                    mime_type TEXT,
                    size INTEGER DEFAULT 0,
                    ignored INTEGER NOT NULL DEFAULT 0,
                    no_content INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    processed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_files_nextcloud_file_id
                    ON files(nextcloud_file_id);

                CREATE INDEX IF NOT EXISTS idx_files_etag
                    ON files(etag);

                CREATE TABLE IF NOT EXISTS suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    suggested_filename TEXT,
                    suggested_folder TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    category TEXT,
                    paperless_candidate INTEGER NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0.0,
                    reason TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    applied_at TEXT,
                    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_suggestions_file_id
                    ON suggestions(file_id);

                CREATE INDEX IF NOT EXISTS idx_suggestions_status
                    ON suggestions(status);
                """
            )

            # Safely migrate existing databases.
            columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(files)"
                )
            }

            if "deleted_at" not in columns:
                conn.execute(
                    "ALTER TABLE files "
                    "ADD COLUMN deleted_at TEXT"
                )

            if "deletion_note" not in columns:
                conn.execute(
                    "ALTER TABLE files "
                    "ADD COLUMN deletion_note TEXT"
                )

            columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(suggestions)"
                )
            }

            if "original_path" not in columns:
                conn.execute(
                    "ALTER TABLE suggestions "
                    "ADD COLUMN original_path TEXT"
                )

    def upsert_file(
            self,
            nextcloud_file_id: str,
            path: str,
            etag: str,
            mime_type: str,
            size: int = 0,
    ) -> int:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO files (nextcloud_file_id,
                                   path,
                                   etag,
                                   mime_type,
                                   size)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(path) DO
                UPDATE SET
                    nextcloud_file_id = excluded.nextcloud_file_id,
                    etag = excluded.etag,
                    mime_type = excluded.mime_type,
                    size = excluded.size,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (
                    str(nextcloud_file_id or ""),
                    path,
                    str(etag or ""),
                    str(mime_type or ""),
                    int(size or 0),
                ),
            )

            row = conn.execute(
                "SELECT id FROM files WHERE path = ?",
                (path,),
            ).fetchone()

            return int(row["id"])

    def get_file(self, path: str) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE path = ?",
                (path,),
            ).fetchone()

            return dict(row) if row else None

    def get_file_by_id(self, file_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE id = ?",
                (file_id,),
            ).fetchone()

            return dict(row) if row else None

    def is_ignored(self, path: str) -> bool:
        file_record = self.get_file(path)
        return bool(file_record and file_record.get("ignored"))

    def set_ignored(self, path: str, ignored: bool = True) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE files
                SET ignored      = ?,
                    last_seen_at = CURRENT_TIMESTAMP
                WHERE path = ?
                """,
                (1 if ignored else 0, path),
            )

    def mark_no_content(self, file_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE files
                SET no_content   = 1,
                    processed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (file_id,),
            )

    def mark_processed(self, file_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE files
                SET no_content   = 0,
                    processed_at = CURRENT_TIMESTAMP,
                    last_seen_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (file_id,),
            )

    def save_suggestion(
            self,
            file_id: int,
            suggested_filename: str,
            suggested_folder: str,
            tags,
            category: str,
            paperless_candidate: bool,
            confidence: float,
            reason: str,
            status: str = "pending",
    ) -> int:
        tags_json = json.dumps(tags or [], ensure_ascii=False)

        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO suggestions (file_id,
                                         suggested_filename,
                                         suggested_folder,
                                         tags_json,
                                         category,
                                         paperless_candidate,
                                         confidence,
                                         reason,
                                         status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    suggested_filename,
                    suggested_folder,
                    tags_json,
                    category,
                    1 if paperless_candidate else 0,
                    float(confidence or 0.0),
                    reason,
                    status,
                ),
            )
            conn.execute(
                """
                UPDATE suggestions
                SET original_path = (SELECT path
                                     FROM files
                                     WHERE id = ?)
                WHERE id = ?
                """,
                (file_id, int(cursor.lastrowid)),
            )

            suggestion_id = int(cursor.lastrowid)

            conn.execute(
                """
                UPDATE suggestions
                SET original_path = (SELECT path
                                     FROM files
                                     WHERE id = ?)
                WHERE id = ?
                  AND original_path IS NULL
                """,
                (file_id, suggestion_id),
            )

            return suggestion_id

    def get_latest_suggestion(self, file_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM suggestions
                WHERE file_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (file_id,),
            ).fetchone()

            if not row:
                return None

            result = dict(row)
            result["tags"] = json.loads(result.pop("tags_json", "[]"))
            result["paperless_candidate"] = bool(
                result.get("paperless_candidate")
            )

            return result

    def get_suggestion(self, suggestion_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM suggestions WHERE id = ?",
                (int(suggestion_id),),
            ).fetchone()

            if not row:
                return None

            result = dict(row)
            result["tags"] = json.loads(result.pop("tags_json", "[]"))
            result["paperless_candidate"] = bool(result.get("paperless_candidate"))
            return result

    def update_file_path(self, file_id: int, new_path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE files
                SET path         = ?,
                    last_seen_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (new_path, int(file_id)),
            )

    def list_pending(self, limit: int = 100):
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*,
                       f.path AS current_path,
                       f.etag AS file_etag
                FROM suggestions s
                         JOIN files f ON f.id = s.file_id
                WHERE s.status = 'pending'
                ORDER BY s.created_at ASC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()

            results = []

            for row in rows:
                item = dict(row)
                item["tags"] = json.loads(item.pop("tags_json", "[]"))
                item["paperless_candidate"] = bool(
                    item.get("paperless_candidate")
                )
                results.append(item)

            return results

    def update_suggestion_status(
            self,
            suggestion_id: int,
            status: str,
    ) -> None:
        allowed = {
            "pending",
            "accepted",
            "rejected",
            "applied",
        }

        if status not in allowed:
            raise ValueError(
                f"Invalid suggestion status '{status}'. "
                f"Allowed: {sorted(allowed)}"
            )

        applied_sql = (
            ", applied_at = CURRENT_TIMESTAMP"
            if status == "applied"
            else ""
        )

        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE suggestions
                SET status = ?,
                    updated_at = CURRENT_TIMESTAMP
                    {applied_sql}
                WHERE id = ?
                """,
                (status, suggestion_id),
            )

    def get_file_by_nextcloud_id(self, nextcloud_file_id: str):
        """Look up a file by its stable Nextcloud file ID."""
        file_id = str(nextcloud_file_id or "").strip()

        if not file_id:
            return None

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM files
                WHERE nextcloud_file_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (file_id,),
            ).fetchone()

        return dict(row) if row else None

    def list_review(self, limit: int = 100, offset: int = 0):
        """Return the latest pending or accepted suggestion per file."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*,
                       f.path AS current_path,
                       f.nextcloud_file_id,
                       f.etag AS file_etag,
                       f.mime_type,
                       f.size
                FROM suggestions AS s
                         JOIN files AS f ON f.id = s.file_id
                WHERE f.ignored = 0
                  AND s.status IN ('pending', 'accepted')
                  AND f.deleted_at IS NULL
                  AND s.id = (SELECT MAX(s2.id)
                              FROM suggestions AS s2
                              WHERE s2.file_id = s.file_id)
                ORDER BY s.created_at DESC, s.id DESC LIMIT ?
                OFFSET ?
                """,
                (int(limit), int(offset)),
            ).fetchall()

        results = []

        for row in rows:
            item = dict(row)

            item["tags"] = json.loads(
                item.pop("tags_json") or "[]"
            )

            item["paperless_candidate"] = bool(
                item["paperless_candidate"]
            )

            results.append(item)

        return results

    def count_review(self) -> int:
        """Count the latest pending or accepted suggestions."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM suggestions AS s
                         JOIN files AS f ON f.id = s.file_id
                WHERE f.ignored = 0
                  AND s.status IN ('pending', 'accepted')
                  AND f.deleted_at IS NULL
                  AND s.id = (SELECT MAX(s2.id)
                              FROM suggestions AS s2
                              WHERE s2.file_id = s.file_id)
                """
            ).fetchone()

        return int(row["total"])

    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _load_config(config_file: str) -> Dict:
        try:
            with open(config_file, "r", encoding="utf-8") as handle:
                config = yaml.safe_load(handle) or {}
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Configuration file not found: {config_file}"
            ) from exc
        except yaml.YAMLError as exc:
            raise RuntimeError(
                f"Invalid YAML configuration: {config_file}"
            ) from exc

        if not isinstance(config, dict):
            raise RuntimeError("Configuration root must be a YAML mapping.")

        return config

    def archive_missing_file(
            self,
            file_id: int,
            note: str = (
                    "File no longer available in Nextcloud. "
                    "Deletion detected during reconciliation."
            ),
    ) -> bool:
        """
        Archive missing files without deleting database records.

        file_id is the SQLite files.id, NOT the Nextcloud ID.
        """

        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE files
                SET deleted_at    = CURRENT_TIMESTAMP,
                    deletion_note = ?
                WHERE id = ?
                  AND deleted_at IS NULL
                """,
                (note, int(file_id)),
            )

            if cursor.rowcount == 0:
                return False

            # Only archive actionable suggestions.
            # Preserve previously completed/rejected history.
            conn.execute(
                """
                UPDATE suggestions
                SET status     = 'deleted',
                    updated_at = CURRENT_TIMESTAMP
                WHERE file_id = ?
                  AND status IN ('pending', 'accepted')
                """,
                (int(file_id),),
            )

            self.log.info(
                "Archived missing file: database ID %s",
                file_id,
            )

            return True

    def update_file_location(
            self,
            file_id: int,
            path: str,
    ) -> None:

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE files
                SET path          = ?,
                    last_seen_at  = CURRENT_TIMESTAMP,
                    deleted_at    = NULL,
                    deletion_note = NULL
                WHERE id = ?
                """,
                (path, int(file_id)),
            )
