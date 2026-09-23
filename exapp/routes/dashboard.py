"""Read-only Unprocessed and Review endpoints for Nextcloud AI Organizer.

Install as exapp/routes/dashboard.py. Supports ?debug=true diagnostics.
"""

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from pathlib import PurePosixPath
from typing import Literal, Optional
from pydantic import BaseModel, Field

from exapp.routes.analyze import analyze_file, generate_suggestion, _public_suggestion
from exapp.routes.file_action import remember_context

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
log = logging.getLogger("exapp.dashboard")


def reconcile_review_files(organizer):
    """
    Check all Review records against live Nextcloud IDs.

    - Existing file: keep in Review.
    - Moved file: update its path.
    - Missing file: archive its pending suggestions.
    - API error: abort without treating the file as deleted.
    """

    database = organizer.database

    # Collect all Review records BEFORE updating statuses.
    # Otherwise, pagination may skip records when rows
    # disappear from Review during reconciliation.
    rows = []
    offset = 0
    page_size = 200

    while True:

        batch = database.list_review(
            limit=page_size,
            offset=offset,
        )

        rows.extend(batch)

        if len(batch) < page_size:
            break

        offset += len(batch)

    checked = set()
    archived = 0
    moved = 0

    for row in rows:

        database_file_id = int(row["file_id"])

        # Multiple suggestions may reference one file.
        if database_file_id in checked:
            continue

        checked.add(database_file_id)

        nextcloud_id = str(
            row.get("nextcloud_file_id") or ""
        ).strip()

        if not nextcloud_id:
            log.warning(
                "Cannot reconcile database file %s: "
                "missing Nextcloud ID",
                database_file_id,
            )
            continue

        db_file = database.get_file_by_id(
            database_file_id
        )

        if not db_file or db_file.get("deleted_at"):
            continue

        try:
            remote = organizer.nextcloud.find_file_by_id(
                nextcloud_id
            )

        except Exception as exc:

            log.exception(
                "Unable to verify Nextcloud file %s",
                nextcloud_id,
            )

            raise HTTPException(
                status_code=502,
                detail=(
                    "Unable to verify Nextcloud files. "
                    "No further records were archived."
                ),
            ) from exc

        if remote is None:

            note = (
                "File no longer found in the connected "
                "Nextcloud account's active files. "
                f"Last known path: {db_file['path']}. "
                "Deletion or loss of access detected "
                "during reconciliation."
            )

            changed = database.archive_missing_file(
                database_file_id,
                note=note,
            )

            if changed:
                archived += 1

                log.info(
                    "Archived missing Nextcloud file %s: %s",
                    nextcloud_id,
                    db_file["path"],
                )

            continue

        # File still exists. Check whether it was moved.
        current_path = remote["path"]

        if current_path != db_file["path"]:

            try:
                database.update_file_location(
                    database_file_id,
                    current_path,
                )

            except sqlite3.IntegrityError:

                # The new path is already owned by another
                # database record. Never overwrite history.
                log.exception(
                    "Path conflict while updating file %s "
                    "from %s to %s",
                    nextcloud_id,
                    db_file["path"],
                    current_path,
                )

                continue

            moved += 1

            log.info(
                "Updated location of Nextcloud file %s: "
                "%s -> %s",
                nextcloud_id,
                db_file["path"],
                current_path,
            )

    log.info(
        "Review reconciliation complete: "
        "%s checked, %s archived, %s moved",
        len(checked),
        archived,
        moved,
    )

    return {
        "checked": len(checked),
        "archived": archived,
        "moved": moved,
    }


