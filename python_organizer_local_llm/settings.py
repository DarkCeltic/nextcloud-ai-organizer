"""Persisted, validated admin settings. Deployment credentials remain outside SQLite."""
from __future__ import annotations

import copy
import math
import re
import threading
from urllib.parse import urlsplit

from python_organizer_local_llm.file_types import (
    extensions_for, from_extensions, validate_ids,
)


class SettingsService:
    def __init__(self, organizer):
        self.organizer = organizer
        self.lock = threading.RLock()
        self.defaults = self._defaults()
        stored = organizer.database.load_settings()
        self.values = self.validate({**self.defaults, **stored})
        self._apply(self.values)

    def _defaults(self):
        config = self.organizer.classifier.config
        ollama = config.get('ollama', {})
        classifier = config.get('classifier', {})
        ocr = config.get('ocr', {})
        scanner = self.organizer.scanner
        prefer_send = config.get('paperless', {}).get('prefer_send') or []
        if isinstance(prefer_send, str):
            prefer_send = [prefer_send]
        return {
            'ollama_url': str(ollama.get('url', 'http://localhost:11434')),
            'model': str(ollama.get('model', 'qwen2.5:7b')),
            'timeout': int(ollama.get('timeout', 180)),
            'temperature': float(ollama.get('temperature', 0.1)),
            'max_content_chars': int(classifier.get('max_content_chars', 8000)),
            'ocr_enabled': bool(ocr.get('enabled', True)),
            'ocr_max_pages': int(ocr.get('max_pages', 10)),
            'file_types': from_extensions(config.get('scanner', {}).get('allowed_extensions')),
            'scan_paths': list(scanner.scan_paths),
            'exclude_paths': list(scanner.exclude_paths),
            'schedule_enabled': False,
            'interval_minutes': 60,
            'auto_analyze': False,
            'auto_apply': False,
            'auto_apply_warning_accepted': False,
            'minimum_auto_confidence': 0.95,
            'global_instructions': '',
            'folder_rules': [],
            'paperless_enabled': bool(config.get('paperless', {}).get('enabled', False)),
            'paperless_inbox': str(config.get('paperless', {}).get('inbox_path', '/inbox')),
            'paperless_prefer_send': list(prefer_send),
        }

    @staticmethod
    def _paths(raw, name, allow_empty=False):
        if not isinstance(raw, list) or len(raw) > 100:
            raise ValueError(f'{name} must be a list of at most 100 folders')
        clean = []
        for value in raw:
            if not isinstance(value, str):
                raise ValueError(f'{name} must contain folder paths')
            text = '/' + '/'.join(part for part in value.strip().replace('\\', '/').split('/') if part)
            if any(part in {'.', '..'} for part in text.split('/')):
                raise ValueError(f'{name} contains an invalid folder')
            if len(text) > 1024:
                raise ValueError(f'{name} contains a path that is too long')
            if text not in clean:
                clean.append(text)
        if not clean and not allow_empty:
            raise ValueError(f'{name} must contain at least one folder')
        return clean

    @staticmethod
    def _inside(path, folder):
        return folder == '/' or path == folder or path.startswith(folder.rstrip('/') + '/')

    @classmethod
    def validate(cls, incoming):
        allowed = {
            'ollama_url', 'model', 'timeout', 'temperature', 'max_content_chars', 'scan_paths',
            'exclude_paths', 'schedule_enabled', 'interval_minutes', 'auto_analyze',
            'auto_apply', 'auto_apply_warning_accepted', 'minimum_auto_confidence',
            'global_instructions', 'folder_rules', 'paperless_enabled', 'paperless_inbox',
            'paperless_prefer_send', 'ocr_enabled', 'ocr_max_pages', 'file_types'
        }
        if not isinstance(incoming, dict) or set(incoming) != allowed:
            raise ValueError('Settings payload is missing keys or contains unknown settings')
        v = copy.deepcopy(incoming)
        v['file_types'] = validate_ids(v['file_types'])
        url = str(v['ollama_url']).strip().rstrip('/')
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.path not in ('', '/') or parts.query or parts.fragment:
            raise ValueError('Ollama URL must be an http(s) host and optional port, without credentials or path')
        v['ollama_url'] = url
        v['model'] = str(v['model']).strip()
        if not 1 <= len(v['model']) <= 150 or any(c.isspace() for c in v['model']):
            raise ValueError('Select a valid Ollama model name')
        for name, low, high in [('timeout', 5, 1800), ('max_content_chars', 500, 100000),
                                ('interval_minutes', 1, 10080), ('ocr_max_pages', 1, 100)]:
            if isinstance(v[name], bool) or not isinstance(v[name], int) or not low <= v[name] <= high:
                raise ValueError(f'{name} must be between {low} and {high}')
        temperature = v['temperature']
        if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                or not math.isfinite(temperature) or not 0 <= temperature <= 2):
            raise ValueError('Ollama temperature must be a finite number between 0 and 2')
        v['temperature'] = float(temperature)
        for name in ('schedule_enabled', 'auto_analyze', 'auto_apply', 'auto_apply_warning_accepted', 'ocr_enabled'):
            if not isinstance(v[name], bool):
                raise ValueError(f'{name} must be true or false')
        if float(v['minimum_auto_confidence']) != 0.95:
            raise ValueError('Automatic Apply requires at least 95% confidence')
        v['minimum_auto_confidence'] = 0.95
        if v['auto_apply'] and not v['auto_apply_warning_accepted']:
            raise ValueError('You must acknowledge the file-movement warning before enabling Automatic Apply')
        # Advisory category preferences only: explicit never_send and credential
        # safeguards remain authoritative. Normalize when saved, so restarts are stable.
        raw_preferences = v['paperless_prefer_send']
        if not isinstance(raw_preferences, list) or len(raw_preferences) > 100:
            raise ValueError('Paperless preferred categories must be a list of at most 100 entries')
        normalized = []
        for category in raw_preferences:
            if not isinstance(category, str) or len(category) > 100:
                raise ValueError('Paperless preferred categories must be short text entries')
            entry = category.strip()
            if not re.fullmatch(r'[A-Za-z0-9]+(?:[ _-][A-Za-z0-9]+)*', entry):
                raise ValueError('Paperless preferred categories may contain letters, numbers, spaces, _ and -')
            policy_name = entry.lower().replace('-', '_').replace(' ', '_')
            if policy_name not in normalized:
                normalized.append(policy_name)
        v['paperless_prefer_send'] = normalized
        if not isinstance(v['paperless_enabled'], bool):
            raise ValueError('paperless_enabled must be true or false')
        if not v['paperless_enabled'] and not str(v['paperless_inbox'] or '').strip():
            v['paperless_inbox'] = '/inbox'
        v['paperless_inbox'] = cls._paths([v['paperless_inbox']], 'Paperless consume folder')[0]
        if v['paperless_inbox'] == '/' or v['paperless_inbox'].casefold() == '/ai inbox':
            raise ValueError('Paperless consume folder must not be the Files root or AI Inbox')
        v['scan_paths'] = cls._paths(v['scan_paths'], 'scan_paths')
        if v['paperless_enabled'] and any(root != '/' and cls._inside(v['paperless_inbox'], root)
                                          for root in v['scan_paths']):
            raise ValueError('Paperless consume folder must not be inside a scan root')
        v['exclude_paths'] = cls._paths(v['exclude_paths'], 'exclude_paths', allow_empty=True)
        if '/' in v['exclude_paths']:
            raise ValueError('Cannot exclude the entire Nextcloud Files root')
        for scan in v['scan_paths']:
            if any(cls._inside(scan, excluded) for excluded in v['exclude_paths']):
                raise ValueError(f'Scan root {scan} is inside an excluded folder')
        instructions = v['global_instructions']
        if not isinstance(instructions, str) or len(instructions) > 8000:
            raise ValueError('Global instructions must be at most 8000 characters')
        rules = v['folder_rules']
        if not isinstance(rules, list) or len(rules) > 50:
            raise ValueError('folder_rules must be a list of at most 50 rules')
        result = []
        for rule in rules:
            if not isinstance(rule, dict) or set(rule) != {'folder', 'instructions'}:
                raise ValueError('Each folder rule requires folder and instructions')
            if not isinstance(rule['folder'], str) or not rule['folder'].strip():
                raise ValueError('Folder rule must have a nonempty folder path')
            folder = cls._paths([rule['folder']], 'folder rule')[0]
            instruction = rule['instructions']
            if not isinstance(instruction, str) or len(instruction) > 4000:
                raise ValueError('Folder rule must have at most 4000 characters')
            result.append({'folder': folder, 'instructions': instruction})
        v['folder_rules'] = result
        return v

    def get(self):
        with self.lock:
            return copy.deepcopy(self.values)

    def save(self, values):
        with self.lock:
            validated = self.validate(values)
            self.organizer.database.save_settings(validated)
            self.values = validated
            self._apply(validated)
            return copy.deepcopy(validated)

    def _apply(self, v):
        classifier = self.organizer.classifier
        classifier.base_url = v['ollama_url']
        classifier.model = v['model']
        classifier.timeout = v['timeout']
        classifier.temperature = v['temperature']
        classifier.max_content_chars = v['max_content_chars']
        classifier.paperless_enabled = v['paperless_enabled']
        classifier.paperless_inbox = v['paperless_inbox']
        classifier.paperless_prefer_send = set(v['paperless_prefer_send'])
        self.organizer.paperless_enabled = v['paperless_enabled']
        self.organizer.paperless_inbox = v['paperless_inbox']
        classifier.global_instructions = v['global_instructions']
        classifier.folder_rules = copy.deepcopy(v['folder_rules'])
        scanner = self.organizer.scanner
        scanner.scan_paths = list(v['scan_paths'])
        scanner.allowed_extensions = extensions_for(v['file_types'])
        scanner.allow_sensitive = 'credential' in v['file_types']
        effective_excludes = list(v['exclude_paths'])
        if v['paperless_enabled'] and v['paperless_inbox'] not in effective_excludes:
            effective_excludes.append(v['paperless_inbox'])
        scanner.exclude_paths = effective_excludes
        nextcloud = self.organizer.nextcloud
        nextcloud.ocr_enabled = v['ocr_enabled']
        nextcloud.ocr_max_pages = v['ocr_max_pages']
        if hasattr(nextcloud, "_ocr_cache"):
            nextcloud._ocr_cache.clear()
        nextcloud.allowed_extensions = extensions_for(v['file_types'])
        nextcloud.allow_sensitive = 'credential' in v['file_types']
        nextcloud.scan_paths = list(v['scan_paths'])
        nextcloud.exclude_paths = list(effective_excludes)
