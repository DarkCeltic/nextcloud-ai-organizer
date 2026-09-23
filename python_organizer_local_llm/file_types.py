"""Single source of truth for supported file types, user labels and AppAPI MIME types.

Nextcloud MIME values describe a file; the final processing decision uses its
known, supported extension. Generic/misreported MIME types cannot turn an
unselected filename into an eligible file. Unsupported binary formats are not
advertised merely because Nextcloud labels them application/octet-stream.
"""
from __future__ import annotations

from pathlib import PurePosixPath

from python_organizer_local_llm.sensitive import sensitive_filename

# Entries are ordered for predictable display and stable API output. Each
# extension has a real extraction path in NextcloudClient.
FILE_TYPES = (
    ('pdf', 'PDF documents', ('.pdf',), ('application/pdf',), 'Searchable PDFs and locally OCR-scanned PDFs'),
    ('docx', 'Word documents', ('.docx',), ('application/vnd.openxmlformats-officedocument.wordprocessingml.document',), 'Modern Microsoft Word files'),
    ('odt', 'OpenDocument text', ('.odt',), ('application/vnd.oasis.opendocument.text',), 'LibreOffice / OpenDocument text'),
    ('rtf', 'Rich Text', ('.rtf',), ('application/rtf', 'text/rtf', 'application/x-rtf'), 'Rich Text documents'),
    ('xlsx', 'Excel spreadsheets', ('.xlsx',), ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',), 'Excel workbook cell text and values'),
    ('csv', 'CSV spreadsheets', ('.csv',), ('text/csv', 'application/csv', 'text/plain'), 'Comma-separated text'),
    ('txt', 'Plain text', ('.txt',), ('text/plain',), 'Plain text documents'),
    ('md', 'Markdown', ('.md', '.markdown'), ('text/markdown', 'text/x-markdown', 'text/plain'), 'Markdown notes'),
    ('log', 'Log files', ('.log',), ('text/plain', 'text/x-log'), 'Plain-text logs'),
    ('json', 'JSON files', ('.json',), ('application/json', 'text/json', 'text/plain'), 'JSON text'),
    ('xml', 'XML files', ('.xml',), ('application/xml', 'text/xml', 'text/plain'), 'XML text'),
    ('yaml', 'YAML files', ('.yaml', '.yml'), ('application/yaml', 'text/yaml', 'text/x-yaml', 'text/plain'), 'YAML configuration text; check for secrets before enabling'),
    ('credential', 'Credential / secret filenames', (), (), 'Special safe suggestions; recognized filenames are never sent to the AI'),
)

BY_ID = {item[0]: item for item in FILE_TYPES}
BY_EXTENSION = {extension: item[0] for item in FILE_TYPES for extension in item[2]}
DEFAULT_FILE_TYPES = ('pdf', 'docx', 'odt', 'rtf', 'xlsx', 'csv', 'txt', 'md', 'log', 'json', 'xml', 'credential')


def catalog():
    """Nontechnical labels, paired with exact accepted extensions and MIME types."""
    return [{'id': ident, 'label': label, 'extensions': list(extensions),
             'mime_types': list(mimes), 'description': description}
            for ident, label, extensions, mimes, description in FILE_TYPES]


def validate_ids(ids):
    if not isinstance(ids, list):
        raise ValueError('file_types must be a list of selected file types')
    if any(not isinstance(value, str) or value not in BY_ID for value in ids):
        raise ValueError('Unknown file type. Choose a supported type from Settings.')
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate file types are not allowed')
    return [item[0] for item in FILE_TYPES if item[0] in ids]


def from_extensions(extensions):
    """Migrate the existing scanner.allowed_extensions defaults from YAML.

    If an old configuration explicitly lists no extensions, select no types.
    Settings stored in SQLite take precedence over this bootstrap mapping.
    """
    if extensions is None:
        return list(DEFAULT_FILE_TYPES)
    if not isinstance(extensions, list):
        raise ValueError('scanner.allowed_extensions must be a list')
    selected = set()
    for entry in extensions:
        if not isinstance(entry, str):
            raise ValueError('scanner.allowed_extensions must contain text extensions')
        ext = '.' + entry.strip().lower().lstrip('.')
        if ext in BY_EXTENSION:
            selected.add(BY_EXTENSION[ext])
    # Preserve earlier safe handling of extensionless credential filenames.
    if extensions:
        selected.add('credential')
    return [item[0] for item in FILE_TYPES if item[0] in selected]


def extensions_for(ids):
    selected = set(validate_ids(ids))
    return {ext for item in FILE_TYPES if item[0] in selected for ext in item[2]}


def supported(path, ids=None, mime_type=''):
    """Check actual filename type, never a MIME-only wildcard.

    Some Nextcloud installations return application/octet-stream or text/plain
    for various known formats; in those cases the supported extension remains
    the reliable discriminator. The MIME parameter is accepted for callers that
    have DAV metadata, but must not override the filename's selected type.
    """
    extension = PurePosixPath(str(path or '')).suffix.lower()
    type_id = BY_EXTENSION.get(extension)
    if type_id is None:
        type_id = 'credential' if sensitive_filename(path) else None
    if type_id is None:
        return False
    if ids is None:
        return True
    return type_id in ids


def app_action_mimes():
    """Register the union once, so changing saved settings needs no re-register.

    Each action is still checked against current settings by the backend.
    """
    return ','.join(dict.fromkeys(mime for item in FILE_TYPES for mime in item[3]))
