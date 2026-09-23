"""Conservative credential guard. Never send detected credential content to an LLM.

This is a protective heuristic, NOT a guarantee that every secret will be detected.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

_CREDENTIAL_NAME = re.compile(
    r"(?:^|[._-])(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"secret|secrets|credentials?|passwords?|private[_-]?key|"
    r"cloudflare[_-]?token)(?:[._-]|$)", re.IGNORECASE
)
_CREDENTIAL_TEXT = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|"
    r"auth[_-]?token|password)\b\s*[:=]\s*['\"]?[^\s'\"]{16,})",
    re.IGNORECASE,
)


def sensitive_filename(path: str) -> bool:
    name = PurePosixPath(str(path)).name
    return name.lower() in {'.env', '.npmrc', '.pypirc', 'id_rsa', 'id_ed25519'} or bool(
        _CREDENTIAL_NAME.search(name)
    ) or PurePosixPath(name).suffix.lower() in {'.pem', '.key', '.p12', '.pfx'}


def sensitive_content(content: str) -> bool:
    return bool(_CREDENTIAL_TEXT.search(str(content or '')[:100000]))


def is_sensitive(path: str, content: str = '') -> bool:
    return sensitive_filename(path) or sensitive_content(content)


def safe_suggestion(path: str) -> dict:
    """Offer non-destructive metadata based on filename, never on secret values."""
    file = PurePosixPath(path)
    known_provider = 'Cloudflare' if 'cloudflare' in file.name.casefold() else None
    basename = f'{known_provider}_API_Token' if known_provider else 'Credential_File'
    extension = file.suffix
    filename = basename + extension
    # Keep the location unchanged; moving a credential needs explicit review.
    return {
        'suggested_filename': filename,
        'suggested_folder': '/Security/Credentials',
        'tags': ([known_provider.lower(), 'credential'] if known_provider else ['credential']),
        'category': 'credential',
        'paperless_candidate': False,
        'confidence': 0.0,  # Never eligible for scheduled automatic apply.
        'reason': 'Potential credential detected. Contents were not sent to the AI. /Security/Credentials is a proposed folder, not an existing or verified destination; review it manually.',
    }
