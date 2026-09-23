from __future__ import annotations

import threading
import logging
from pathlib import PurePosixPath
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()
log = logging.getLogger("exapp.file_action")

_CONTEXTS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()


def _normalize_directory(directory: str) -> str:
    directory = str(directory or "").strip().replace("\\", "/")
    if not directory or directory == ".":
        return "/"
    if not directory.startswith("/"):
        directory = "/" + directory
    return directory.rstrip("/") or "/"


def _build_path(directory: str, name: str) -> str:
    directory = _normalize_directory(directory)
    name = PurePosixPath(str(name or "").replace("\\", "/")).name
    if not name:
        raise ValueError("File action payload is missing a filename.")
    return f"/{name}" if directory == "/" else f"{directory}/{name}"


def remember_context(payload: Dict[str, Any]) -> Dict[str, Any]:
    file_id = str(payload.get("fileId") or payload.get("file_id") or "").strip()
    if not file_id:
        raise ValueError("File action payload is missing fileId.")

    file_type = str(payload.get("fileType") or payload.get("type") or "file").lower()
    if file_type not in {"file", "document"}:
        raise ValueError("AI Organize currently supports files only, not folders.")

    context = {
        "file_id": file_id,
        "name": str(payload.get("name") or "").strip(),
        "directory": _normalize_directory(payload.get("directory") or "/"),
        "etag": str(payload.get("etag") or "").strip('"'),
        "mime_type": str(payload.get("mime") or payload.get("mime_type") or ""),
        "size": int(payload.get("size") or 0),
        "user_id": str(payload.get("userId") or "").strip(),
        "permissions": payload.get("permissions"),
    }
    context["path"] = _build_path(context["directory"], context["name"])

    with _LOCK:
        _CONTEXTS[file_id] = context
    return dict(context)


def get_context(file_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        value = _CONTEXTS.get(str(file_id))
        return dict(value) if value else None


def update_context(file_id: str, **changes: Any) -> None:
    with _LOCK:
        context = _CONTEXTS.get(str(file_id))
        if not context:
            return
        context.update(changes)
        if "name" in changes or "directory" in changes:
            context["path"] = _build_path(
                context.get("directory", "/"),
                context.get("name", ""),
            )


@router.post("/file-action")
async def file_action(request: Request) -> Dict[str, str]:
    log.info("Entered /file-action")

    try:
        payload = await request.json()
        log.info("Received payload: %s", payload)

        if not isinstance(payload, dict):
            raise ValueError("Invalid file action payload.")

        # Nextcloud may send the file inside a "files" array.
        if "files" in payload:
            files = payload["files"]

            if not isinstance(files, list) or not files:
                raise ValueError("No files selected.")

            if len(files) != 1:
                raise ValueError(
                    "AI Organize currently supports one file at a time."
                )

            file_info = files[0]
        else:
            # Support the original flat payload format.
            file_info = payload

        if not isinstance(file_info, dict):
            raise ValueError("Invalid selected file information.")

        # Store the actual file information, not the wrapper.
        context = remember_context(file_info)

        log.info(
            "File context cached: ID=%s, Path=%s",
            context["file_id"],
            context["path"],
        )

    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    # Keep your existing redirect unchanged.
    return {"redirect_handler": "organizer"}


@router.get("/api/context/{file_id}")
def context(file_id: str) -> Dict[str, Any]:
    value = get_context(file_id)
    if not value:
        raise HTTPException(
            status_code=404,
            detail=(
                "The file-action context is no longer cached. "
                "Open the file menu and choose AI Organize again."
            ),
        )
    return value