@router.get("/unprocessed")
def list_unprocessed(
        request: Request,
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        debug: bool = Query(default=False),
):
    """List never-analyzed or changed files without running Ollama.

    Add ?debug=true to see exactly why files were omitted. The endpoint is
    read-only: it does not modify SQLite, download file contents, or apply tags.
    """
    organizer = request.app.state.organizer
    database = organizer.database
    scanner = organizer.scanner

    results = []
    seen = set()
    reasons = Counter()
    examples = defaultdict(list)
    discovered = 0

    def skip(reason: str, path: str):
        reasons[reason] += 1
        if len(examples[reason]) < 5:
            examples[reason].append(path)
        if debug:
            log.info("Unprocessed skipped %s: %s", path, reason)

    for scan_path in scanner.scan_paths:
        if scanner.is_excluded(scan_path):
            skip("scan_root_excluded", scan_path)
            continue

        try:
            remote_files = organizer.nextcloud.list_files_recursive(scan_path)
        except Exception as exc:
            log.exception("Unable to list Nextcloud folder %s", scan_path)
            raise HTTPException(
                status_code=502,
                detail=f"Unable to scan configured folder: {scan_path}",
            ) from exc

        log.info("Unprocessed: scanned root %s; returned %d items", scan_path,
                 len(remote_files))

        for remote in remote_files:
            discovered += 1
            path = remote.get("path") or "(no path)"

            if not remote.get("path") or remote.get("is_directory") or \
                    remote.get("type") in {"directory", "dir", "folder"}:
                skip("directory_or_missing_path", path)
                continue

            if scanner.is_excluded(path):
                skip("excluded_path", path)
                continue

            if not scanner.is_supported_file(path):
                skip("unsupported_extension", path)
                continue

            nextcloud_id = str(
                remote.get("file_id") or remote.get("nextcloud_file_id") or ""
            ).strip()
            if not nextcloud_id:
                skip("missing_nextcloud_file_id", path)
                log.warning("Nextcloud returned no file ID for %s; check WebDAV "
                            "oc:fileid property parsing", path)
                continue

            if nextcloud_id in seen:
                skip("duplicate_scan_root", path)
                continue
            seen.add(nextcloud_id)

            existing = database.get_file_by_nextcloud_id(nextcloud_id)
            if existing is None:
                by_path = database.get_file(path)
                if by_path and str(by_path.get("nextcloud_file_id") or "") in {
                    "", nextcloud_id
                }:
                    existing = by_path

            if existing and existing.get("ignored"):
                skip("ignored_in_database", path)
                continue

            if database.get_open_failure(nextcloud_id):
                skip("awaiting_failed_retry", path)
                continue

            latest = (
                database.get_latest_suggestion(int(existing["id"]))
                if existing else None
            )

            current_etag = str(remote.get("etag") or "").strip('"')
            analyzed_etag = str(existing.get("etag") or "").strip('"') \
                if existing else ""

            if latest:
                if latest["status"] in {"pending", "accepted"}:
                    # A pending suggestion should be reviewed, not re-analyzed.
                    skip("awaiting_review", path)
                    continue
                if not (current_etag and analyzed_etag and
                        current_etag != analyzed_etag):
                    skip("already_analyzed_unchanged", path)
                    continue
                state = "changed"
            else:
                state = "no_content" if existing and existing.get("no_content") \
                    else "unprocessed"

            results.append({
                "file_id": nextcloud_id,
                "database_file_id": existing["id"] if existing else None,
                "path": path,
                "name": remote.get("name") or PurePosixPath(path).name,
                "etag": current_etag,
                "mime_type": remote.get("mime_type") or "",
                "size": int(remote.get("size") or 0),
                "status": state,
            })

    results.sort(key=lambda item: item["path"].casefold())
    log.info("Unprocessed: roots=%r discovered=%d queued=%d skipped=%s",
             scanner.scan_paths, discovered, len(results), dict(reasons))

    response = {
        "items": results[offset:offset + limit],
        "count": len(results),
        "limit": limit,
        "offset": offset,
    }
    if debug:
        response["diagnostics"] = {
            "scan_paths": scanner.scan_paths,
            "discovered_items": discovered,
            "eligible_files": len(results),
            "skipped_by_reason": dict(reasons),
            "example_paths_by_reason": dict(examples),
        }
    return response


@router.get("/review")
def list_review(
        request: Request,
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
):
    """Return latest pending or partly applied suggestion for each file."""
    organizer = request.app.state.organizer
    database = organizer.database

    # Verify and archive missing files before returning Review.
    reconcile_review_files(organizer)

    rows = database.list_review(
        limit=limit,
        offset=offset,
    )
    items = []
    for row in rows:
        path = row["current_path"]
        items.append({
            "suggestion_id": row["id"],
            "file_id": str(row.get("nextcloud_file_id") or ""),
            "database_file_id": row["file_id"],
            "path": path,
            "name": PurePosixPath(path).name,
            "status": row["status"],
            "original_path": row.get("original_path"),
            "suggested_filename": row.get("suggested_filename") or "",
            "suggested_folder": row.get("suggested_folder") or "",
            "tags": row.get("tags") or [],
            "category": row.get("category") or "",
            "paperless_candidate": bool(row.get("paperless_candidate")),
            "dual_options_ready": bool(row.get('dual_options_ready')),
            "paperless_enabled": bool(organizer.paperless_enabled),
            "paperless_inbox": organizer.paperless_inbox,
            "selected_destination": row.get('selected_destination'),
            "confidence": float(row.get("confidence") or 0),
            "ocr_used": bool(row.get("ocr_used")),
            "reason": row.get("reason") or "",
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "etag": row.get("file_etag") or "",
            "mime_type": row.get("mime_type") or "",
            "size": int(row.get("size") or 0),
        })
    return {"items": items, "count": database.count_review(),
            "limit": limit, "offset": offset}


