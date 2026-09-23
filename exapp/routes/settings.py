"""Admin-only via info.xml: Nextcloud AppAPI enforces ADMIN before proxying."""
from __future__ import annotations

import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from python_organizer_local_llm.file_types import catalog

router = APIRouter(prefix='/api/settings', tags=['settings'])


class SettingsUpdate(BaseModel):
    settings: dict


def _service(request):
    return request.app.state.settings


@router.get('')
def get_settings(request: Request):
    service = _service(request)
    return {'settings': service.get(), 'automation': service.organizer.database.get_automation_run(),
            'file_types_catalog': catalog()}


@router.put('')
def save_settings(body: SettingsUpdate, request: Request):
    try:
        return {'settings': _service(request).save(body.settings)}
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get('/models')
def list_models(request: Request):
    v = _service(request).get()
    try:
        response = requests.get(f"{v['ollama_url']}/api/tags", timeout=min(v['timeout'], 10))
        response.raise_for_status()
        data = response.json()
        return {'connected': True, 'models': [x['name'] for x in data.get('models', [])
                                              if isinstance(x, dict) and x.get('name')]}
    except (requests.RequestException, ValueError, TypeError) as exc:
        raise HTTPException(status_code=502, detail=f'Ollama connection failed: {exc}') from exc


@router.get('/folders')
def list_folders(request: Request):
    client = _service(request).organizer.nextcloud
    try:
        folders = ['/']
        for item in client._propfind('/', depth='infinity'):
            path = item.get('path')
            if item.get('is_directory') and path and path not in folders:
                folders.append(path)
        return {'folders': sorted(folders, key=str.casefold)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail='Unable to retrieve Nextcloud folders') from exc
