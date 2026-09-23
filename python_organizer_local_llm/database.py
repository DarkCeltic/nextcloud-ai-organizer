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
                    paperless_excluded INTEGER NOT NULL DEFAULT 0,
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
                    original_path TEXT,
                    applied_path TEXT,
                    applied_actions_json TEXT NOT NULL DEFAULT '[]',
                    applied_tags_json TEXT,
                    decision_note TEXT,
                    manual_review_only INTEGER NOT NULL DEFAULT 0,
                    ocr_used INTEGER NOT NULL DEFAULT 0,
                    selected_destination TEXT,
                    dual_options_ready INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_suggestions_file_id
                    ON suggestions(file_id);

                CREATE INDEX IF NOT EXISTS idx_suggestions_status
                    ON suggestions(status);

                -- Failure records survive page reloads and retries; only unresolved
                -- records appear in Failed. Nextcloud files are never deleted here.
                CREATE TABLE IF NOT EXISTS analysis_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nextcloud_file_id TEXT NOT NULL UNIQUE,
                    path TEXT NOT NULL,
                    error TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'analyze',
                    attempts INTEGER NOT NULL DEFAULT 1,
                    first_failed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_failed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TEXT,
                    resolution TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_analysis_failures_open
                    ON analysis_failures(resolved_at, last_failed_at);

                -- Decisions on never-analyzed files cannot be stored in suggestions.
                CREATE TABLE IF NOT EXISTS file_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status = 'ignored'),
                    original_path TEXT,
                    note TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_file_decisions_file
                    ON file_decisions(file_id);
                """
            )

            conn.execute("""CREATE TABLE IF NOT EXISTS organizer_settings (
                name TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS automation_runs (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                started_at TEXT,
                finished_at TEXT,
                analyzed INTEGER DEFAULT 0,
                auto_applied INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                last_error TEXT
            )""")
            conn.execute("INSERT OR IGNORE INTO automation_runs(id) VALUES (1)")

            # Safely migrate existing databases.
            columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(files)"
                )
            }

            if 'paperless_excluded' not in columns:
                conn.execute('ALTER TABLE files ADD COLUMN paperless_excluded INTEGER NOT NULL DEFAULT 0')

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
                    "ALTER TABLE suggestions ADD COLUMN original_path TEXT"
                )
            if "applied_path" not in columns:
                conn.execute(
                    "ALTER TABLE suggestions ADD COLUMN applied_path TEXT"
                )
            if "applied_actions_json" not in columns:
                conn.execute(
                    "ALTER TABLE suggestions ADD COLUMN applied_actions_json "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
            if "applied_tags_json" not in columns:
                conn.execute(
                    "ALTER TABLE suggestions ADD COLUMN applied_tags_json TEXT"
                )
            if "decision_note" not in columns:
                conn.execute("ALTER TABLE suggestions ADD COLUMN decision_note TEXT")
            if 'manual_review_only' not in columns:
                conn.execute('ALTER TABLE suggestions ADD COLUMN manual_review_only INTEGER NOT NULL DEFAULT 0')
            if 'ocr_used' not in columns:
                conn.execute('ALTER TABLE suggestions ADD COLUMN ocr_used INTEGER NOT NULL DEFAULT 0')
            if 'selected_destination' not in columns:
                conn.execute('ALTER TABLE suggestions ADD COLUMN selected_destination TEXT')
            if 'dual_options_ready' not in columns:
                conn.execute('ALTER TABLE suggestions ADD COLUMN dual_options_ready INTEGER NOT NULL DEFAULT 0')

    def load_settings(self) -> Dict:
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM organizer_settings WHERE name='config'").fetchone()
        return json.loads(row["value_json"]) if row else {}

    def save_settings(self, settings: Dict) -> None:
        with self._connect() as conn:
            conn.execute("""INSERT INTO organizer_settings(name, value_json) VALUES ('config', ?)
                         ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json,
                         updated_at=CURRENT_TIMESTAMP""",
                         (json.dumps(settings, ensure_ascii=False),))

    def get_automation_run(self) -> Dict:
        with self._connect() as conn:
            return dict(conn.execute("SELECT * FROM automation_runs WHERE id=1").fetchone())

    def start_automation_run(self) -> None:
        with self._connect() as conn:
            conn.execute("""UPDATE automation_runs SET started_at=CURRENT_TIMESTAMP,
                finished_at=NULL, analyzed=0, auto_applied=0, failed=0,
                last_error=NULL WHERE id=1""")

    def finish_automation_run(self, analyzed: int, auto_applied: int,
                              failed: int, error: str = "") -> None:
        with self._connect() as conn:
            conn.execute("""UPDATE automation_runs SET finished_at=CURRENT_TIMESTAMP,
                analyzed=?, auto_applied=?, failed=?, last_error=? WHERE id=1""",
                (analyzed, auto_applied, failed, error[:2000] or None))

    def get_open_failure(self, nextcloud_file_id: str) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM analysis_failures "
                "WHERE nextcloud_file_id = ? AND resolved_at IS NULL",
                (str(nextcloud_file_id),),
            ).fetchone()
        return dict(row) if row else None

    def record_analysis_failure(self, nextcloud_file_id: str, path: str,
                                error: str, stage: str = "analyze") -> Dict:
        file_id = str(nextcloud_file_id or "").strip()
        if not file_id.isdecimal() or not path or not path.startswith("/"):
            raise ValueError("A Nextcloud file ID and absolute file path are required")
        message = str(error or "Unknown analysis error").strip()[:4000]
        with self._connect() as conn:
            file_row = conn.execute(
                "SELECT ignored, deleted_at FROM files "
                "WHERE nextcloud_file_id = ? ORDER BY id DESC LIMIT 1",
                (file_id,),
            ).fetchone()
            if file_row and (file_row["ignored"] or file_row["deleted_at"]):
                raise ValueError("This file is already ignored or archived")
            conn.execute(
                """INSERT INTO analysis_failures
                   (nextcloud_file_id, path, error, stage)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(nextcloud_file_id) DO UPDATE SET
                      path = excluded.path, error = excluded.error,
                      stage = excluded.stage,
                      attempts = analysis_failures.attempts + 1,
                      last_failed_at = CURRENT_TIMESTAMP,
                      resolved_at = NULL, resolution = NULL""",
                (file_id, path, message, str(stage or "analyze")[:60]),
            )
        return self.get_open_failure(file_id)

    def resolve_analysis_failure(self, nextcloud_file_id: str,
                                 resolution: str = "analyzed") -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE analysis_failures
                   SET resolved_at = CURRENT_TIMESTAMP, resolution = ?
                   WHERE nextcloud_file_id = ? AND resolved_at IS NULL""",
                (str(resolution), str(nextcloud_file_id)),
            )

    def list_failures(self, limit: int = 50, offset: int = 0):
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT af.*, f.id AS database_file_id, f.deleted_at
                   FROM analysis_failures AS af
                   LEFT JOIN files AS f ON f.id = (
                       SELECT MAX(id) FROM files
                       WHERE nextcloud_file_id = af.nextcloud_file_id
                   )
                   WHERE af.resolved_at IS NULL
                     AND COALESCE(f.ignored, 0) = 0 AND f.deleted_at IS NULL
                   ORDER BY af.last_failed_at DESC, af.id DESC
                   LIMIT ? OFFSET ?""",
                (int(limit), int(offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_failures(self) -> int:
        with self._connect() as conn:
            return conn.execute(
                """SELECT COUNT(*) FROM analysis_failures AS af
                   LEFT JOIN files AS f ON f.id = (
                       SELECT MAX(id) FROM files
                       WHERE nextcloud_file_id = af.nextcloud_file_id
                   )
                   WHERE af.resolved_at IS NULL
                     AND COALESCE(f.ignored, 0) = 0 AND f.deleted_at IS NULL"""
            ).fetchone()[0]

    def decide_file(self, nextcloud_file_id: str, decision: str,
                    path: str = "", etag: str = "", suggestion_id=None) -> Dict:
        """Atomic metadata-only decision; never changes a Nextcloud file."""
        file_id = str(nextcloud_file_id or "").strip()
        if not file_id.isdecimal() or decision not in ("ignore", "reject"):
            raise ValueError("Invalid file ID or decision")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE nextcloud_file_id = ? "
                "ORDER BY id DESC LIMIT 1", (file_id,),
            ).fetchone()
            if row is None:
                if decision != "ignore" or not path or not path.startswith("/"):
                    raise ValueError("Cannot find an analyzed file for this decision")
                # Reuse a legacy path record whose Nextcloud ID was missing.
                # Never take ownership of a path associated with another ID.
                conn.execute(
                    """UPDATE files SET nextcloud_file_id = ?
                       WHERE path = ? AND COALESCE(nextcloud_file_id, '') = ''""",
                    (file_id, path),
                )
                conn.execute(
                    """INSERT INTO files (nextcloud_file_id, path, etag)
                       VALUES (?, ?, ?) ON CONFLICT(path) DO NOTHING""",
                    (file_id, path, etag),
                )
                row = conn.execute(
                    "SELECT * FROM files WHERE nextcloud_file_id = ? "
                    "ORDER BY id DESC LIMIT 1", (file_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("Path belongs to another file; no decision saved")
            if row["deleted_at"]:
                raise ValueError("The file is already archived as unavailable")
            file_pk = int(row["id"])
            latest = conn.execute(
                """SELECT id, status FROM suggestions
                   WHERE file_id = ? ORDER BY id DESC LIMIT 1""",
                (file_pk,),
            ).fetchone()
            active = (latest if latest and latest["status"] in
                      ("pending", "accepted") else None)
            if suggestion_id is not None:
                if active is None or int(active["id"]) != int(suggestion_id):
                    raise ValueError("Suggestion is no longer awaiting review")
            failure = conn.execute(
                "SELECT error FROM analysis_failures "
                "WHERE nextcloud_file_id = ? AND resolved_at IS NULL", (file_id,),
            ).fetchone()
            note = ("Ignored after analysis failure: " + failure["error"]
                    if failure else "Ignored by user")
            if decision == "reject":
                if active is None or suggestion_id is None:
                    raise ValueError("Reject requires a pending Review suggestion")
                conn.execute(
                    """UPDATE suggestions SET status = 'rejected',
                       decision_note = 'Rejected by user',
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (int(suggestion_id),),
                )
                status = "rejected"
            else:
                if row["ignored"]:
                    raise ValueError("File is already ignored")
                conn.execute("UPDATE files SET ignored = 1 WHERE id = ?", (file_pk,))
                if active:
                    conn.execute(
                        """UPDATE suggestions SET status = 'ignored',
                           decision_note = ?,
                           updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                        (note, int(active["id"])),
                    )
                else:
                    conn.execute(
                        """INSERT INTO file_decisions
                           (file_id, status, original_path, note)
                           VALUES (?, 'ignored', ?, ?)""",
                        (file_pk, row["path"], note),
                    )
                status = "ignored"
            conn.execute(
                """UPDATE analysis_failures
                   SET resolved_at = CURRENT_TIMESTAMP, resolution = ?
                   WHERE nextcloud_file_id = ? AND resolved_at IS NULL""",
                (status, file_id),
            )
        return {"ok": True, "status": status, "file_id": file_id}

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
            manual_review_only: bool = False,
            ocr_used: bool = False,
            dual_options_ready: bool = True,
            supersede_active: bool = False,
    ) -> int:

        tags_json = json.dumps(tags or [], ensure_ascii=False)

        # Fetch and snapshot the actual path at analysis time, not the AI's
        # suggested destination. Both operations commit in one transaction.
        with self._connect() as conn:
            file_row = conn.execute(
                "SELECT path FROM files WHERE id = ?", (int(file_id),)
            ).fetchone()
            if not file_row or not file_row["path"]:
                raise ValueError(f"Cannot save suggestion: file {file_id} has no path")
            if supersede_active:
                # Commit the new Review row and the previous row's archival together.
                # A failed inference never supersedes any existing Review work.
                conn.execute(
                    """UPDATE suggestions
                       SET status = 'superseded',
                           decision_note = 'Replaced by a fresh analysis requested from History',
                           updated_at = CURRENT_TIMESTAMP,
                           manual_review_only = 1
                       WHERE file_id = ? AND status IN ('pending', 'accepted')""",
                    (int(file_id),),
                )
            cursor = conn.execute(
                """
                INSERT INTO suggestions (
                    file_id, original_path, suggested_filename,
                    suggested_folder, tags_json, category,
                    paperless_candidate, confidence, reason, status, manual_review_only, ocr_used, dual_options_ready
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(file_id), file_row["path"], suggested_filename,
                    suggested_folder, tags_json, category,
                    int(bool(paperless_candidate)), float(confidence or 0.0),
                    reason, status, int(bool(manual_review_only or ocr_used)),
                    int(bool(ocr_used)), int(bool(dual_options_ready)),
                ),
            )
            return int(cursor.lastrowid)

    def reenable_history_file(self, file_id: int, original_record_id: int, record_status: str,
                              live_path: str) -> None:
        """Unignore a known historical record without changing its old status."""
        if record_status not in ('ignored', 'applied', 'rejected'):
            raise ValueError('Only ignored, applied or rejected history can be re-analyzed')
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM files WHERE id=?', (int(file_id),)).fetchone()
            if not row or row['deleted_at']:
                raise ValueError('Historical file is unavailable')
            if original_record_id > 0:
                record = conn.execute('SELECT status FROM suggestions WHERE id=? AND file_id=?',
                                      (int(original_record_id), int(file_id))).fetchone()
            else:
                record = conn.execute('SELECT status FROM file_decisions WHERE id=? AND file_id=?',
                                      (-int(original_record_id), int(file_id))).fetchone()
            if not record or record['status'] != record_status:
                raise ValueError('History record not found or status changed')
            # Pause Automatic Apply for any existing Review row while the new
            # inference runs. Never mark it superseded until the new row is saved.
            conn.execute(
                """UPDATE suggestions SET manual_review_only=1
                   WHERE file_id=? AND status IN ('pending', 'accepted')""",
                (int(file_id),),
            )
            conn.execute(
                'UPDATE files SET ignored=0, path=?, last_seen_at=CURRENT_TIMESTAMP WHERE id=?',
                (live_path, int(file_id)),
            )
            return None

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


    def record_apply_progress(
        self,
        file_id: int,
        suggestion_id: int,
        current_path: str,
        actions,
        paperless: bool = False,
        tags=None,
    ) -> Dict:
        """Atomically record a successfully completed Apply step.

        Applied actions are cumulative across multiple clicks. Existing original
        paths are never overwritten or guessed for older suggestions.
        """
        valid = {"folder", "filename", "tags"}
        actions = set(actions)
        if not actions or not actions.issubset(valid):
            raise ValueError("Apply progress requires valid, nonempty actions")
        if not current_path or not current_path.startswith("/"):
            raise ValueError("A valid current path is required")

        with self._connect() as conn:
            row = conn.execute(
                "SELECT file_id, status, applied_actions_json, paperless_candidate, selected_destination "
                "FROM suggestions WHERE id = ?", (int(suggestion_id),)
            ).fetchone()
            if row is None or int(row["file_id"]) != int(file_id):
                raise ValueError("Suggestion does not belong to this file")
            if row["status"] not in ("pending", "accepted"):
                raise ValueError("Suggestion is no longer actionable")
            previous = set(json.loads(row["applied_actions_json"] or "[]"))
            cumulative = previous | actions
            is_paperless = bool(row["paperless_candidate"])
            if paperless and not is_paperless:
                raise ValueError("The AI did not recommend Paperless for this suggestion")
            destination = 'paperless' if paperless else 'nextcloud'
            if row['selected_destination'] and row['selected_destination'] != destination:
                raise ValueError('This suggestion already has changes applied to the other destination')
            complete = "folder" in cumulative if paperless else valid <= cumulative
            status = "applied" if complete else "accepted"
            changed = conn.execute(
                "UPDATE files SET path = ?, last_seen_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND deleted_at IS NULL",
                (current_path, int(file_id)),
            )
            if changed.rowcount != 1:
                raise ValueError("File unavailable or missing from database")
            tags_json = (json.dumps(tags, ensure_ascii=False)
                         if "tags" in actions and tags is not None else None)
            conn.execute(
                """
                UPDATE suggestions SET
                    applied_path = ?,
                    applied_actions_json = ?,
                    applied_tags_json = CASE WHEN ? IS NOT NULL
                                             THEN ? ELSE applied_tags_json END,
                    status = ?, selected_destination = ?, updated_at = CURRENT_TIMESTAMP,
                    applied_at = CASE WHEN ? = 'applied'
                                     THEN COALESCE(applied_at, CURRENT_TIMESTAMP)
                                     ELSE applied_at END
                WHERE id = ?
                """,
                (current_path, json.dumps(sorted(cumulative)), tags_json,
                 tags_json, status, destination, status, int(suggestion_id)),
            )
            return {"complete": complete, "status": status,
                    "applied_actions": sorted(cumulative), "path": current_path}

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
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis_failures AS af
                      WHERE af.nextcloud_file_id = f.nextcloud_file_id
                        AND af.resolved_at IS NULL
                  )
                  AND s.id = (SELECT MAX(s2.id)
                              FROM suggestions AS s2
                              WHERE s2.file_id = s.file_id
                                AND s2.status IN ('pending', 'accepted'))
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
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis_failures AS af
                      WHERE af.nextcloud_file_id = f.nextcloud_file_id
                        AND af.resolved_at IS NULL
                  )
                  AND s.id = (SELECT MAX(s2.id)
                              FROM suggestions AS s2
                              WHERE s2.file_id = s.file_id
                                AND s2.status IN ('pending', 'accepted'))
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

            conn.execute(
                """UPDATE analysis_failures
                   SET resolved_at = CURRENT_TIMESTAMP, resolution = 'deleted'
                   WHERE nextcloud_file_id = (
                       SELECT nextcloud_file_id FROM files WHERE id = ?
                   ) AND resolved_at IS NULL""", (int(file_id),),
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