class FailureReport(BaseModel):
    file_id: str
    path: str
    error: str = Field(min_length=1, max_length=4000)
    stage: Literal["analyze", "activate", "context", "ocr"] = "analyze"


class DecisionRequest(BaseModel):
    file_id: str
    decision: Literal["ignore", "reject"]
    path: str = ""
    etag: str = ""
    suggestion_id: Optional[int] = None


@router.post("/failed/report")
def report_failure(body: FailureReport, request: Request):
    """Persist a UI-observed processing failure; no DAV request or deletion."""
    database = request.app.state.organizer.database
    try:
        record = database.record_analysis_failure(
            body.file_id, body.path, body.error, body.stage
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "failure": record}


@router.post("/failed/{file_id}/resolve")
def resolve_failure(file_id: str, request: Request):
    if not file_id.isdecimal():
        raise HTTPException(status_code=400, detail="Invalid Nextcloud file ID.")
    request.app.state.organizer.database.resolve_analysis_failure(file_id)
    return {"ok": True}


@router.get("/failed")
def list_failed(request: Request,
                limit: int = Query(default=50, ge=1, le=200),
                offset: int = Query(default=0, ge=0)):
    database = request.app.state.organizer.database
    rows = database.list_failures(limit, offset)
    items = []
    for row in rows:
        path = row["path"]
        items.append({
            "file_id": str(row["nextcloud_file_id"]),
            "database_file_id": row["database_file_id"],
            "path": path, "name": PurePosixPath(path).name,
            "status": "failed", "error": row["error"],
            "stage": row["stage"], "attempts": row["attempts"],
            "first_failed_at": row["first_failed_at"],
            "last_failed_at": row["last_failed_at"],
        })
    return {"items": items, "count": database.count_failures(),
            "limit": limit, "offset": offset}


