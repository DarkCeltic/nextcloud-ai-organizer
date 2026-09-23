"""Single-worker periodic automation. All potentially destructive actions use Apply route."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

from exapp.routes.analyze import analyze_file
from exapp.routes.apply import ApplyRequest, apply_suggestion
from exapp.routes.dashboard import list_unprocessed
from python_organizer_local_llm.sensitive import sensitive_filename

log = logging.getLogger('exapp.automation')


class AutomationScheduler:
    def __init__(self, app):
        self.app = app
        self.stop = threading.Event()
        self.enabled = True
        self.worker = None
        self.run_lock = threading.Lock()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.stop.clear()
        self.worker = threading.Thread(target=self._loop, name='AI-Organizer-scheduler', daemon=True)
        self.worker.start()

    def shutdown(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=10)

    @staticmethod
    def _seconds_since(timestamp):
        if not timestamp:
            return float('inf')
        try:
            return (datetime.now(timezone.utc) - datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
                    .replace(tzinfo=timezone.utc)).total_seconds()
        except (ValueError, TypeError):
            return float('inf')

    def _loop(self):
        while not self.stop.wait(15):
            try:
                v = self.app.state.settings.get()
                if not self.enabled or not v['schedule_enabled'] or not (v['auto_analyze'] or v['auto_apply']):
                    continue
                last = self.app.state.organizer.database.get_automation_run()
                reference = last['finished_at'] or last['started_at']
                if self._seconds_since(reference) < v['interval_minutes'] * 60:
                    continue
                self.run_once()
            except Exception:
                log.exception('Automation loop failed; next interval will retry')

    def run_once(self):
        """Run bounded work once; skip overlaps; no automatic Paperless moves."""
        if not self.run_lock.acquire(blocking=False):
            return
        db = self.app.state.organizer.database
        processed = applied = failed = 0
        errors = []
        try:
            # Copy settings once: changes from Save affect the following run.
            v = self.app.state.settings.get()
            if not self.enabled or not v['schedule_enabled']:
                return
            db.start_automation_run()
            request = SimpleNamespace(app=self.app)
            if v['auto_analyze']:
                # First-page polling prevents offset-skips as analyzed files leave the queue.
                # Cap each interval to avoid monopolizing a slow local GPU.
                examined = set()
                while len(examined) < 20 and not self.stop.is_set() and self.enabled:
                    result = list_unprocessed(request=request, limit=20, offset=0, debug=False)
                    batch = [r for r in result['items'] if r['file_id'] not in examined]
                    if not batch:
                        break
                    for item in batch:
                        if len(examined) >= 20 or self.stop.is_set() or not self.enabled:
                            break
                        file_id = str(item['file_id'])
                        examined.add(file_id)
                        try:
                            analyze_file(file_id=file_id, request=request, force=False)
                            db.resolve_analysis_failure(file_id)
                            processed += 1
                        except Exception as exc:
                            failed += 1
                            errors.append(f'Analyze {file_id}: {exc}')
                            # OCR / empty-text failures are persisted inside the
                            # analyze endpoint with their original error stage.
                            if (getattr(exc, 'status_code', None) == 422 and
                                    str(getattr(exc, 'detail', '')).startswith(('OCR:', 'Extraction:'))):
                                continue
                            try:
                                db.record_analysis_failure(file_id, item['path'], str(exc), 'analyze')
                            except ValueError:
                                log.warning('Could not persist analysis failure %s', file_id)
            if v['auto_apply'] and self.enabled and not self.stop.is_set():
                # Never auto-process a partly applied/edited record; leave it in Review.
                # The dedicated Apply route preserves all existing safety checks.
                for item in db.list_review(limit=100, offset=0):
                    if self.stop.is_set() or not self.enabled:
                        break
                    if item['status'] != 'pending' or bool(item['paperless_candidate']) or bool(item.get('manual_review_only')) or sensitive_filename(item.get('current_path') or '') or item.get('category') == 'credential':
                        continue
                    if float(item['confidence'] or 0) < 0.95:
                        continue
                    destination = str(item.get('suggested_folder') or '')
                    scanner = self.app.state.organizer.scanner
                    if scanner.is_excluded(destination):
                        log.warning('Skipping auto-apply to excluded folder: %s', destination)
                        continue
                    file_id = str(item.get('nextcloud_file_id') or '')
                    if not file_id.isdecimal():
                        continue
                    try:
                        apply_suggestion(file_id=file_id,
                                         body=ApplyRequest(suggestion_id=item['id'],
                                             actions=['folder', 'filename', 'tags'], destination='nextcloud'),
                                         request=request)
                        applied += 1
                    except Exception as exc:
                        failed += 1
                        errors.append(f'Apply {file_id}: {exc}')
                        # Leave partial progress in Review; never label as deleted.
                        log.exception('Automatic Apply failed for file %s', file_id)
        except Exception as exc:
            failed += 1
            errors.append(f'Scan: {exc}')
            log.exception('Scheduled scan failed (no files archived on scan errors)')
        finally:
            try:
                db.finish_automation_run(processed, applied, failed, '; '.join(errors)[:2000])
            except Exception:
                log.exception('Unable to persist automation run outcome')
            self.run_lock.release()
