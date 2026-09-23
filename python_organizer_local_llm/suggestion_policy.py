"""Deterministic checks: reject obviously unrelated folder paths and fill basic tags.
Corrections lower confidence so scheduled Automatic Apply cannot act on them.
"""
from __future__ import annotations
import re
from pathlib import PurePosixPath

EXPORT = {'takeout', 'google takeout', 'backup', 'backups', 'appdata', 'system backup', 'configurations'}
SOURCE = {'ai inbox', 'ai ignored'}
GENERIC = {'', 'unknown', 'unsorted', 'document', 'file', 'other', 'misc'}


def components(path):
    return tuple(piece.casefold().replace('_', ' ').replace('-', ' ')
                 for piece in PurePosixPath(str(path or '/')).parts if piece != '/')


def is_export(path):
    return any(part in EXPORT for part in components(path))


def is_resume(category, filename):
    text = str(category or '') + ' ' + PurePosixPath(filename).stem
    return bool(re.search(r'(?:^|[\W_])(?:resume|curriculum vitae|cv)(?:[\W_]|$)', text, re.I))


def improve_suggestion(suggestion, *, file_path, existing_folders=(), existing_tags=(), paperless_inbox='/inbox'):
    # Apply folder/tag quality safeguards to the saved Nextcloud alternative,
    # even when Paperless is the preferred destination.
    proposed = str(suggestion.get('suggested_folder') or '')
    parts = components(proposed)
    category = str(suggestion.get('category') or '').strip().casefold()
    resume = is_resume(category, PurePosixPath(file_path).name)
    note = ''
    paperless_root = str(paperless_inbox or '/inbox').rstrip('/').casefold()
    unsafe_paperless = (proposed.casefold().rstrip('/') == paperless_root
                        or proposed.casefold().startswith(paperless_root + '/')
                        or 'paperless media' in parts)
    if resume and (is_export(proposed) or any(part in SOURCE for part in parts)
                   or not any(part in {'resumes', 'resume', 'cv', 'curriculum vitae'} for part in parts)):
        matches = [path for path in existing_folders if
                   not is_export(path) and not any(part in SOURCE for part in components(path))
                   and any(part in {'resume', 'resumes', 'cv', 'curriculum vitae'} for part in components(path))]
        suggestion['suggested_folder'] = (sorted(matches, key=lambda x: (len(components(x)), x.casefold()))[0]
                                          if matches else '/Documents/Resumes')
        note = 'Resume destination corrected; verify the proposed folder manually.'
    elif (unsafe_paperless or any(part in SOURCE for part in parts)
          or (is_export(proposed) and not is_export(file_path))):
        suggestion['suggested_folder'] = '/Documents/Unsorted'
        note = 'Unrelated inbox/export destination rejected; choose an appropriate folder manually.'
    if not note and str(suggestion.get('suggested_folder') or '').rstrip('/') not in {
            str(folder).rstrip('/') for folder in existing_folders}:
        note = 'Destination was not found among existing folders; review before creating it.'
    if note:
        suggestion['confidence'] = min(float(suggestion.get('confidence') or 0), .69)
        suggestion['reason'] = (str(suggestion.get('reason') or '') + ' ' + note).strip()
    if not suggestion.get('tags'):
        available = {str(t).strip().casefold().replace('_', ' '): str(t).strip()
                     for t in existing_tags if str(t).strip()}
        candidates = ('resume', 'cv') if resume else (category,)
        match = next((available[t] for t in candidates if t in available), None)
        if match:
            suggestion['tags'] = [match]
        elif category not in GENERIC and re.fullmatch(r'[\w -]{2,48}', category):
            suggestion['tags'] = [category.replace('_', ' ')]
    return suggestion