@router.post("/decision")
def decide_file(body: DecisionRequest, request: Request):
    database = request.app.state.organizer.database
    try:
        return database.decide_file(
            body.file_id, body.decision, path=body.path,
            etag=body.etag, suggestion_id=body.suggestion_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# from exapp.routes.file_action import remember_context

@router.post("/activate/{file_id}")
def activate_file(file_id: str, request: Request):
    """
    Activate a file using its actual Nextcloud file ID.

    A missing file is archived and returns HTTP 410.
    """

    if not file_id.isdecimal():
        raise HTTPException(
            status_code=400,
            detail="Invalid Nextcloud file ID.",
        )

    organizer = request.app.state.organizer

    database = organizer.database
    scanner = organizer.scanner

    db_file = database.get_file_by_nextcloud_id(
        file_id
    )

    if db_file and db_file.get("ignored"):
        raise HTTPException(
            status_code=403,
            detail="This file is ignored.",
        )

    if db_file and db_file.get("deleted_at"):
        raise HTTPException(
            status_code=410,
            detail=(
                "This file was previously archived "
                "because it is no longer available."
            ),
        )

    # Search by ID, not by the original path.
    try:
        remote = organizer.nextcloud.find_file_by_id(
            file_id
        )

    except Exception as exc:

        log.exception(
            "Nextcloud lookup failed for file %s",
            file_id,
        )

        raise HTTPException(
            status_code=502,
            detail="Unable to verify file in Nextcloud.",
        ) from exc

    # File was not found anywhere in the account.
    if remote is None:

        if db_file:

            database.archive_missing_file(
                int(db_file["id"]),
                note=(
                    "File no longer found in the "
                    "connected Nextcloud account. "
                    f"Last known path: {db_file['path']}. "
                    "Archived during activation."
                ),
            )

            log.info(
                "Archived missing file %s",
                file_id,
            )

            raise HTTPException(
                status_code=410,
                detail=(
                    "File no longer available. "
                    "Its suggestion has been moved "
                    "to History. Refresh Review."
                ),
            )

        raise HTTPException(
            status_code=404,
            detail="File not found in Nextcloud.",
        )

    path = remote.get("path") or ""

    # New files must be inside a configured scan root.
    # Existing indexed files are recognized by their ID.
    if db_file is None:

        allowed = any(
            scanner._normalize_path(root) == "/"
            or path == scanner._normalize_path(root)
            or path.startswith(
                scanner._normalize_path(root).rstrip("/")
                + "/"
            )
            for root in scanner.scan_paths
        )

        if not allowed:
            raise HTTPException(
                status_code=403,
                detail="File is outside configured scan roots.",
            )

    if (
        not path
        or remote.get("is_directory")
        or scanner.is_excluded(path)
        or not scanner.is_supported_file(path)
    ):
        raise HTTPException(
            status_code=403,
            detail="File is not eligible for AI Organizer.",
        )

    # Update moved files.
    if db_file and path != db_file["path"]:

        try:
            database.update_file_location(
                int(db_file["id"]),
                path,
            )

        except sqlite3.IntegrityError as exc:

            log.exception(
                "Database path conflict for file %s",
                file_id,
            )

            raise HTTPException(
                status_code=409,
                detail=(
                    "File was found, but its new path "
                    "conflicts with an existing database "
                    "record. No history was overwritten."
                ),
            ) from exc

    log.info(
        "Activation resolved file %s: %s",
        file_id,
        path,
    )

    # Rebuild context using current Nextcloud metadata.
    from exapp.routes.file_action import remember_context

    context = remember_context({
        "fileId": file_id,
        "fileType": "file",
        "name": remote.get("name")
                or PurePosixPath(path).name,
        "directory": str(PurePosixPath(path).parent),
        "etag": remote.get("etag") or "",
        "mime": remote.get("mime_type") or "",
        "size": remote.get("size") or 0,
        "userId": organizer.nextcloud.username,
    })

    return context


# History uses the same already-registered dashboard router as Review.
def _columns(conn, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _history_query(conn):
    file_columns = _columns(conn, "files")
    suggestion_columns = _columns(conn, "suggestions")

    deleted_at = "f.deleted_at" if "deleted_at" in file_columns else "NULL"
    deletion_note = "f.deletion_note" if "deletion_note" in file_columns else "NULL"
    applied_path = "s.applied_path" if "applied_path" in suggestion_columns else "NULL"
    applied_actions = ("s.applied_actions_json" if "applied_actions_json" in suggestion_columns
                       else "'[]'")
    applied_tags = ("s.applied_tags_json" if "applied_tags_json" in suggestion_columns
                    else "NULL")
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
                {applied_path} AS applied_path,
                {applied_actions} AS applied_actions_json,
                {applied_tags} AS applied_tags_json,
                f.etag AS etag,
                f.mime_type AS mime_type,
                f.size AS size,
                s.suggested_filename,
                s.suggested_folder,
                s.tags_json,
                s.category,
                s.paperless_candidate,
                s.selected_destination,
                s.confidence,
                s.reason,
                s.ocr_used,
                {history_status} AS status,
                s.created_at,
                s.updated_at,
                s.applied_at,
                {deleted_at} AS deleted_at,
                {deletion_note} AS deletion_note,
                s.decision_note AS event_note,
                {event_at} AS event_at
            FROM suggestions AS s
            JOIN files AS f ON f.id = s.file_id
            WHERE s.status IN ('applied', 'rejected', 'deleted', 'ignored', 'superseded')
               OR {deletion_condition}
            UNION ALL
            SELECT
                -d.id AS suggestion_id,
                d.file_id AS database_file_id,
                f.nextcloud_file_id AS file_id,
                f.path AS last_known_path,
                d.original_path AS original_path,
                NULL AS applied_path,
                '[]' AS applied_actions_json,
                NULL AS applied_tags_json,
                f.etag AS etag,
                f.mime_type AS mime_type,
                f.size AS size,
                NULL AS suggested_filename,
                NULL AS suggested_folder,
                '[]' AS tags_json,
                NULL AS category,
                0 AS paperless_candidate,
                NULL AS selected_destination,
                0.0 AS confidence,
                NULL AS reason,
                0 AS ocr_used,
                d.status AS status,
                d.created_at AS created_at,
                d.created_at AS updated_at,
                NULL AS applied_at,
                f.deleted_at AS deleted_at,
                f.deletion_note AS deletion_note,
                d.note AS event_note,
                d.created_at AS event_at
            FROM file_decisions AS d
            JOIN files AS f ON f.id = d.file_id
        )
    """


class HistoryReanalyzeRequest(BaseModel):
    record_id: int
    status: Literal['ignored', 'applied', 'rejected']


@router.post('/history/{file_id}/reanalyze')
def reanalyze_history(file_id: str, body: HistoryReanalyzeRequest, request: Request):
    """Reopen a real surviving file while retaining every old History entry."""
    if not file_id.isdecimal():
        raise HTTPException(status_code=400, detail='Invalid Nextcloud file ID.')
    organizer = request.app.state.organizer
    db = organizer.database
    record = db.get_file_by_nextcloud_id(file_id)
    if not record or record.get('deleted_at'):
        raise HTTPException(status_code=410, detail='Historical file unavailable.')
    try:
        remote = organizer.nextcloud.find_file_by_id(file_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail='Could not verify the file in Nextcloud; History unchanged.') from exc
    if not remote:
        raise HTTPException(status_code=410, detail='File not found in Nextcloud. History unchanged.')
    path = remote.get('path') or ''
    if not path or remote.get('is_directory') or organizer.scanner.is_excluded(path) or not organizer.scanner.is_supported_file(path):
        raise HTTPException(status_code=403, detail='File is no longer eligible for analysis in its current location.')
    try:
        db.reenable_history_file(int(record['id']), body.record_id, body.status, path)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Refresh the short-lived context using the live Nextcloud file ID/path;
    # force=True guarantees a NEW suggestion, even if Review already has one.
    context = remember_context({
        'fileId': file_id, 'fileType': 'file', 'name': remote.get('name') or PurePosixPath(path).name,
        'directory': str(PurePosixPath(path).parent), 'etag': remote.get('etag') or '',
        'mime': remote.get('mime_type') or '', 'size': remote.get('size') or 0,
        'userId': organizer.nextcloud.username,
    })
    try:
        suggestion = analyze_file(file_id=file_id, request=request, force=True,
                                  manual_review_only=True, supersede_active=True)
        db.resolve_analysis_failure(file_id)
    except HTTPException as exc:
        # OCR failures were already persisted with stage='ocr' by Analyze;
        # don't double-count attempts or replace the more useful error stage.
        if not (exc.status_code == 422 and str(exc.detail).startswith(('OCR:', 'Extraction:'))):
            try:
                db.record_analysis_failure(file_id, path, str(exc.detail), 'analyze')
            except ValueError:
                log.warning('Could not record History re-analysis failure for %s', file_id)
        raise
    except Exception as exc:
        try:
            db.record_analysis_failure(file_id, path, 'Re-analysis error', 'analyze')
        except ValueError:
            pass
        raise HTTPException(status_code=502, detail='Historical file analysis failed. See Failed tab.') from exc
    return {'ok': True, 'new_review': True,
            'context': context, 'suggestion': suggestion}


@router.get("/history")
def list_history(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: Literal["all", "applied", "rejected", "ignored", "deleted", "superseded"] = "all",
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
        for field, output in (("applied_actions_json", "applied_actions"),
                              ("applied_tags_json", "applied_tags")):
            raw = item.pop(field, None)
            try:
                decoded = json.loads(raw) if raw is not None else None
                item[output] = decoded if isinstance(decoded, list) else None
            except (ValueError, TypeError):
                item[output] = None
        item["paperless_candidate"] = bool(item["paperless_candidate"])
        item["confidence"] = float(item["confidence"] or 0)
        item["file_id"] = str(item["file_id"] or "")
        display_path = (item["original_path"] or item["applied_path"] or
                        item["last_known_path"] or "")
        item["name"] = PurePosixPath(display_path).name
        item["deleted"] = item["deleted_at"] is not None or item["status"] == "deleted"
        # A historical move may have overwritten files.path. Do not falsely
        # label it an original path when no original snapshot was saved.
        items.append(item)

    return {"items": items, "count": count, "limit": limit,
            "offset": offset, "status": status}
