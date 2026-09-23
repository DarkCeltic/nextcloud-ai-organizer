"""Apply reviewed Nextcloud changes, recording each completed step durably."""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from exapp.routes.file_action import get_context, update_context

router = APIRouter()


class ApplyRequest(BaseModel):
    suggestion_id: int
    actions: List[Literal["filename", "folder", "tags"]] = Field(
        default_factory=lambda: ["filename", "folder", "tags"]
    )
    suggested_filename: Optional[str] = None
    suggested_folder: Optional[str] = None
    tags: Optional[List[str]] = None
    # Explicit choice required: a cached old UI must NEVER silently select the other route.
    destination: Literal['nextcloud', 'paperless']


def _safe_filename(name: str) -> str:
    name = str(name or "").strip()
    if not name or name in {".", ".."}:
        raise ValueError("Filename cannot be empty.")
    if any(char in name for char in ("/", "\\", "\x00")):
        raise ValueError("Filename contains an invalid character.")
    return name


def _safe_folder(path: str) -> str:
    path = str(path or "").strip().replace("\\", "/")
    if not path.startswith("/"):
        path = "/" + path
    parts = [part for part in path.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("Folder path cannot contain '.' or '..'.")
    return "/" + "/".join(parts) if parts else "/"


def _is_inside(path: str, root: str) -> bool:
    path = _safe_folder(path).casefold()
    root = _safe_folder(root).casefold()
    return path == root or path.startswith(root.rstrip("/") + "/")


def _refresh_context(organizer, file_id: str, current_path: str) -> None:
    """Context is a convenience cache; a failure must not undo a committed move."""
    try:
        directory = str(PurePosixPath(current_path).parent)
        update_context(
            file_id,
            path=current_path,
            name=PurePosixPath(current_path).name,
            directory=directory if directory != "." else "/",
        )
    except Exception:
        organizer.log.warning("Could not refresh file-action context for %s", file_id,
                              exc_info=True)


@router.post("/api/apply/{file_id}")
def apply_suggestion(file_id: str, body: ApplyRequest, request: Request) -> Dict[str, Any]:
    organizer = request.app.state.organizer
    database = organizer.database
    database.initialize()

    if not file_id.isdecimal():
        raise HTTPException(status_code=400, detail="Invalid Nextcloud file ID.")

    # Prefer a stable ID: file paths can change between Apply clicks.
    db_file = database.get_file_by_nextcloud_id(file_id)
    context = get_context(file_id)
    if db_file:
        current_path = db_file["path"]
    elif context:
        current_path = context["path"]
        db_file = database.get_file(current_path)
    else:
        try:
            resolved = organizer.nextcloud.find_file_by_id(file_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Unable to resolve Nextcloud file.") from exc
        if not resolved:
            raise HTTPException(status_code=404, detail="Nextcloud file not found.")
        current_path = resolved["path"]
        db_file = database.get_file(current_path)

    if not db_file or str(db_file.get("nextcloud_file_id") or file_id) != file_id:
        raise HTTPException(status_code=404, detail="No AI analysis exists for this file.")
    if db_file.get("deleted_at") or db_file.get("ignored"):
        raise HTTPException(status_code=410, detail="This file is archived or ignored.")

    suggestion = database.get_suggestion(body.suggestion_id)
    if not suggestion or int(suggestion["file_id"]) != int(db_file["id"]):
        raise HTTPException(status_code=404, detail="Suggestion does not belong to this file.")
    if suggestion["status"] not in {"pending", "accepted"}:
        raise HTTPException(status_code=409, detail="Suggestion is no longer actionable.")
    selected = suggestion.get('selected_destination')
    if selected and selected != body.destination:
        raise HTTPException(status_code=409, detail='A different destination has already been partially applied. Complete or re-analyze before switching.')

    actions = set(body.actions)
    if not actions:
        raise HTTPException(status_code=400, detail="Select at least one Apply action.")
    paperless = body.destination == 'paperless'
    if paperless and (not organizer.paperless_enabled or not suggestion['paperless_candidate']):
        raise HTTPException(status_code=409, detail='Paperless integration is disabled or not recommended for this file.')
    if not paperless and suggestion['paperless_candidate'] and not suggestion.get('dual_options_ready'):
        raise HTTPException(status_code=409, detail='Legacy suggestion has no Nextcloud alternative. Re-analyze this file first.')
    if paperless and organizer.classifier.is_never_paperless(
        suggestion.get("category", ""), PurePosixPath(current_path).name,
    ):
        raise HTTPException(
            status_code=409,
            detail="This document is excluded from Paperless. Re-analyze it to generate a Nextcloud destination.",
        )
    if paperless:
        actions = {"folder"}

    # Validate only actions actually selected. A Paperless suggestion does not
    # require a valid proposed filename because it never renames the file.
    try:
        filename = None
        if "filename" in actions:
            filename = _safe_filename(
                body.suggested_filename if body.suggested_filename is not None
                else suggestion["suggested_filename"]
            )
            extension = PurePosixPath(current_path).suffix
            if extension and PurePosixPath(filename).suffix.lower() != extension.lower():
                raise ValueError(f"The file extension must remain {extension}.")

        folder = None
        if "folder" in actions:
            folder = (_safe_folder(organizer.paperless_inbox) if paperless else
                      _safe_folder(body.suggested_folder if body.suggested_folder is not None
                                   else suggestion["suggested_folder"]))
            if not paperless:
                blocked = ("/paperless-media", _safe_folder(organizer.paperless_inbox))
                if any(_is_inside(folder, location) for location in blocked):
                    raise ValueError(f"Invalid destination: {folder}. Choose a normal Nextcloud folder.")
        tags = (body.tags if body.tags is not None else suggestion.get("tags", []))
        tags = [str(tag).strip() for tag in tags if str(tag).strip()]
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db_file_id = int(db_file["id"])
    cumulative = []
    try:
        # Persist each step immediately. If a later step fails, Review retains
        # the partial progress and SQLite retains the actual confirmed path.
        if "folder" in actions:
            organizer.nextcloud.ensure_folder(folder)
            destination = folder.rstrip("/") + "/" + PurePosixPath(current_path).name
            if destination != current_path:
                current_path = organizer.nextcloud.move_file(
                    current_path, destination, overwrite=False
                ) or destination
            progress = database.record_apply_progress(
                db_file_id, body.suggestion_id, current_path, {"folder"}, paperless=paperless
            )
            cumulative = progress["applied_actions"]
            _refresh_context(organizer, file_id, current_path)

        if "filename" in actions and not paperless:
            destination = str(PurePosixPath(current_path).with_name(filename))
            if destination != current_path:
                current_path = organizer.nextcloud.move_file(
                    current_path, destination, overwrite=False
                ) or destination
            progress = database.record_apply_progress(
                db_file_id, body.suggestion_id, current_path, {"filename"}
            )
            cumulative = progress["applied_actions"]
            _refresh_context(organizer, file_id, current_path)

        if "tags" in actions and not paperless:
            if tags:
                organizer.nextcloud.assign_tags(file_id=file_id, tags=tags, create_missing=True)
            # Empty checkbox selection means no tags requested. This does not
            # remove existing Nextcloud tags (assign_tags is additive).
            progress = database.record_apply_progress(
                db_file_id, body.suggestion_id, current_path, {"tags"}, tags=tags
            )
            cumulative = progress["applied_actions"]

        return {
            "ok": True,
            "path": current_path,
            "paperless_candidate": bool(suggestion['paperless_candidate']),
            "selected_destination": body.destination,
            "applied": sorted(actions),
            "applied_actions": cumulative,
            "complete": progress["complete"],
        }
    except HTTPException:
        raise
    except Exception as exc:
        organizer.log.exception("Failed to apply suggestion %s; last known path %s",
                                body.suggestion_id, current_path)
        raise HTTPException(
            status_code=502,
            detail=f"Apply failed: {exc}. Last known path: {current_path}. Refresh Review before retrying.",
        ) from exc
