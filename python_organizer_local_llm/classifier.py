#!/usr/bin/env python3

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import re
from pathlib import PurePosixPath
from typing import Dict, List, Optional

import requests
import yaml

from python_organizer_local_llm.sensitive import is_sensitive, safe_suggestion
from python_organizer_local_llm.suggestion_policy import improve_suggestion


class Classifier:
    """
    Sends document content to Ollama and returns structured
    organization suggestions.

    This class ONLY generates suggestions.

    It does NOT:
      - Rename files
      - Move files
      - Delete files
      - Apply tags
      - Send files to Paperless
    """

    def __init__(self, config_file: str = "config.yaml"):
        self.log = logging.getLogger("classifier")
        self.config = self._load_config(config_file)
        self.global_instructions = ""
        self.folder_rules = []
        # Request-local limit: one bounded inference for Paperless overrides.
        self._quick_mode = ContextVar("organizer_quick_mode", default=False)

        ollama_config = self.config.get("ollama", {})

        self.base_url = ollama_config.get(
            "url",
            "http://localhost:11434",
        ).rstrip("/")

        self.model = ollama_config.get(
            "model",
            "qwen2.5:7b",
        )

        self.timeout = int(
            ollama_config.get(
                "timeout",
                180,
            )
        )

        self.temperature = float(
            ollama_config.get(
                "temperature",
                0.1,
            )
        )

        classifier_config = self.config.get(
            "classifier",
            {},
        )

        self.max_content_chars = int(
            classifier_config.get(
                "max_content_chars",
                8000,
            )
        )

        self.max_folder_entries = int(
            classifier_config.get(
                "max_folder_entries",
                500,
            )
        )

        self.max_tag_entries = int(
            classifier_config.get(
                "max_tag_entries",
                200,
            )
        )

        self.minimum_confidence = float(
            classifier_config.get(
                "minimum_confidence",
                0.0,
            )
        )

        paperless_config = self.config.get(
            "paperless",
            {},
        )

        self.paperless_enabled = bool(
            paperless_config.get(
                "enabled",
                True,
            )
        )

        self.paperless_inbox = paperless_config.get(
            "inbox_path",
            "/inbox",
        )

        never_send = paperless_config.get(
            "never_send",
            [],
        )

        if isinstance(never_send, str):
            never_send = [never_send]

        self.paperless_never_send = {
            self._normalize_policy_value(value)
            for value in never_send
            if str(value).strip()
        }
        prefer_send = paperless_config.get('prefer_send') or []
        if isinstance(prefer_send, str):
            prefer_send = [prefer_send]
        self.paperless_prefer_send = {
            self._normalize_policy_value(value)
            for value in prefer_send if str(value).strip()
        }

    @contextmanager
    def quick_mode(self):
        token = self._quick_mode.set(True)
        try:
            yield
        finally:
            self._quick_mode.reset(token)

    def classify(
            self,
            filename: str,
            file_path: str,
            content: str,
            existing_folders: Optional[List[str]] = None,
            existing_tags: Optional[List[str]] = None,
            force_nextcloud: bool = False,
    ) -> Dict:
        """
        Analyze a document and return organization suggestions.

        Expected result:

        {
            "suggested_filename": "...",
            "suggested_folder": "...",
            "tags": [],
            "category": "...",
            "paperless_candidate": false,
            "confidence": 0.0,
            "reason": "..."
        }
        """
        # Credential contents must never reach Ollama, prompt logging or raw-response logging.
        if is_sensitive(file_path, content):
            return safe_suggestion(file_path)
        existing_folders = existing_folders or []
        existing_tags = existing_tags or []

        content = self._prepare_content(content)

        self.log.debug(
            "Prepared %d characters of document text for Ollama: %s",
            len(content),
            file_path,
        )

        prompt = self._build_prompt(
            filename=filename,
            file_path=file_path,
            content=content,
            existing_folders=existing_folders,
            existing_tags=existing_tags,
            force_nextcloud=force_nextcloud,
        )

        self.log.info(
            "Classifying with Ollama model %s: %s",
            self.model,
            file_path,
        )

        response_text = self._query_ollama(prompt)
        suggestion = self._parse_response(response_text)

        missing = self._missing_required_model_fields(suggestion)
        if missing and self._quick_mode.get():
            # Do not silently launch another lengthy inference after a user
            # explicitly rejects Paperless. Return a visible, retriable error.
            raise ValueError('Ollama returned an incomplete Nextcloud suggestion: '
                             + ', '.join(missing) + '. Try again or lower the model timeout in Settings.')
        if missing:
            self.log.warning(
                "Ollama response missing required field(s) %s for %s; retrying once.",
                ", ".join(missing),
                file_path,
            )
            retry_prompt = (
                    prompt
                    + "\n\nYour previous response was incomplete. "
                    + "Return ALL required JSON fields exactly as specified. "
                    + "Do not omit fields and do not use alternate key names."
            )
            response_text = self._query_ollama(retry_prompt)
            suggestion = self._parse_response(response_text)
            missing = self._missing_required_model_fields(suggestion)
            if missing:
                raise ValueError(
                    "Ollama response is missing required field(s): "
                    + ", ".join(missing)
                    + ". Parsed keys: "
                    + ", ".join(sorted(suggestion.keys()))
                )

        suggestion = self._normalize_suggestion(
            suggestion=suggestion,
            original_filename=filename,
            original_path=file_path,
        )

        # A one-file Nextcloud choice must never be silently routed back to Paperless.
        if force_nextcloud:
            suggestion['paperless_candidate'] = False
        suggestion = self._apply_paperless_policy(
            suggestion=suggestion,
            filename=filename,
            content=content,
        )
        if force_nextcloud:
            suggestion['paperless_candidate'] = False
            blocked = (self.paperless_inbox.rstrip('/').casefold(), '/paperless-media')
            proposed = suggestion['suggested_folder'].rstrip('/').casefold()
            if any(proposed == root or proposed.startswith(root + '/') for root in blocked if root):
                suggestion['suggested_folder'] = '/Documents/Unsorted'

        # Never issue a second whole-document request merely because tags were empty.
        # It can double Ollama latency or appear to hang when rejecting Paperless.
        # This policy reuses an exact existing tag match first, then a supported
        # category label, and protects against obviously unrelated folder trees.
        suggestion = improve_suggestion(
            suggestion, file_path=file_path, existing_folders=existing_folders,
            existing_tags=existing_tags, paperless_inbox=self.paperless_inbox,
        )

        self._validate_suggestion(suggestion)

        return suggestion

    def _query_ollama(self, prompt: str) -> str:
        """Send classification request to Ollama."""
        url = f"{self.base_url}/api/chat"

        response_schema = {
            "type": "object",
            "properties": {
                "suggested_filename": {"type": "string"},
                "suggested_folder": {"type": "string"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "category": {"type": "string"},
                "paperless_candidate": {"type": "boolean"},
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "reason": {"type": "string"},
            },
            "required": [
                "suggested_filename",
                "suggested_folder",
                "tags",
                "category",
                "paperless_candidate",
                "confidence",
                "reason",
            ],
            "additionalProperties": False,
        }

        payload = {
            "model": self.model,
            "stream": False,
            "format": response_schema,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a document organization classifier. "
                        "Return valid JSON only. "
                        "Do not perform actions. "
                        "Do not claim that files were moved, renamed, "
                        "tagged, or deleted."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            "options": {
                "temperature": self.temperature,
            },
        }

        # A Paperless override has a strict per-inference budget. Normal analysis
        # continues to respect the administrator-configured timeout.
        quick = self._quick_mode.get()
        budget = min(self.timeout, 45) if quick else self.timeout
        deadline = time.monotonic() + budget
        try:
            response = requests.post(
                url, json=payload, timeout=(min(5, budget), budget),
            )

            # Older Ollama builds may not accept a JSON Schema object in
            # `format`. Fall back to plain JSON mode rather than failing the
            # entire classification request.
            if response.status_code == 400:
                self.log.warning(
                    "Ollama rejected structured-output schema; "
                    "retrying with format=json for compatibility."
                )
                fallback_payload = dict(payload)
                fallback_payload["format"] = "json"
                remaining = deadline - time.monotonic()
                if remaining <= 1:
                    raise requests.Timeout('No time remains for JSON-mode fallback')
                response = requests.post(
                    url, json=fallback_payload,
                    timeout=(min(5, remaining), remaining),
                )

            response.raise_for_status()

        except requests.Timeout as exc:
            raise RuntimeError(
                f"Ollama request timed out after "
                f"{budget} seconds per inference."
            ) from exc

        except requests.RequestException as exc:
            raise RuntimeError(
                f"Unable to communicate with Ollama at "
                f"{self.base_url}: {exc}"
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                "Ollama returned a non-JSON HTTP response."
            ) from exc

        message = data.get("message", {})
        response_text = message.get("content")

        if not response_text:
            raise RuntimeError(
                "Ollama returned an empty classification response."
            )

        return response_text

    def _build_prompt(
            self,
            filename: str,
            file_path: str,
            content: str,
            existing_folders: List[str],
            existing_tags: List[str],
            force_nextcloud: bool = False,
    ) -> str:
        """Build a constrained classification prompt."""
        folders = self._prepare_folder_list(existing_folders)
        tags = self._prepare_tag_list(existing_tags)

        paperless_rules = ""

        if self.paperless_enabled and not force_nextcloud:
            never_send_text = self._format_never_send_rules()
            prefer_send_text = self._format_prefer_send_rules()
            paperless_rules = f"""
PAPERLESS-NGX:

Set "paperless_candidate" to true when the file appears to be
a document that is better managed by Paperless-NGX.

Typical Paperless candidates include:

* Bills
* Statements
* Receipts
* Invoices
* Tax documents
* Insurance documents
* Contracts
* Official letters
* Financial records
* Medical paperwork
* Warranty documents
* Government documents

Files that normally should remain in Nextcloud include:

* Resumes
* CVs / curriculum vitae
* Cover letters
* Portfolios
* Project files
* Source code
* Configuration files
* Notes
* Manuals used as project/reference files
* Creative work
* General working documents
* Files that belong to an existing project folder

Configured NEVER-PAPERLESS document types:
{never_send_text}

If the document category matches any configured NEVER-PAPERLESS type,
"paperless_candidate" MUST be false.

Configured PREFER-PAPERLESS document categories (advisory, only when
supported by DOCUMENT CONTENT):
{prefer_send_text}

If the determined category matches a PREFER-PAPERLESS type, recommend Paperless,
unless it matches a NEVER-PAPERLESS rule. NEVER-PAPERLESS always wins.
Do not change or invent a category merely to match this preference.

Even when paperless_candidate is true, ALWAYS populate suggested_filename,
suggested_folder, category and 1-5 evidence-supported tags for the ALTERNATIVE
of keeping the file in Nextcloud. The suggested_folder must be a normal
Nextcloud folder, never the Paperless consume folder {self.paperless_inbox}.
Use reason to explain why Paperless is recommended, and briefly justify the
alternative folder. The actual Paperless destination is configured separately
by the application; do not put it in suggested_folder.

Do not automatically decide that every PDF belongs in Paperless.
""".strip()

        if force_nextcloud:
            paperless_rules = (
                'ONE-FILE USER OVERRIDE: Keep this file in Nextcloud. '
                'paperless_candidate MUST be false. Do not choose the Paperless inbox '
                'or Paperless media as the suggested_folder. Produce a meaningful '
                'content-supported filename, Nextcloud folder, category and tags.'
            )

        custom_rules = self.global_instructions.strip()
        matched = sorted(
            (rule for rule in self.folder_rules if rule.get('instructions') and (
                rule['folder'] == '/' or file_path == rule['folder']
                or file_path.startswith(rule['folder'].rstrip('/') + '/'))),
            key=lambda rule: len(rule['folder']), reverse=True
        )
        folder_instructions = '\n'.join(
            f"For files within {rule['folder']}: {rule['instructions']}" for rule in matched
        )
        custom_section = (
            '\nUSER ORGANIZATION PREFERENCES (advisory; never override safety or Paperless restrictions):\n'
            + custom_rules + '\n' + folder_instructions + '\n'
        ) if (custom_rules or folder_instructions) else ''

        return f"""
Analyze the following Nextcloud file and suggest how it should
be organized.

You are generating suggestions only.

DO NOT:

* Move the file
* Rename the file
* Delete the file
* Apply tags
* Claim that any action has already happened

Return EXACTLY one JSON object.

Required JSON structure:

{{
  "suggested_filename": "string",
  "suggested_folder": "string",
  "tags": ["string"],
  "category": "string",
  "paperless_candidate": false,
  "confidence": 0.0,
  "reason": "short explanation"
}}

RULES:

1. confidence must be a number between 0.0 and 1.0.

2. suggested_filename must include the original file extension.

3. Do not invent dates, company names, people, account numbers,
   subjects, or other facts not supported by the document.

4. Prefer an EXISTING folder when one reasonably matches.

5. Only propose a new folder when none of the existing folders
   are appropriate.

6. Prefer EXISTING tags when appropriate.

7. You may suggest a new tag when existing tags do not adequately
   describe the document.

8. Keep tags short and reusable. For EVERY file (including Paperless candidates)
   suggest Nextcloud alternative tags: 1-5 specific,
   evidence-supported tags when useful. Prefer exact existing tags if they
   represent the document. If none match, propose new descriptive tags;
   use [] only when the document has no supported category or topic.

9. Do not create many nearly identical tags.

10. Do not use the original filename as evidence when it appears
    to be meaningless, such as:
    scan001.pdf
    IMG_1234.pdf
    document.pdf
    file1.pdf

11. Determine the document type from DOCUMENT CONTENT before deciding
    that it is unknown or unsorted. A generic filename is not a reason by
    itself to classify a readable document as unsorted.

12. If the document identity is uncertain, preserve the original filename
    rather than inventing a specific title.

13. For suggested_folder, first choose the best semantic match from the
    EXISTING NEXTCLOUD FOLDERS. If none matches, create a concise, meaningful
    folder path based on the document content. Use /Documents/Unsorted only
    when the readable content genuinely does not reveal what the file is.

14. Treat an existing folder as a FULL semantic path, not as a collection
    of unrelated folder-name keywords. A matching final path component is not
    enough. For example, an HR process document does NOT belong in an
    application/configuration path merely because that path ends in /process.
    Only choose an existing folder when the meaning of the complete path fits
    the document. Never route a resume into Google Takeout, an app backup,
    or a settings folder because they share one word. A resume belongs in
    a dedicated Resumes folder, such as /Documents/Resumes. Never select
    /AI Inbox as a destination; it is an incoming queue.

15. category must be descriptive and specific. Prefer values such as
    receipt, invoice, statement, tax, insurance, legal, resume, manual,
    project, notes, correspondence, or another content-supported category.
    Do not use generic values such as "document" when a more specific
    category is supported by the text.

16. Do not place photographs or screenshots into Paperless.

17. The reason must be concise and identify the evidence used. One or two
    sentences maximum.

{paperless_rules}

{custom_section}

CURRENT FILE:

Filename:
{filename}

Current path:
{file_path}

EXISTING NEXTCLOUD FOLDERS:

{folders}

EXISTING NEXTCLOUD TAGS:

{tags}

DOCUMENT CONTENT:

--- BEGIN DOCUMENT ---

{content}

--- END DOCUMENT ---

Return JSON only.
""".strip()

    @staticmethod
    def _normalize_policy_value(value: object) -> str:
        """Normalize a category/policy value for reliable matching."""
        value = str(value or "").strip().lower()
        value = re.sub(r"[^a-z0-9]+", "_", value)
        return value.strip("_")

    def _format_never_send_rules(self) -> str:
        """Render configured never-send values for the LLM prompt."""
        if not self.paperless_never_send:
            return "(none configured)"

        return "\n".join(
            f"* {value.replace('_', ' ')}"
            for value in sorted(self.paperless_never_send)
        )

    def _format_prefer_send_rules(self) -> str:
        """Expose preferred document categories to the LLM without forcing a category."""
        if not self.paperless_prefer_send:
            return '(none configured)'
        return '\n'.join(
            f'* {value.replace("_", " ")}' for value in sorted(self.paperless_prefer_send)
        )

    def _apply_paperless_policy(
            self,
            suggestion: Dict,
            filename: str,
            content: str,
    ) -> Dict:
        """Apply deterministic Paperless routing rules after LLM output.

        The LLM identifies the document. Configuration decides whether a
        known document type is ever allowed to route to Paperless.
        """
        if not self.paperless_enabled:
            suggestion["paperless_candidate"] = False
            return suggestion

        category = self._normalize_policy_value(
            suggestion.get("category", "")
        )

        aliases = {
            "curriculum_vitae": "cv",
            "resume_cv": "resume",
            "cv_resume": "resume",
            "covering_letter": "cover_letter",
        }
        policy_category = aliases.get(category, category)

        if self.is_never_paperless(
                suggestion.get("category", ""),
                filename,
        ):
            suggestion["paperless_candidate"] = False

            folder = str(
                suggestion.get("suggested_folder", "")
            ).rstrip("/").casefold()

            inbox = self.paperless_inbox.rstrip("/").casefold()

            # Correct any destination left behind by an incorrect
            # Paperless recommendation.
            if (
                    folder == inbox
                    or folder.startswith(inbox + "/")
                    or folder == "/paperless-media"
                    or folder.startswith("/paperless-media/")
            ):
                suggestion["suggested_folder"] = "/Documents/Unsorted"

            return suggestion

        # A preference only acts on the classifier's independently determined
        # category. It never overrides never_send or forces actions in Nextcloud.
        if policy_category in self.paperless_prefer_send:
            if not suggestion.get('paperless_candidate'):
                reason = str(suggestion.get('reason') or '').strip()
                preference = (
                    'Paperless is preferred by the configured category rule ('
                    + policy_category.replace('_', ' ') + '); you can still keep it in Nextcloud.'
                )
                suggestion['reason'] = (reason + ' ' + preference).strip()
            suggestion['paperless_candidate'] = True
        # Preserve the Nextcloud alternative regardless of the Paperless choice.
        return suggestion

    def _prepare_content(self, content: str) -> str:
        """Clean and limit document text before sending it to Ollama."""
        if not content:
            return ""

        content = str(content)
        content = content.replace("\x00", "")
        content = re.sub(r"\r\n?", "\n", content)
        content = re.sub(r"[ \t]+", " ", content)
        content = re.sub(r"\n{4,}", "\n\n\n", content)
        content = content.strip()

        if len(content) <= self.max_content_chars:
            return content

        head_chars = int(self.max_content_chars * 0.75)
        tail_chars = self.max_content_chars - head_chars

        head = content[:head_chars]
        tail = content[-tail_chars:]

        return (
                head
                + "\n\n"
                + "[... document content truncated ...]"
                + "\n\n"
                + tail
        )

    def _prepare_folder_list(self, folders: List[str]) -> str:
        """Format existing folder paths for the prompt."""
        cleaned = []

        for folder in folders:
            if isinstance(folder, dict):
                folder = folder.get("path") or folder.get("name")

            if not folder:
                continue

            folder = str(folder).strip()

            if folder not in cleaned:
                cleaned.append(folder)

        cleaned = sorted(cleaned, key=str.lower)

        if len(cleaned) > self.max_folder_entries:
            cleaned = cleaned[:self.max_folder_entries]
            cleaned.append("[additional folders omitted]")

        if not cleaned:
            return "(No existing folder list available)"

        return "\n".join(f"- {folder}" for folder in cleaned)

    def _prepare_tag_list(self, tags: List[str]) -> str:
        """Format existing Nextcloud tags for the prompt."""
        cleaned = []

        for tag in tags:
            if isinstance(tag, dict):
                tag = tag.get("name") or tag.get("display_name")

            if not tag:
                continue

            tag = str(tag).strip()

            if tag not in cleaned:
                cleaned.append(tag)

        cleaned = sorted(cleaned, key=str.lower)

        if len(cleaned) > self.max_tag_entries:
            cleaned = cleaned[:self.max_tag_entries]
            cleaned.append("[additional tags omitted]")

        if not cleaned:
            return "(No existing tags available)"

        return "\n".join(f"- {tag}" for tag in cleaned)

    def _parse_response(self, response_text: str) -> Dict:
        """
        Parse JSON returned by Ollama.

        Ollama's format=json normally produces clean JSON, but this also
        handles markdown fences or accidental surrounding text.
        """
        response_text = response_text.strip()

        try:
            parsed = json.loads(response_text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        fenced_match = re.search(
            r"```(?:json)?\s*(\{.*?\})\s*```",
            response_text,
            re.DOTALL | re.IGNORECASE,
        )

        if fenced_match:
            try:
                parsed = json.loads(fenced_match.group(1))
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        start = response_text.find("{")
        end = response_text.rfind("}")

        if start != -1 and end != -1 and end > start:
            candidate = response_text[start:end + 1]

            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "Ollama returned malformed JSON:\n"
                    f"{response_text}"
                ) from exc

        raise ValueError(
            "Unable to find a valid JSON object in the Ollama response."
        )

    @staticmethod
    def _missing_required_model_fields(suggestion: Dict) -> List[str]:
        """Return required keys omitted by the model before normalization.

        This is intentionally checked before fallback/default values are
        applied so malformed model output cannot silently turn into an
        Unsorted / zero-confidence suggestion.
        """
        required = {
            "suggested_filename",
            "suggested_folder",
            "tags",
            "category",
            "paperless_candidate",
            "confidence",
            "reason",
        }
        return sorted(required.difference(suggestion.keys()))

    def _normalize_suggestion(
            self,
            suggestion: Dict,
            original_filename: str,
            original_path: str,
    ) -> Dict:
        """
        Normalize model output into the exact structure expected
        by python_organizer_local_llm.py.
        """
        original_extension = PurePosixPath(original_filename).suffix

        suggested_filename = str(
            suggestion.get("suggested_filename", "")
        ).strip()

        if not suggested_filename:
            suggested_filename = original_filename

        suggested_extension = PurePosixPath(
            suggested_filename
        ).suffix

        if original_extension:
            if not suggested_extension:
                suggested_filename += original_extension
            elif suggested_extension.lower() != original_extension.lower():
                suggested_filename = (
                        str(
                            PurePosixPath(
                                suggested_filename
                            ).with_suffix("")
                        )
                        + original_extension
                )

        suggested_folder = str(
            suggestion.get(
                "suggested_folder",
                "/Documents/Unsorted",
            )
        ).strip()

        if not suggested_folder:
            suggested_folder = "/Documents/Unsorted"

        if not suggested_folder.startswith("/"):
            suggested_folder = "/" + suggested_folder

        tags = suggestion.get(
            "tags",
            suggestion.get("suggested_tags", []),
        )

        if isinstance(tags, str):
            tags = [
                item.strip()
                for item in tags.split(",")
                if item.strip()
            ]

        if not isinstance(tags, list):
            tags = []

        normalized_tags = []

        for tag in tags:
            tag = str(tag).strip()
            if (
                    tag
                    and tag.lower()
                    not in {item.lower() for item in normalized_tags}
            ):
                normalized_tags.append(tag)

        category = str(
            suggestion.get("category", "unknown")
        ).strip()

        if not category:
            category = "unknown"

        paperless_candidate = suggestion.get(
            "paperless_candidate",
            False,
        )

        if isinstance(paperless_candidate, str):
            paperless_candidate = (
                    paperless_candidate.strip().lower()
                    in ("true", "yes", "1")
            )
        else:
            paperless_candidate = bool(paperless_candidate)

        try:
            confidence = float(
                suggestion.get("confidence", 0.0)
            )
        except (TypeError, ValueError):
            confidence = 0.0

        confidence = max(0.0, min(confidence, 1.0))

        reason = str(
            suggestion.get("reason", "")
        ).strip()

        return {
            "suggested_filename": suggested_filename,
            "suggested_folder": suggested_folder,
            "tags": normalized_tags,
            "category": category,
            "paperless_candidate": paperless_candidate,
            "confidence": confidence,
            "reason": reason,
        }

    def _validate_suggestion(self, suggestion: Dict) -> None:
        """Final sanity checks."""
        required_fields = [
            "suggested_filename",
            "suggested_folder",
            "tags",
            "category",
            "paperless_candidate",
            "confidence",
            "reason",
        ]

        for field in required_fields:
            if field not in suggestion:
                raise ValueError(
                    "Classifier result missing "
                    f"required field: {field}"
                )

        filename = suggestion["suggested_filename"]

        if not filename:
            raise ValueError(
                "Suggested filename cannot be empty."
            )

        invalid_filename_chars = ["/", "\\", "\x00"]

        for character in invalid_filename_chars:
            if character in filename:
                raise ValueError(
                    "Suggested filename contains "
                    "an invalid character: "
                    f"{character!r}"
                )

        folder = suggestion["suggested_folder"]

        if not folder.startswith("/"):
            raise ValueError(
                "Suggested folder must begin with '/'."
            )

        if not isinstance(suggestion["tags"], list):
            raise ValueError(
                "Suggested tags must be a list."
            )

        if not isinstance(
                suggestion["paperless_candidate"],
                bool,
        ):
            raise ValueError(
                "paperless_candidate must be true or false."
            )

        confidence = suggestion["confidence"]

        if confidence < 0.0 or confidence > 1.0:
            raise ValueError(
                "Confidence must be between 0.0 and 1.0."
            )

        if confidence < self.minimum_confidence:
            self.log.info(
                "Low-confidence suggestion: %.2f",
                confidence,
            )

    @staticmethod
    def _load_config(config_file: str) -> Dict:
        """Load YAML configuration."""
        try:
            with open(
                    config_file,
                    "r",
                    encoding="utf-8",
            ) as file:
                config = yaml.safe_load(file)

        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Configuration file not found: {config_file}"
            ) from exc

        except yaml.YAMLError as exc:
            raise RuntimeError(
                f"Invalid YAML configuration: {config_file}"
            ) from exc

        if config is None:
            config = {}

        if not isinstance(config, dict):
            raise RuntimeError(
                "Configuration root must be a YAML mapping."
            )

        return config

    def is_never_paperless(self, category: str, filename: str) -> bool:
        category = self._normalize_policy_value(category)

        aliases = {
            "curriculum_vitae": "cv",
            "resume_cv": "resume",
            "cv_resume": "resume",
            "covering_letter": "cover_letter",
        }

        category = aliases.get(category, category)

        # Respect configured exclusions.
        if category in self.paperless_never_send:
            return True

        # Explicit safeguard for resumes and CVs.
        filename_stem = self._normalize_policy_value(
            PurePosixPath(filename).stem
        )

        resume_pattern = (
            r"(?:^|_)(?:resume|cv|curriculum_vitae)(?:_|$)"
        )

        return bool(
            re.search(resume_pattern, category)
            or re.search(resume_pattern, filename_stem)
        )


if __name__ == "__main__":
    print(
        "classifier.py is a library module "
        "and is normally called by python_organizer_local_llm.py."
    )
