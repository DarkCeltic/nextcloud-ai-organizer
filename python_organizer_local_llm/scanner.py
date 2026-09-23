#!/usr/bin/env python3

import logging
from pathlib import PurePosixPath
from typing import Dict, Iterator, List, Optional

import yaml

from python_organizer_local_llm.sensitive import sensitive_filename
from python_organizer_local_llm.file_types import BY_EXTENSION



class Scanner:
    """
    Discovers files in Nextcloud that are eligible for AI processing.

    Responsibilities:
      - Walk configured Nextcloud folders recursively.
      - Exclude configured paths.
      - Filter by supported file extensions.
      - Skip files that have already been processed and have not changed.
      - Yield file metadata to python_organizer_local_llm.py.

    The Scanner does NOT:
      - Download file contents.
      - Call Ollama.
      - Rename files.
      - Move files.
      - Delete files.
      - Apply tags.

    Those responsibilities belong to other components.
    """

    def __init__(
            self,
            nextcloud,
            database,
            config_file: Optional[str] = None,
            scan_paths: Optional[List[str]] = None,
            exclude_paths: Optional[List[str]] = None,
            allowed_extensions: Optional[List[str]] = None,
    ):
        self.log = logging.getLogger("scanner")

        self.nextcloud = nextcloud
        self.database = database

        config = {}
        if config_file:
            try:
                with open(config_file, "r", encoding="utf-8") as handle:
                    config = yaml.safe_load(handle) or {}
            except FileNotFoundError:
                self.log.warning("Scanner config file not found: %s", config_file)
            except yaml.YAMLError as exc:
                raise RuntimeError(f"Invalid YAML configuration: {config_file}") from exc

        organizer_cfg = config.get("python_organizer_local_llm", {}) if isinstance(config, dict) else {}
        scanner_cfg = config.get("scanner", {}) if isinstance(config, dict) else {}

        self.scan_paths = scan_paths or scanner_cfg.get(
            "scan_paths", organizer_cfg.get("scan_paths", ["/AI Inbox"])
        )

        self.exclude_paths = exclude_paths or scanner_cfg.get(
            "exclude_paths",
            organizer_cfg.get(
                "exclude_paths",
                ["/paperless-media", "/inbox", "/Photos", "/AI Ignored"],
            ),
        )

        configured_extensions = scanner_cfg.get(
            "allowed_extensions",
            ["pdf", "txt", "docx", "odt", "rtf", "md"],
        )
        self.allowed_extensions = {
            self._normalize_extension(extension)
            for extension in (configured_extensions if allowed_extensions is None else allowed_extensions)
            if self._normalize_extension(extension) in BY_EXTENSION
        }

    def scan(
            self,
            force: bool = False,
            limit: Optional[int] = None,
    ) -> Iterator[Dict]:
        """
        Scan configured paths and yield files that should be processed.

        Parameters
        ----------
        force:
            If True, return files even if their stored ETag matches
            the current Nextcloud ETag.

        limit:
            Maximum number of eligible files to return.

        Yields
        ------
        dict
            Nextcloud file metadata.
        """

        yielded = 0

        self.log.info(
            "Starting scan of %d configured path(s).",
            len(self.scan_paths),
        )

        for scan_path in self.scan_paths:

            scan_path = self._normalize_path(scan_path)

            if self.is_excluded(scan_path):
                self.log.warning(
                    "Configured scan path is excluded: %s",
                    scan_path,
                )
                continue

            self.log.info(
                "Scanning Nextcloud path: %s",
                scan_path,
            )

            try:
                files = self.nextcloud.list_files_recursive(
                    scan_path
                )

            except Exception:
                self.log.exception(
                    "Unable to scan Nextcloud path: %s",
                    scan_path,
                )
                continue

            for remote_file in files:

                if limit is not None and yielded >= limit:
                    self.log.info(
                        "Scanner limit of %d eligible files reached.",
                        limit,
                    )
                    return

                try:
                    if not self.should_process(
                            remote_file,
                            force=force,
                    ):
                        continue

                    yielded += 1

                    yield remote_file

                except Exception:
                    self.log.exception(
                        "Error evaluating file: %s",
                        remote_file.get(
                            "path",
                            "<unknown>",
                        ),
                    )

        self.log.info(
            "Scan completed. %d eligible file(s) found.",
            yielded,
        )

    def should_process(
            self,
            remote_file: Dict,
            force: bool = False,
    ) -> bool:
        """
        Determine whether a Nextcloud file should be processed.
        """

        path = remote_file.get("path")

        if not path:
            self.log.debug(
                "Skipping Nextcloud item with no path."
            )
            return False

        path = self._normalize_path(path)

        #
        # Skip folders.
        #
        if remote_file.get("is_directory", False):
            return False

        if remote_file.get("type") in (
                "directory",
                "dir",
                "folder",
        ):
            return False

        #
        # Never process excluded paths.
        #
        if self.is_excluded(path):
            self.log.debug(
                "Excluded: %s",
                path,
            )
            return False

        #
        # Only process supported document types.
        #
        if not self.is_supported_file(path):
            self.log.debug(
                "Unsupported extension: %s",
                path,
            )
            return False

        #
        # Extraction/OCR failures wait in Failed until explicitly retried.
        file_id = str(remote_file.get('file_id') or remote_file.get('nextcloud_file_id') or '')
        if file_id and self.database.get_open_failure(file_id):
            return False

        # Files explicitly marked as ignored should stay ignored.
        #
        try:
            if self.database.is_ignored(path):
                self.log.debug(
                    "AI processing disabled for: %s",
                    path,
                )
                return False

        except AttributeError:
            # Database implementation may not have ignore
            # support yet.
            pass

        #
        # --force bypasses ETag checking.
        #
        if force:
            self.log.debug(
                "Force processing enabled: %s",
                path,
            )
            return True

        #
        # Check whether this file was previously processed.
        #
        existing = self.database.get_file(path)

        if not existing:
            self.log.debug(
                "New file: %s",
                path,
            )
            return True

        # A database record alone does not mean
        # the file was successfully analyzed.
        if not existing.get("processed_at"):
            self.log.info(
                "File has not been processed: %s",
                path,
            )
            return True

        # Don't repeatedly analyze files that are
        # already waiting for a user decision.
        latest = self.database.get_latest_suggestion(
            existing["id"]
        )

        if latest and latest.get("status") in {
            "pending",
            "accepted",
        }:
            self.log.debug(
                "File is awaiting review: %s",
                path,
            )
            return False

        current_etag = remote_file.get("etag")
        stored_etag = existing.get("etag")

        #
        # If ETags are unavailable, err on the safe side and
        # allow processing.
        #
        if not current_etag or not stored_etag:
            self.log.debug(
                "ETag unavailable; processing: %s",
                path,
            )
            return True

        #
        # Matching ETag means Nextcloud says the file has not
        # changed since we last recorded it.
        #
        if current_etag == stored_etag:
            self.log.debug(
                "Unchanged: %s",
                path,
            )
            return False

        self.log.info(
            "Changed file detected: %s",
            path,
        )

        return True

    def is_supported_file(
            self,
            path: str,
    ) -> bool:
        """
        Check whether a file extension is supported.
        """

        suffix = PurePosixPath(path).suffix.lower()

        return suffix in self.allowed_extensions or (
            getattr(self, "allow_sensitive", True) and sensitive_filename(path)
        )

    def is_excluded(
            self,
            path: str,
    ) -> bool:
        """
        Determine whether path is inside one of the configured
        excluded paths.

        Example:

            /Photos

        also excludes:

            /Photos/2026/image.jpg
        """

        normalized_path = self._normalize_path(path)

        for excluded in self.exclude_paths:

            excluded = self._normalize_path(excluded)

            if normalized_path == excluded:
                return True

            if normalized_path.startswith(
                    excluded.rstrip("/") + "/"
            ):
                return True

        return False

    def get_scan_summary(
            self,
            force: bool = False,
    ) -> Dict[str, int]:
        """
        Scan without performing AI processing and return counts.

        Useful later for the web UI.
        """

        summary = {
            "total": 0,
            "eligible": 0,
            "excluded": 0,
            "unsupported": 0,
            "unchanged": 0,
            "ignored": 0,
        }

        for scan_path in self.scan_paths:

            scan_path = self._normalize_path(scan_path)

            try:
                files = self.nextcloud.list_files_recursive(
                    scan_path
                )

            except Exception:
                self.log.exception(
                    "Unable to scan %s",
                    scan_path,
                )
                continue

            for remote_file in files:

                path = remote_file.get("path")

                if not path:
                    continue

                #
                # Don't count directories as files.
                #
                if remote_file.get(
                        "is_directory",
                        False,
                ):
                    continue

                if remote_file.get("type") in (
                        "directory",
                        "dir",
                        "folder",
                ):
                    continue

                summary["total"] += 1

                if self.is_excluded(path):
                    summary["excluded"] += 1
                    continue

                if not self.is_supported_file(path):
                    summary["unsupported"] += 1
                    continue

                try:
                    if self.database.is_ignored(path):
                        summary["ignored"] += 1
                        continue

                except AttributeError:
                    pass

                if force:
                    summary["eligible"] += 1
                    continue

                existing = self.database.get_file(path)

                if not existing:
                    summary["eligible"] += 1
                    continue

                current_etag = remote_file.get("etag")
                stored_etag = existing.get("etag")

                if (
                        current_etag
                        and stored_etag
                        and current_etag == stored_etag
                ):
                    summary["unchanged"] += 1
                    continue

                summary["eligible"] += 1

        return summary

    @staticmethod
    def _normalize_extension(
            extension: str,
    ) -> str:
        """
        Normalize extension to:

            .pdf
            .docx
            .txt
        """

        extension = extension.strip().lower()

        if not extension.startswith("."):
            extension = "." + extension

        return extension

    @staticmethod
    def _normalize_path(
            path: str,
    ) -> str:
        """
        Normalize Nextcloud paths while preserving POSIX-style
        separators.
        """

        path = str(path).strip()

        if not path:
            return "/"

        if not path.startswith("/"):
            path = "/" + path

        if len(path) > 1:
            path = path.rstrip("/")

        return path
