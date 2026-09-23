from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request

from exapp.routes.file_action import get_context
from python_organizer_local_llm.sensitive import sensitive_filename, safe_suggestion
from python_organizer_local_llm.ocr import OCRProcessingError

router = APIRouter()


def _services(request: Request):
    return request.app.state.organizer


def _public_suggestion(suggestion_id: int, path: str, suggestion: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "suggestion_id": suggestion_id,
        "original_path": path,
        "suggested_filename": suggestion.get("suggested_filename", ""),
        "suggested_folder": suggestion.get("suggested_folder", ""),
        "tags": suggestion.get("tags", []),
        "category": suggestion.get("category", ""),
        "paperless_candidate": bool(suggestion.get("paperless_candidate", False)),
        "dual_options_ready": bool(suggestion.get("dual_options_ready", True)),
        "selected_destination": suggestion.get("selected_destination"),
        "confidence": float(suggestion.get("confidence", 0.0)),
        "reason": suggestion.get("reason", ""),
        "paperless_enabled": bool(suggestion.get("paperless_enabled", False)),
        "paperless_inbox": suggestion.get("paperless_inbox", ""),
        "ocr_used": bool(suggestion.get("ocr_used", False)),
    }


def generate_suggestion(organizer, path: str, database_file_id: int | None = None,
                        force_nextcloud: bool = False) -> Dict[str, Any]:
    """Classify a file without applying changes. Used for Analyze and Keep in Nextcloud."""
    filename = PurePosixPath(path).name
    if sensitive_filename(path):
        suggestion = safe_suggestion(path)
        organizer._validate_suggestion(suggestion)
        return suggestion

    try:
        # Compatibility with custom Nextcloud mocks/clients still exposing only
        # get_file_text(). The real client provides OCR provenance and caching.
        file_record = organizer.database.get_file_by_id(database_file_id) if database_file_id is not None else None
        extractor = getattr(organizer.nextcloud, 'get_file_text_with_metadata', None)
        if callable(extractor):
            extracted = extractor(path, file_id=str(file_record['nextcloud_file_id'] or '') if file_record else '',
                                  etag=str(file_record['etag'] or '') if file_record else '')
        else:
            extracted = {'text': organizer.nextcloud.get_file_text(path), 'ocr_used': False}
        content = extracted['text']
        if not content or not content.strip():
            raise OCRProcessingError(
                'OCR: no readable content after PDF processing; preview and review manually.'
                if path.lower().endswith('.pdf') else
                'Extraction: no readable text found; preview and review manually.'
            )
    except OCRProcessingError as exc:
        if database_file_id is not None:
            file_record = organizer.database.get_file_by_id(database_file_id)
            organizer.database.record_analysis_failure(
                file_record['nextcloud_file_id'], path, str(exc),
                'ocr' if str(exc).startswith('OCR:') else 'extract'
            )
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        folders = organizer.nextcloud.get_folder_tree()
    except Exception:
        organizer.log.exception('Unable to retrieve folder tree; continuing with none.')
        folders = []
    try:
        tags = organizer.nextcloud.get_tags()
    except Exception:
        organizer.log.exception('Unable to retrieve system tags; continuing with none.')
        tags = []

    # Rejection should not generate repeated full-content LLM calls. Limit
    # its one inference without changing settings for other concurrent requests.
    if force_nextcloud:
        with organizer.classifier.quick_mode():
            suggestion = organizer.classifier.classify(
                filename=filename, file_path=path, content=content,
                existing_folders=folders, existing_tags=tags,
                force_nextcloud=True,
            )
    else:
        suggestion = organizer.classifier.classify(
            filename=filename, file_path=path, content=content,
            existing_folders=folders, existing_tags=tags,
            force_nextcloud=False,
        )
    suggestion = organizer.classifier._apply_paperless_policy(suggestion, filename, content)
    if force_nextcloud:
        suggestion['paperless_candidate'] = False
        folder = str(suggestion.get('suggested_folder') or '').rstrip('/').casefold()
        blocked = (organizer.paperless_inbox.rstrip('/').casefold(), '/paperless-media')
        if any(folder == root or folder.startswith(root + '/') for root in blocked if root):
            suggestion['suggested_folder'] = '/Documents/Unsorted'
    suggestion['ocr_used'] = bool(extracted['ocr_used'])
    organizer._validate_suggestion(suggestion)
    return suggestion


@router.post("/api/analyze/{file_id}")
def analyze_file(
    file_id: str,
    request: Request,
    force: bool = Query(default=False),
    manual_review_only: bool = False,
    supersede_active: bool = False,
) -> Dict[str, Any]:
    organizer = _services(request)
    context = get_context(file_id)

    if context:
        file_info = {
            "file_id": file_id,
            "path": context["path"],
            "etag": context.get("etag", ""),
            "mime_type": context.get("mime_type", ""),
            "size": context.get("size", 0),
        }
    else:
        try:
            file_info = organizer.nextcloud.find_file_by_id(file_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Unable to resolve Nextcloud file: {exc}") from exc
        if not file_info:
            raise HTTPException(status_code=404, detail="Nextcloud file not found.")

    path = file_info["path"]
    if not organizer.nextcloud.is_supported_file(path):
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {PurePosixPath(path).suffix or '(none)'}",
        )

    organizer.database.initialize()
    existing_file = organizer.database.get_file(path)
    existing_db_id = int(existing_file["id"]) if existing_file else None
    if existing_file and (existing_file.get('ignored') or existing_file.get('deleted_at')):
        raise HTTPException(status_code=409, detail='File is ignored or archived; reopen it from History first.')
    if not force and existing_file and existing_db_id:
        latest = organizer.database.get_latest_suggestion(existing_db_id)
        if (
            latest
            and latest.get("status") in {"pending", "accepted"}
            and str(existing_file.get("etag") or "")
            and str(existing_file.get("etag") or "") == str(file_info.get("etag") or "")
        ):
            return _public_suggestion(latest["id"], path, {
                **latest, 'paperless_enabled': organizer.paperless_enabled,
                'paperless_inbox': organizer.paperless_inbox,
            })

    database_file_id = organizer.database.upsert_file(
        nextcloud_file_id=file_id,
        path=path,
        etag=file_info.get("etag", ""),
        mime_type=file_info.get("mime_type", ""),
        size=file_info.get("size", 0),
    )

    try:
        suggestion = generate_suggestion(
            organizer, path, database_file_id,
            force_nextcloud=False,
        )
        suggestion_id = organizer.database.save_suggestion(
            file_id=database_file_id,
            suggested_filename=suggestion["suggested_filename"],
            suggested_folder=suggestion["suggested_folder"],
            tags=suggestion.get("tags", []),
            category=suggestion.get("category", ""),
            paperless_candidate=suggestion.get("paperless_candidate", False),
            confidence=suggestion.get("confidence", 0.0),
            reason=suggestion.get("reason", ""),
            status="pending",
            manual_review_only=manual_review_only or bool(suggestion.get("ocr_used")),
            ocr_used=bool(suggestion.get("ocr_used")),
            supersede_active=supersede_active,
        )
        organizer.database.mark_processed(database_file_id)
        organizer.database.resolve_analysis_failure(file_id)
        return _public_suggestion(suggestion_id, path, {
            **suggestion, 'paperless_enabled': organizer.paperless_enabled,
            'paperless_inbox': organizer.paperless_inbox,
        })

    except HTTPException:
        raise
    except Exception as exc:
        organizer.log.exception("On-demand analysis failed for %s", path)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
