/** Nextcloud AI Organizer: sidebar, independently scrolling file list and details. */
(() => {
    'use strict';

    const API_BASE = '/apps/app_api/proxy/ai_nextcloud_organizer/';
    const VIEWS = ['unprocessed', 'review', 'failed', 'history', 'settings'];
    const APPLY_ACTIONS = ['filename', 'folder', 'tags'];
    const HISTORY_STATUSES = ['all', 'applied', 'rejected', 'ignored', 'deleted', 'superseded'];
    const PAGE_SIZE = 50;

    const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[char]);

    const formatDate = (value) => {
        if (!value) return 'Unknown';
        const text = String(value).trim();
        const iso = text.replace(' ', 'T');
        const date = new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(iso) ? iso : `${iso}Z`);
        return Number.isNaN(date.getTime()) ? text : date.toLocaleString();
    };

    async function api(path, options = {}) {
        const response = await fetch(`${API_BASE}${path.replace(/^\/+/, '')}`, {
            credentials: 'same-origin',
            ...options,
            headers: {
                'Content-Type': 'application/json',
                ...(options.headers || {})
            }
        });
        let payload = {};
        try {
            payload = await response.json();
        } catch (_) {
            // Preserve the HTTP status when a proxy returns an empty/non-JSON error.
        }
        if (!response.ok) {
            const error = new Error(payload.detail || payload.message || `Request failed (${response.status})`);
            error.status = response.status;
            throw error;
        }
        return payload;
    }

    function initialize() {
        const root = document.querySelector('#content.app-app_api');
        if (!root) {
            console.error('AI Organizer: Nextcloud content element not found');
            return;
        }

        // The server may provide this shell; otherwise create exactly one.
        let shell = root.querySelector('#ai_organize');
        if (!shell || !['status', 'suggestion', 'reanalyze'].every((id) => shell.querySelector(`#${id}`))) {
            if (shell) shell.remove();
            shell = document.createElement('main');
            shell.id = 'ai_organize';
            shell.className = 'app-shell';
            shell.innerHTML = `
                <header class="app-header">
                    <div>
                        <h1>AI Organizer</h1>
                        <p>Review AI suggestions before changing anything in Nextcloud.</p>
                    </div>
                    <button id="reanalyze" type="button" class="secondary" disabled>Re-analyze</button>
                </header>
                <section id="status" class="status-card" aria-live="polite">Loading AI Organizer…</section>
                <section id="suggestion" class="suggestion-card hidden" aria-live="polite"></section>`;
            root.appendChild(shell);
        }
        if (shell.parentElement !== root) root.appendChild(shell);

        // Size the workspace to the space below Nextcloud's actual header.
        // The lists and details can then scroll without moving the whole page.
        function syncViewportHeight() {
            const top = Math.max(0, root.getBoundingClientRect().top);
            root.style.setProperty('--organizer-viewport-height',
                `${Math.max(320, window.innerHeight - top)}px`);
        }
        syncViewportHeight();
        window.addEventListener('resize', syncViewportHeight);

        const headerEl = shell.querySelector('.app-header');
        const statusEl = shell.querySelector('#status');
        const suggestionEl = shell.querySelector('#suggestion');
        const reanalyzeButton = shell.querySelector('#reanalyze');

        // Construct the only dashboard layout; move—not clone—the live editor elements.
        shell.querySelectorAll('.organizer-layout, .organizer-dashboard-layout').forEach((old) => old.remove());
        const layout = document.createElement('div');
        layout.className = 'organizer-layout';
        layout.innerHTML = `
            <nav class="organizer-sidebar" aria-label="AI Organizer workspace">
                <div class="organizer-sidebar-label">WORKSPACE</div>
                <button type="button" data-view="unprocessed" aria-current="page">
                    <span>Unprocessed</span><span id="unprocessed-count" class="organizer-nav-count">…</span>
                </button>
                <button type="button" data-view="review" aria-current="false">
                    <span>Review</span><span id="review-count" class="organizer-nav-count">…</span>
                </button>
                <button type="button" data-view="history" aria-current="false">
                    <span>History</span><span id="history-count" class="organizer-nav-count">…</span>
                </button>
                <button type="button" data-view="failed" aria-current="false">
                    <span>Failed</span><span id="failed-count" class="organizer-nav-count">…</span>
                </button>
                <button type="button" data-view="settings" aria-current="false" hidden>
                    <span>Settings</span><span aria-hidden="true">⚙</span>
                </button>
            </nav>
            <div class="organizer-main">
                <section class="organizer-list-panel" aria-label="File selection">
                    <div id="organizer-file-list" class="organizer-file-list"
                         aria-label="Files for selected workspace view"></div>
                    <div class="organizer-pager">
                        <button type="button" id="organizer-load-more" hidden>Load more</button>
                        <button type="button" id="history-previous" hidden>Previous</button>
                        <span id="history-page-label" hidden></span>
                        <button type="button" id="history-next" hidden>Next</button>
                    </div>
                </section>
                <section class="organizer-editor" aria-label="Selected file details">
                    <div class="organizer-editor-header"><h2 id="organizer-editor-title">Selected file</h2></div>
                </section>
            </div>`;
        const settingsPanel = document.createElement('section');
        settingsPanel.className = 'organizer-settings hidden';
        settingsPanel.setAttribute('aria-label', 'Administrator settings');
        layout.appendChild(settingsPanel);
        const sidebar = layout.querySelector('.organizer-sidebar');
        const mainPanel = layout.querySelector('.organizer-main');
        const fileList = layout.querySelector('#organizer-file-list');
        const editor = layout.querySelector('.organizer-editor');
        const editorHeader = layout.querySelector('.organizer-editor-header');
        const editorTitle = layout.querySelector('#organizer-editor-title');
        const moreButton = layout.querySelector('#organizer-load-more');
        const previousButton = layout.querySelector('#history-previous');
        const nextButton = layout.querySelector('#history-next');
        const pageLabel = layout.querySelector('#history-page-label');

        // Native Nextcloud viewer: file contents remain governed by the signed-in
        // user's permissions. Never proxy raw file bytes through the ExApp.
        const previewLink = document.createElement('a');
        previewLink.className = 'organizer-preview secondary';
        previewLink.textContent = 'Preview in Nextcloud ↗';
        previewLink.target = '_blank';
        previewLink.rel = 'noopener noreferrer';
        previewLink.hidden = true;
        const editorActions = document.createElement('div');
        editorActions.className = 'organizer-editor-actions';
        editorActions.append(previewLink, reanalyzeButton);
        function previewHref(fileId) {
            const id = String(fileId || '');
            if (!/^\d+$/.test(id)) return null;
            const prefix = window.location.pathname.match(
                /^(.*?)\/(?:index\.php\/)?(?:apps|f)(?:\/|$)/
            )?.[1] || '';
            return `${window.location.origin}${prefix}/index.php/f/${id}`;
        }
        function showPreview(fileId) {
            const href = previewHref(fileId);
            previewLink.hidden = !href;
            if (href) previewLink.href = href;
            else previewLink.removeAttribute('href');
        }

        editorHeader.appendChild(editorActions);
        editor.append(statusEl, suggestionEl);
        if (headerEl && headerEl.parentElement !== shell) shell.prepend(headerEl);
        shell.appendChild(layout);

        const state = {
            view: 'unprocessed', items: [], count: 0, offset: 0, request: 0,
            busy: false, historyFilter: 'all', currentFileId: null,
            currentSuggestion: null, currentFile: null, historyRecord: null
        };

        const settingTabs = ['llm', 'folders', 'file-types', 'paperless', 'scheduling', 'rules', 'ocr'];
        let fileTypeCatalog = [];
        let settingsAvailable = false;
        let settingsBusy = false;

        function settingsRows(container, rules) {
            container.innerHTML = rules.map((rule, i) => `
                <div class="settings-rule">
                    <label>Folder path <input data-rule-folder value="${escapeHtml(rule.folder)}"
                        list="settings-folder-options" placeholder="/Projects"></label>
                    <label>Instructions <textarea data-rule-text rows="3"
                        placeholder="Rules for this folder and its subfolders">${escapeHtml(rule.instructions)}</textarea></label>
                    <button type="button" class="secondary" data-remove-rule="${i}">Remove</button>
                </div>`).join('');
        }

        function renderSettings(settings, automation) {
            const activeTab = settingsPanel.querySelector('[data-settings-tab][aria-current="page"]')?.dataset.settingsTab || 'llm';
            const checked = (value) => value ? 'checked' : '';
            settingsPanel.innerHTML = `
                <header class="settings-heading">
                    <div><h2>Settings</h2><p>Administrator configuration — saved to SQLite.</p></div>
                    <button type="button" class="primary" id="settings-save">Save changes</button>
                </header>
                <nav class="settings-tabs" aria-label="Settings categories">
                    <button type="button" data-settings-tab="llm" aria-current="page">Local LLM</button>
                    <button type="button" data-settings-tab="folders" aria-current="false">Folders</button>
                    <button type="button" data-settings-tab="file-types" aria-current="false">File Types</button>
                    <button type="button" data-settings-tab="paperless" aria-current="false">Paperless</button>
                    <button type="button" data-settings-tab="scheduling" aria-current="false">Scheduling</button>
                    <button type="button" data-settings-tab="rules" aria-current="false">AI Rules</button>
                    <button type="button" data-settings-tab="ocr" aria-current="false">OCR</button>
                </nav>
                <div id="settings-feedback" class="settings-feedback" role="status" aria-live="polite"></div>
                <div class="settings-content">
                    <section class="settings-section" data-section="llm">
                        <h3>Local LLM</h3>
                        <label>Ollama URL <input id="setting-url" type="url" required
                            value="${escapeHtml(settings.ollama_url)}" placeholder="http://192.168.1.2:11434"></label>
                        <label>Model <input id="setting-model" required list="settings-model-options"
                            value="${escapeHtml(settings.model)}"></label>
                        <datalist id="settings-model-options"></datalist>
                        <button type="button" class="secondary" id="settings-model-test">Test connection / discover models</button>
                        <div id="settings-model-results" class="hint" role="status"></div>
                        <label>Request timeout (seconds) <input id="setting-timeout" type="number" min="5"
                            max="1800" required value="${settings.timeout}"></label>
                        <label>Temperature (0–2; lower is more consistent)
                            <input id="setting-temperature" type="number" min="0" max="2" step="0.05"
                                required value="${escapeHtml(settings.temperature)}"></label>
                        <p class="hint">0 favors more repeatable classification; higher settings allow greater variation.
                            This setting applies to subsequent Ollama requests and persists in SQLite.</p>
                        <label>Document text limit (characters) <input id="setting-characters" type="number"
                            min="500" max="100000" required value="${settings.max_content_chars}"></label>
                    </section>
                    <section class="settings-section hidden" data-section="folders">
                        <h3>Scan roots and excluded folders</h3>
                        <p>One absolute folder path per line. Exclusions include every subfolder.</p>
                        <label>Multiple scan folders <textarea id="setting-scan-paths" rows="5"
                            required>${escapeHtml(settings.scan_paths.join('\n'))}</textarea></label>
                        <label>Ignored folders (recursive) <textarea id="setting-exclude-paths" rows="5"
                            >${escapeHtml(settings.exclude_paths.join('\n'))}</textarea></label>
                        <label>Browse folders <input id="settings-path-picker" list="settings-folder-options"
                            placeholder="Choose a Nextcloud folder"></label>
                        <datalist id="settings-folder-options"></datalist>
                        <div class="settings-inline-actions">
                            <button type="button" class="secondary" data-add-folder="scan">Add to scan folders</button>
                            <button type="button" class="secondary" data-add-folder="exclude">Add to ignored folders</button>
                            <button type="button" class="secondary" id="settings-refresh-folders">Refresh folder list</button>
                        </div>
                    </section>
                    <section class="settings-section hidden" data-section="file-types">
                        <h3>Choose which file types to analyze</h3>
                        <p>Select formats by name. The server maps each selection to supported extensions
                            and MIME types. This applies to Unprocessed, scheduled scans and manual analysis.
                            History and existing suggestions are never erased by changing this list.</p>
                        <div class="file-type-grid" role="group" aria-label="Allowed file types">
                            ${fileTypeCatalog.map((type) => `
                                <label class="file-type-choice">
                                    <input type="checkbox" name="setting-file-type"
                                        value="${escapeHtml(type.id)}"
                                        ${checked((settings.file_types || []).includes(type.id))}>
                                    <span><strong>${escapeHtml(type.label)}</strong>
                                        <small>${escapeHtml(type.extensions.join(', '))}</small>
                                        <small>${escapeHtml(type.description)}</small>
                                    </span>
                                </label>`).join('')}
                        </div>
                        <p class="hint">Uncheck every type to pause new file processing.
                            Existing Review and History entries remain available. Old .doc files are not
                            offered because this organizer does not have a real .doc extractor.
                            Credential-like filenames are not an exception to disabled file types.</p>
                    </section>
                    <section class="settings-section hidden" data-section="paperless">
                        <h3>Paperless integration</h3>
                        <p class="hint">Optional. This only moves a file into the configured Nextcloud consume folder; Paperless must already be connected to that folder.</p>
                        <label class="settings-toggle"><input id="setting-paperless-enabled" type="checkbox"
                            ${checked(settings.paperless_enabled)}> Enable Paperless integration</label>
                        <label>Nextcloud Paperless consume folder
                            <input id="setting-paperless-inbox" type="text" required list="settings-folder-options"
                                value="${escapeHtml(settings.paperless_inbox)}" placeholder="/inbox"></label>
                        <label>Preferred document categories (one per line)
                            <textarea id="setting-paperless-prefer-send" rows="7"
                                placeholder="receipt&#10;invoice&#10;statement&#10;tax"
                                >${escapeHtml((settings.paperless_prefer_send || []).join('\n'))}</textarea></label>
                        <p class="hint">Matches the document category, not a filename keyword. For example: receipt, invoice, statement, tax.
                            Configured Never-send restrictions (including resumes) take precedence. A preference only recommends Paperless;
                            you still choose the destination manually. Automatic Apply never sends files to Paperless.</p>
                        <p class="hint">When disabled, the AI only recommends Nextcloud. Enabling integration does not make Paperless routing automatic.</p>
                    </section>
                    <section class="settings-section hidden" data-section="scheduling">
                        <h3>Scheduling and automation</h3>
                        <label class="settings-toggle"><input id="setting-schedule" type="checkbox"
                            ${checked(settings.schedule_enabled)}> Enable scheduled scans</label>
                        <label>Scan interval (minutes) <input id="setting-interval" type="number"
                            min="1" max="10080" required value="${settings.interval_minutes}"></label>
                        <label class="settings-toggle"><input id="setting-auto-analyze" type="checkbox"
                            ${checked(settings.auto_analyze)}> Automatically analyze unprocessed files</label>
                        <div class="settings-warning" role="note">
                            <strong>Warning: Automatic Apply changes actual files.</strong>
                            A high-confidence AI response can still be wrong. Files may be renamed or moved
                            to the wrong folder. Verify numerous suggestions manually before enabling this.
                            Paperless routing is never automatic, and the confidence requirement is fixed at 95%.
                        </div>
                        <label class="settings-toggle"><input id="setting-warning" type="checkbox"
                            ${checked(settings.auto_apply_warning_accepted)}> I understand and accept this risk.</label>
                        <label class="settings-toggle"><input id="setting-auto-apply" type="checkbox"
                            ${checked(settings.auto_apply)}> Automatically apply eligible suggestions (≥95%)</label>
                        <p class="hint">Only pending, never partly applied, non-Paperless suggestions qualify.
                            Automatic Apply is off by default.</p>
                        <div class="settings-run"><strong>Last scheduled run</strong><div>
                            ${escapeHtml(automation?.finished_at || automation?.started_at || 'Never')}
                            · Analyzed ${Number(automation?.analyzed) || 0}
                            · Applied ${Number(automation?.auto_applied) || 0}
                            · Failures ${Number(automation?.failed) || 0}
                            ${automation?.last_error ? `<p>${escapeHtml(automation.last_error)}</p>` : ''}
                        </div></div>
                    </section>
                    <section class="settings-section hidden" data-section="ocr">
                        <h3>PDF OCR</h3>
                        <p>When a PDF has no searchable text, attempt local OCR. The original file is never changed.</p>
                        <label class="settings-toggle"><input type="checkbox" id="setting-ocr-enabled"
                            ${checked(settings.ocr_enabled !== false)}> Enable automatic OCR fallback</label>
                        <label>Maximum pages per OCR attempt (1–100)
                            <input type="number" id="setting-ocr-pages" min="1" max="100" required
                                value="${Number(settings.ocr_max_pages ?? 10)}"></label>
                        <label class="settings-toggle"><input type="checkbox" checked disabled>
                            Require manual Review for OCR-generated suggestions (always on)</label>
                        <p class="hint">PDFs above the page limit, unreadable PDFs and unsuccessful OCR appear in Failed.
                            They can be previewed, retried or ignored. Retry after raising the limit.
                            OCR text is cached only in process memory, not SQLite.</p>
                    </section>
                    <section class="settings-section hidden" data-section="rules">
                        <h3>Local AI instructions</h3>
                        <p>Advisory preferences only. Application safety and Paperless exclusions take precedence.</p>
                        <label>Global instructions <textarea id="setting-global-rules" rows="7"
                            maxlength="8000" placeholder="Prefer existing folders; retain resumes in Nextcloud…"
                            >${escapeHtml(settings.global_instructions)}</textarea></label>
                        <h3>Folder-specific rules</h3>
                        <div id="settings-rules"></div>
                        <button type="button" class="secondary" id="settings-add-rule">Add folder rule</button>
                    </section>
                </div>`;
            settingsRows(settingsPanel.querySelector('#settings-rules'), settings.folder_rules);
            showSettingsTab(activeTab);
        }

        function showSettingsTab(tab) {
            if (!settingTabs.includes(tab)) return;
            settingsPanel.querySelectorAll('[data-settings-tab]').forEach((button) => {
                button.setAttribute('aria-current', button.dataset.settingsTab === tab ? 'page' : 'false');
            });
            settingsPanel.querySelectorAll('[data-section]').forEach((section) => {
                section.classList.toggle('hidden', section.dataset.section !== tab);
            });
        }

        function settingsValue(id) {
            return settingsPanel.querySelector(`#${id}`);
        }

        function collectSettings() {
            const lines = (id) => settingsValue(id).value.split(/\r?\n/)
                .map((value) => value.trim()).filter(Boolean);
            return {
                ollama_url: settingsValue('setting-url').value.trim(),
                model: settingsValue('setting-model').value.trim(),
                timeout: Number(settingsValue('setting-timeout').value),
                temperature: Number(settingsValue('setting-temperature').value),
                max_content_chars: Number(settingsValue('setting-characters').value),
                ocr_enabled: settingsValue('setting-ocr-enabled').checked,
                ocr_max_pages: Number(settingsValue('setting-ocr-pages').value),
                file_types: Array.from(settingsPanel.querySelectorAll('input[name="setting-file-type"]:checked'),
                    (checkbox) => checkbox.value),
                scan_paths: lines('setting-scan-paths'),
                exclude_paths: lines('setting-exclude-paths'),
                schedule_enabled: settingsValue('setting-schedule').checked,
                interval_minutes: Number(settingsValue('setting-interval').value),
                auto_analyze: settingsValue('setting-auto-analyze').checked,
                auto_apply: settingsValue('setting-auto-apply').checked,
                auto_apply_warning_accepted: settingsValue('setting-warning').checked,
                minimum_auto_confidence: 0.95,
                paperless_enabled: settingsValue('setting-paperless-enabled').checked,
                paperless_inbox: settingsValue('setting-paperless-inbox').value.trim(),
                paperless_prefer_send: lines('setting-paperless-prefer-send'),
                global_instructions: settingsValue('setting-global-rules').value,
                folder_rules: Array.from(settingsPanel.querySelectorAll('.settings-rule')).map((row) => ({
                    folder: row.querySelector('[data-rule-folder]').value.trim(),
                    instructions: row.querySelector('[data-rule-text]').value
                }))
            };
        }

        function settingsFeedback(message, error = false) {
            const element = settingsPanel.querySelector('#settings-feedback');
            if (!element) return;
            element.classList.toggle('error', error);
            element.textContent = message;
        }

        async function loadSettings() {
            settingsPanel.textContent = 'Loading administrator settings…';
            try {
                const data = await api('api/settings');
                if (state.view !== 'settings') return;
                fileTypeCatalog = Array.isArray(data.file_types_catalog) ? data.file_types_catalog : [];
                renderSettings(data.settings, data.automation);
            } catch (error) {
                settingsPanel.textContent = `Unable to load settings: ${error.message}`;
            }
        }

        async function discoverModels() {
            const output = settingsPanel.querySelector('#settings-model-results');
            output.textContent = 'Checking Ollama…';
            try {
                const result = await api('api/settings/models');
                output.textContent = `Connected. ${result.models.length} model(s) found. Save the URL first if you changed it.`;
                settingsValue('settings-model-options').innerHTML = result.models.map((model) =>
                    `<option value="${escapeHtml(model)}"></option>`).join('');
            } catch (error) {
                output.textContent = `Connection failed: ${error.message}`;
            }
        }

        async function discoverFolders() {
            settingsFeedback('Loading Nextcloud folders…');
            try {
                const result = await api('api/settings/folders');
                settingsValue('settings-folder-options').innerHTML = result.folders.map((folder) =>
                    `<option value="${escapeHtml(folder)}"></option>`).join('');
                settingsFeedback(`${result.folders.length} folder(s) loaded.`);
            } catch (error) {
                settingsFeedback(`Folder discovery failed: ${error.message}`, true);
            }
        }

        async function saveSettings() {
            if (settingsBusy) return;
            settingsBusy = true;
            try {
                const value = collectSettings();
                if (value.auto_apply && !value.auto_apply_warning_accepted) {
                    settingsFeedback('Acknowledge the Automatic Apply warning before saving.', true);
                    showSettingsTab('scheduling');
                    return;
                }
                const data = await api('api/settings', {
                    method: 'PUT', body: JSON.stringify({settings: value})
                });
                renderSettings(data.settings, null);
                settingsFeedback('Settings saved to SQLite and applied to the running organizer.');
            } catch (error) {
                settingsFeedback(`Settings not saved: ${error.message}`, true);
            } finally {
                settingsBusy = false;
            }
        }

        settingsPanel.addEventListener('click', (event) => {
            const button = event.target.closest('button');
            if (!button || !settingsPanel.contains(button)) return;
            if (button.dataset.settingsTab) showSettingsTab(button.dataset.settingsTab);
            else if (button.id === 'settings-save') void saveSettings();
            else if (button.id === 'settings-model-test') void discoverModels();
            else if (button.id === 'settings-refresh-folders') void discoverFolders();
            else if (button.id === 'settings-add-rule' || button.hasAttribute('data-remove-rule')) {
                const rules = Array.from(settingsPanel.querySelectorAll('.settings-rule')).map((row) => ({
                    folder: row.querySelector('[data-rule-folder]').value,
                    instructions: row.querySelector('[data-rule-text]').value
                }));
                if (button.id === 'settings-add-rule') rules.push({folder: '', instructions: ''});
                else rules.splice(Number(button.dataset.removeRule), 1);
                settingsRows(settingsPanel.querySelector('#settings-rules'), rules);
            } else if (button.dataset.addFolder) {
                const folder = settingsValue('settings-path-picker').value.trim();
                if (!folder) return;
                const target = settingsValue(button.dataset.addFolder === 'scan'
                    ? 'setting-scan-paths' : 'setting-exclude-paths');
                const existing = target.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
                if (!existing.includes(folder)) existing.push(folder);
                target.value = existing.join('\n');
            }
        });

        function setStatus(message, kind = '') {
            statusEl.className = `status-card ${kind}`.trim();
            // All interpolated external values must pass through escapeHtml().
            statusEl.innerHTML = message;
        }

        function clearSelection(message = 'Select a file above to inspect it.') {
            state.currentFileId = null;
            state.currentSuggestion = null;
            state.currentFile = null;
            state.historyRecord = null;
            reanalyzeButton.textContent = 'Re-analyze';
            showPreview(null);
            suggestionEl.innerHTML = '';
            suggestionEl.classList.add('hidden');
            reanalyzeButton.disabled = true;
            editorTitle.textContent = state.view === 'history' ? 'Historical record' : 'Selected file';
            editor.scrollTop = 0;
            setStatus(message);
        }

        function setBadge(view, count) {
            const badge = sidebar.querySelector(`#${view}-count`);
            if (badge) badge.textContent = String(Number(count) || 0);
        }

        function renderSuggestion(context, suggestion) {
            state.historyRecord = null;
            reanalyzeButton.textContent = 'Re-analyze';
            state.currentSuggestion = suggestion;
            showPreview(context.file_id || state.currentFileId);
            const paperless = Boolean(suggestion.paperless_candidate && suggestion.paperless_enabled);
            const confidence = `${Math.round((Number(suggestion.confidence) || 0) * 100)}%`;
            const tags = Array.isArray(suggestion.tags) ? suggestion.tags : [];
            const legacy = Boolean(suggestion.paperless_candidate && !suggestion.dual_options_ready);
            const selection = suggestion.selected_destination || null;
            editorTitle.textContent = 'Selected file';
            suggestionEl.innerHTML = `
                ${suggestion.ocr_used ? '<div class="ocr-notice" role="note">OCR text used · manual Review required; Automatic Apply disabled for this suggestion.</div>' : ''}
                <div class="file-title">
                    <div>
                        <span class="eyebrow">${paperless ? 'Two destination choices · AI recommends Paperless' : 'Nextcloud recommendation'}</span>
                        <h2>${escapeHtml(context.name || context.path || '(unnamed)')}</h2>
                        <code>${escapeHtml(context.path || '')}</code>
                    </div>
                    <div class="confidence"><strong>${confidence}</strong><span>confidence</span></div>
                </div>
                ${paperless ? `
                    <section class="destination-option paperless-box" aria-label="Send to Paperless">
                        <h3>Option 1 · Send to Paperless <span class="destination-label">AI suggested</span></h3>
                        <p><strong>Why:</strong> ${escapeHtml(suggestion.reason || 'The AI considers this a document for Paperless. Review before applying.')}</p>
                        <p class="destination">Consume folder: <code>${escapeHtml(suggestion.paperless_inbox || '/inbox')}</code></p>
                        <button id="apply-paperless" type="button" class="primary"
                            ${selection === 'nextcloud' ? 'disabled' : ''}>Send to Paperless</button>
                        ${selection === 'nextcloud' ? '<p class="hint">Nextcloud changes have already started. Re-analyze to make a different choice.</p>' : ''}
                    </section>` : ''}
                <section class="destination-option nextcloud-option" aria-label="${paperless ? 'Keep in Nextcloud' : 'Nextcloud suggestion'}">
                    <h3>${paperless ? 'Option 2 · Keep in Nextcloud' : 'Nextcloud suggestion'}
                        ${paperless ? '<span class="destination-label">Your alternative</span>' : ''}</h3>
                    ${legacy ? `<p class="settings-warning">This suggestion predates the two-option workflow and has no separate Nextcloud details.
                        Select Re-analyze above to generate both choices. The file has not been changed.</p>` : `
                    <div class="form-grid">
                        <label>Filename<input id="filename" value="${escapeHtml(suggestion.suggested_filename || '')}"></label>
                        <label>Folder<input id="folder" value="${escapeHtml(suggestion.suggested_folder || '')}"></label>
                        <fieldset class="wide tags-fieldset">
                            <legend>Nextcloud tags</legend>
                            <div class="tag-options">${tags.length ? tags.map((tag) => `
                                <label class="tag-option">
                                    <input type="checkbox" name="suggested-tags" value="${escapeHtml(tag)}" checked>
                                    <span>${escapeHtml(tag)}</span>
                                </label>`).join('') : '<span class="hint">No tags suggested. Add your own below.</span>'}
                            </div>
                            <div class="manual-tag-entry">
                                <label for="manual-tag-input">Add a tag</label>
                                <input id="manual-tag-input" type="text" maxlength="60"
                                       placeholder="Existing or new tag" autocomplete="off">
                                <button type="button" class="secondary" data-add-tag>Add tag</button>
                            </div>
                            <p class="hint">Checked tags are added to the file when you choose Apply tags or Apply all.</p>
                        </fieldset>
                        <div class="wide metadata">
                            <span><strong>Category:</strong> ${escapeHtml(suggestion.category || '—')}</span>
                            <span><strong>AI reasoning:</strong> ${escapeHtml(suggestion.reason || '—')}</span>
                        </div>
                    </div>
                    <div class="actions">
                        <button type="button" data-action="filename" class="secondary"
                            ${selection === 'paperless' ? 'disabled' : ''}>Apply filename</button>
                        <button type="button" data-action="folder" class="secondary"
                            ${selection === 'paperless' ? 'disabled' : ''}>Apply folder</button>
                        <button type="button" data-action="tags" class="secondary"
                            ${selection === 'paperless' ? 'disabled' : ''}>Apply tags</button>
                        <button type="button" data-action="all" class="primary"
                            ${selection === 'paperless' ? 'disabled' : ''}>${paperless ? 'Keep in Nextcloud · Apply all' : 'Apply all'}</button>
                    </div>`}
                </section>
                <div class="decision-actions" aria-label="File decisions">
                    <button type="button" class="secondary" data-decision="ignore">Ignore file</button>
                    ${suggestion.suggestion_id ? '<button type="button" class="secondary" data-decision="reject">Reject entire suggestion</button>' : ''}
                </div>`;
            suggestionEl.classList.remove('hidden');
            editor.scrollTop = 0;
            reanalyzeButton.disabled = false;
        }

        function showHistoryRecord(record) {
            const tags = Array.isArray(record.tags) ? record.tags : [];
            const appliedTags = Array.isArray(record.applied_tags) ? record.applied_tags : null;
            const actions = Array.isArray(record.applied_actions) ? record.applied_actions : [];
            const detail = (label, value, code = false) => `
                <div class="history-detail">
                    <dt>${escapeHtml(label)}</dt>
                    <dd>${code && value ? `<code>${escapeHtml(value)}</code>` : escapeHtml(value ?? '—')}</dd>
                </div>`;
            state.currentFileId = null;
            state.currentSuggestion = null;
            state.historyRecord = record;
            showPreview(record.deleted ? null : record.file_id);
            const canReanalyze = !record.deleted && /^\d+$/.test(String(record.file_id || ''))
                && ['ignored', 'applied', 'rejected'].includes(record.status);
            editorTitle.textContent = 'Historical record';
            reanalyzeButton.textContent = 'Re-analyze from History';
            reanalyzeButton.disabled = !canReanalyze;
            suggestionEl.innerHTML = `
                <div class="file-title">
                    <div>
                        <span class="eyebrow">History · ${escapeHtml(record.status || 'unknown')}</span>
                        <h2>${escapeHtml(record.name || '(unnamed)')}</h2>
                    </div>
                </div>
                <dl class="history-details">
                    ${detail('Original path', record.original_path || 'Not recorded for this suggestion', Boolean(record.original_path))}
                    ${detail('Last known path', record.last_known_path, true)}
                    ${detail('Applied path', record.applied_path || 'No applied path recorded', Boolean(record.applied_path))}
                    ${detail('AI suggested filename', record.suggested_filename)}
                    ${detail('AI suggested folder', record.suggested_folder, true)}
                    ${detail('AI suggested tags', tags.join(', ') || '—')}
                    ${detail('Actually applied tags', appliedTags === null ? 'Not recorded' : appliedTags.join(', ') || 'None')}
                    ${detail('Completed actions', actions.join(', ') || 'Not recorded')}
                    ${detail('Category', record.category)}
                    ${detail('Paperless recommended', record.paperless_candidate ? 'Yes' : 'No')}
                    ${detail('Destination actually chosen', record.selected_destination || 'Not recorded')}
                    ${detail('Confidence', `${Math.round((Number(record.confidence) || 0) * 100)}%`)}
                    ${detail('OCR extraction', record.ocr_used ? 'Yes — manually reviewed' : 'No / not recorded')}
                    ${detail('AI reason', record.reason)}
                    ${detail('Analyzed', formatDate(record.created_at))}
                    ${detail('History event', formatDate(record.event_at))}
                    ${record.deleted ? detail('Unavailable detected', formatDate(record.deleted_at)) : ''}
                    ${record.deleted ? detail('Unavailable note', record.deletion_note || 'No note recorded') : ''}
                    ${record.event_note ? detail('Decision note', record.event_note) : ''}
                </dl>
                <p class="hint">Historical decisions and completed actions remain unchanged.</p>
                ${!canReanalyze ? '<p class="hint">Re-analysis is unavailable for this entry. The file may have been deleted or become inaccessible.</p>' : ''}`;
            suggestionEl.classList.remove('hidden');
            editor.scrollTop = 0;
            setStatus(canReanalyze ? 'History is preserved. Re-analyze to create a NEW suggestion for Review.' : 'Historical record selected.');
        }

        function showFailedRecord(record) {
            state.currentFile = record;
            state.currentFileId = String(record.file_id);
            state.currentSuggestion = null;
            showPreview(record.file_id);
            editorTitle.textContent = 'Failed analysis';
            reanalyzeButton.disabled = false;
            suggestionEl.innerHTML = `
                <div class="file-title"><div>
                    <span class="eyebrow">Analysis failed</span>
                    <h2>${escapeHtml(record.name || record.path)}</h2>
                    <code>${escapeHtml(record.path)}</code>
                </div></div>
                <div class="metadata failure-details">
                    <p><strong>Stage:</strong> ${escapeHtml(record.stage || 'analyze')}</p>
                    <p><strong>Attempts:</strong> ${Number(record.attempts) || 1}</p>
                    <p><strong>Last failure:</strong> ${escapeHtml(formatDate(record.last_failed_at))}</p>
                    <p><strong>Error:</strong> ${escapeHtml(record.error || 'Unknown error')}</p>
                </div>
                <div class="decision-actions">
                    <button type="button" class="primary" data-failed-retry>${record.stage === 'ocr' ? 'Retry OCR' : 'Retry analysis'}</button>
                    <button type="button" class="secondary" data-decision="ignore">Ignore file</button>
                </div>`;
            suggestionEl.classList.remove('hidden');
            editor.scrollTop = 0;
            setStatus('Failure saved. Retry analysis or ignore this file.');
        }

        async function recordProcessingError(error, fileId, path, stage) {
            // Backend already saved the OCR failure before returning 422.
            if (error.status === 422 && (/^(OCR|Extraction):/.test(String(error.message)))) return true;
            // Do not reinterpret a known removed file as a processing failure.
            if ([403, 410, 415].includes(error.status)) return false;
            if (!path) return false;
            try {
                await api('api/dashboard/failed/report', {
                    method: 'POST',
                    body: JSON.stringify({file_id: fileId, path, error: error.message, stage})
                });
                return true;
            } catch (reportError) {
                console.warn('AI Organizer: failure could not be saved', reportError);
                return false;
            }
        }

        async function decide(record, decision) {
            if (state.busy || state.view === 'history' || !record?.file_id) return;
            const suggestionId = record.suggestion_id ?? (
                String(record.file_id) === String(state.currentFileId)
                    ? state.currentSuggestion?.suggestion_id : null
            );
            if (decision === 'reject' && !suggestionId) {
                setStatus('Reject requires a saved suggestion. Choose Ignore for an unprocessed file.', 'error');
                return;
            }
            state.busy = true;
            try {
                const result = await api('api/dashboard/decision', {
                    method: 'POST',
                    body: JSON.stringify({
                        file_id: String(record.file_id), decision,
                        path: record.path || '', etag: record.etag || '',
                        suggestion_id: decision === 'reject' ? suggestionId : null
                    })
                });
                clearSelection(`${result.status === 'ignored' ? 'File ignored' : 'Suggestion rejected'}. Decision recorded in History; the Nextcloud file was not changed.`);
                await refreshDashboard();
            } catch (error) {
                setStatus(`<strong>Unable to save decision:</strong> ${escapeHtml(error.message)}`, 'error');
            } finally {
                state.busy = false;
            }
        }

        function renderFileList() {
            const view = state.view;
            const history = view === 'history';
            const failed = view === 'failed';
            const review = view === 'review';
            const titles = {
                unprocessed: 'Unprocessed files', review: 'Review', failed: 'Failed', history: 'History'
            };
            const subtitles = {
                unprocessed: 'Files waiting for AI analysis',
                review: 'Suggestions awaiting your decision',
                failed: 'Analysis errors awaiting retry or ignore',
                history: 'Completed decisions and archived files'
            };
            const first = state.count ? state.offset + 1 : 0;
            const last = state.offset + state.items.length;
            const filter = history ? `
                <label class="history-filter-label" for="history-filter">Status
                    <select id="history-filter">
                        ${HISTORY_STATUSES.map((status) => `
                            <option value="${status}" ${state.historyFilter === status ? 'selected' : ''}>
                                ${status === 'all' ? 'All statuses' : status[0].toUpperCase() + status.slice(1)}
                            </option>`).join('')}
                    </select>
                </label>` : '';
            const rows = state.items.map((item, index) => {
                const name = escapeHtml(item.name || item.path || item.last_known_path || '(unnamed)');
                const path = escapeHtml(history ? (item.original_path || item.last_known_path || '') : (item.path || ''));
                const rawStatus = String(item.status || 'unknown').toLowerCase();
                const label = escapeHtml(rawStatus.charAt(0).toUpperCase() + rawStatus.slice(1));
                const badge = `<span class="status-badge" data-status="${escapeHtml(rawStatus)}">${label}</span>`;
                const preview = (!history || !item.deleted) ? previewHref(item.file_id) : null;
                const previewAction = preview ? `<a class="organizer-preview" href="${escapeHtml(preview)}"
                    target="_blank" rel="noopener noreferrer" aria-label="Preview ${name} in Nextcloud">Preview ↗</a>` : '';
                const buttons = history
                    ? `<div class="row-actions"><button type="button" data-history-index="${index}">Details</button>${previewAction}</div>`
                    : `<div class="row-actions">
                        ${previewAction}
                        <button type="button" data-file-index="${index}">${review ? 'Revisit' : failed ? 'Retry' : 'Analyze'}</button>
                        ${review ? `<button type="button" class="secondary" data-decision="reject" data-record-index="${index}">Reject</button>` : ''}
                        <button type="button" class="secondary" data-decision="ignore" data-record-index="${index}">Ignore</button>
                    </div>`;
                return `<div class="organizer-file-row">
                    <div class="organizer-file-details">
                        <strong>${name}</strong><small>${path}</small>
                        <div class="record-status">${badge}
                            ${history ? `<small>${escapeHtml(formatDate(item.event_at))}</small>` : ''}
                        </div>
                        ${failed ? `<small class="failure-summary" title="${escapeHtml(item.error || '')}">${escapeHtml(item.error || 'Unknown error')}</small>` : ''}
                    </div>
                    ${buttons}
                </div>`;
            }).join('');
            const empty = history ? 'No completed or archived suggestions match this filter.'
                : failed ? 'No failed analyses.'
                    : review ? 'No suggestions awaiting review.' : 'No files awaiting analysis.';
            fileList.innerHTML = `
                <div class="organizer-list-header">
                    <div><h2>${titles[view]}</h2><p>${subtitles[view]}</p></div>
                    <span class="organizer-list-total">${history ? `${first}–${last}` : state.items.length} of ${state.count}</span>
                </div>
                ${filter ? `<div class="history-toolbar">${filter}</div>` : ''}
                <div class="organizer-list-rows">${rows || `<p class="organizer-empty">${empty}</p>`}</div>`;
            moreButton.hidden = history || state.items.length >= state.count;
            moreButton.disabled = false;
            previousButton.hidden = !history;
            nextButton.hidden = !history;
            pageLabel.hidden = !history;
            previousButton.disabled = !history || state.offset === 0;
            nextButton.disabled = !history || last >= state.count;
            pageLabel.textContent = `${first}–${last} of ${state.count}`;
        }

        async function loadView(append = false) {
            const request = ++state.request;
            const view = state.view;
            const offset = view === 'history' ? state.offset : append ? state.items.length : 0;
            if (view !== 'history') state.offset = offset;
            if (!append) fileList.textContent = `Loading ${view}…`;
            moreButton.disabled = true;
            previousButton.disabled = true;
            nextButton.disabled = true;
            try {
                const status = view === 'history' ? `&status=${encodeURIComponent(state.historyFilter)}` : '';
                const result = await api(`api/dashboard/${view}?limit=${PAGE_SIZE}&offset=${offset}${status}`);
                if (request !== state.request || view !== state.view) return;
                state.items = append && view !== 'history'
                    ? state.items.concat(result.items || []) : (result.items || []);
                state.count = Number(result.count) || 0;
                setBadge(view, state.count);
                renderFileList();
                if (view === 'history') {
                    setStatus(`History: ${state.count} record(s). Select an entry to inspect it.`);
                }
            } catch (error) {
                if (request !== state.request || view !== state.view) return;
                if (append) {
                    setStatus(`Unable to load more files: ${escapeHtml(error.message)}`, 'error');
                } else {
                    fileList.innerHTML = `<p class="organizer-empty">Unable to load ${view}: ${escapeHtml(error.message)}</p>`;
                    setStatus(`Unable to load ${view}: ${escapeHtml(error.message)}`, 'error');
                }
                moreButton.hidden = true;
                previousButton.hidden = true;
                nextButton.hidden = true;
                pageLabel.hidden = true;
            }
        }

        async function refreshDashboard() {
            await loadView();
            // These count requests have no effect on which list is currently visible.
            const others = VIEWS.filter((view) => view !== state.view && view !== 'settings');
            await Promise.all(others.map(async (view) => {
                try {
                    const result = await api(`api/dashboard/${view}?limit=1&offset=0`);
                    setBadge(view, result.count);
                } catch (error) {
                    console.warn(`AI Organizer: ${view} count unavailable`, error);
                }
            }));
        }

        function selectView(view) {
            if (state.busy || !VIEWS.includes(view)) return;
            state.view = view;
            state.offset = 0;
            state.items = [];
            state.request++; // Invalidate responses from the previous view.
            sidebar.querySelectorAll('button[data-view]').forEach((button) => {
                button.setAttribute('aria-current', button.dataset.view === view ? 'page' : 'false');
            });
            mainPanel.classList.toggle('hidden', view === 'settings');
            settingsPanel.classList.toggle('hidden', view !== 'settings');
            if (view === 'settings') {
                void loadSettings();
                return;
            }
            clearSelection(view === 'history' 
                ? 'Select a History entry to inspect its recorded details.'
                : view === 'failed' ? 'Select a failed file to review its error or retry analysis.'
                    : 'Select a file above to analyze or review its suggestion.');
            void refreshDashboard();
        }

        async function analyze(force = false) {
            if (!state.currentFileId || state.view === 'history') return;
            const fileId = state.currentFileId;
            let currentPath = state.currentFile?.path || '';
            state.currentSuggestion = null;
            suggestionEl.classList.add('hidden');
            reanalyzeButton.disabled = true;
            setStatus('<span class="spinner"></span> Analyzing file with the local AI…', 'working');
            try {
                const context = await api(`api/context/${encodeURIComponent(fileId)}`);
                currentPath = context.path || currentPath;
                const suggestion = await api(
                    `api/analyze/${encodeURIComponent(fileId)}?force=${force ? 'true' : 'false'}`,
                    {method: 'POST'}
                );
                // Resolve only after successful analysis, not when a retry begins.
                try {
                    await api(`api/dashboard/failed/${encodeURIComponent(fileId)}/resolve`, {method: 'POST'});
                } catch (resolveError) {
                    console.warn('AI Organizer: analysis succeeded but failure resolution failed', resolveError);
                }
                if (state.currentFileId !== fileId || state.view === 'history') return;
                state.currentFile = { ...(state.currentFile || {}),
                    file_id: fileId, path: currentPath, etag: context.etag || '' };
                renderSuggestion(context, suggestion);
                setStatus('Suggestion ready. Nothing changes until you click an Apply button.', 'success');
                void refreshDashboard();
            } catch (error) {
                const saved = await recordProcessingError(error, fileId, currentPath, 'analyze');
                if (state.currentFileId !== fileId) return;
                if (saved) {
                    clearSelection(`Analysis failed: ${escapeHtml(error.message)}. Saved to Failed; choose Failed to retry.`);
                    setStatus(`Analysis failed: ${escapeHtml(error.message)}. Saved to Failed; choose Failed to retry.`, 'error');
                    void refreshDashboard();
                } else {
                    reanalyzeButton.disabled = false;
                    setStatus(`<strong>Unable to analyze:</strong> ${escapeHtml(error.message)}. Failure could not be saved (or file was archived).`, 'error');
                }
            }
        }

        async function reanalyzeHistorical() {
            const record = state.historyRecord;
            if (state.busy || state.view !== 'history' || !record || record.deleted ||
                !['ignored', 'applied', 'rejected'].includes(record.status)) return;
            if (record.status === 'ignored' && !window.confirm(
                'This will unignore the file and generate a new AI suggestion. Previous History records will remain unchanged. Continue?'
            )) return;
            state.busy = true;
            reanalyzeButton.disabled = true;
            setStatus('<span class="spinner"></span> Checking the current Nextcloud file and generating a new suggestion…', 'working');
            try {
                const result = await api(`api/dashboard/history/${encodeURIComponent(record.file_id)}/reanalyze`, {
                    method: 'POST',
                    body: JSON.stringify({record_id: record.suggestion_id, status: record.status})
                });
                state.busy = false;
                selectView('review');
                state.currentFileId = String(result.context.file_id);
                state.currentFile = {...result.context};
                renderSuggestion(result.context, result.suggestion);
                setStatus('New suggestion created in Review. Previous History records were preserved. '
                    + 'Any earlier Review suggestion was archived as Superseded. Automatic Apply is disabled for this suggestion.', 'success');
            } catch (error) {
                reanalyzeButton.disabled = false;
                setStatus(`<strong>Re-analysis failed:</strong> ${escapeHtml(error.message)}. Historical records remain unchanged; check Failed if analysis started.`, 'error');
            } finally {
                state.busy = false;
            }
        }

        async function apply(actions, destination = 'nextcloud') {
            if (!state.currentFileId || !state.currentSuggestion || state.busy || state.view === 'history') return;
            const fileId = state.currentFileId;
            const suggestion = state.currentSuggestion;
            const body = {suggestion_id: suggestion.suggestion_id, actions, destination};
            if (destination === 'nextcloud') {
                if (actions.includes('tags') && !addManualTags()) return;
                body.suggested_filename = suggestionEl.querySelector('#filename').value.trim();
                body.suggested_folder = suggestionEl.querySelector('#folder').value.trim();
                body.tags = Array.from(suggestionEl.querySelectorAll('input[name="suggested-tags"]:checked'),
                    (checkbox) => checkbox.value);
            }
            state.busy = true;
            suggestionEl.querySelectorAll('button').forEach((button) => { button.disabled = true; });
            setStatus('<span class="spinner"></span> Applying selected change(s)…', 'working');
            try {
                const result = await api(`api/apply/${encodeURIComponent(fileId)}`, {
                    method: 'POST', body: JSON.stringify(body)
                });
                setStatus(`<strong>Applied.</strong> Current path: <code>${escapeHtml(result.path)}</code>`, 'success');
                if (result.complete) {
                    state.currentFileId = null;
                    state.currentSuggestion = null;
                    suggestionEl.classList.add('hidden');
                    reanalyzeButton.disabled = true;
                } else {
                    // Partial Apply remains in Review. The saved suggestion is still editable.
                    suggestionEl.querySelectorAll('button').forEach((button) => { button.disabled = false; });
                }
                void refreshDashboard();
            } catch (error) {
                suggestionEl.querySelectorAll('button').forEach((button) => { button.disabled = false; });
                setStatus(`<strong>Apply failed:</strong> ${escapeHtml(error.message)}`, 'error');
                void refreshDashboard();
            } finally {
                state.busy = false;
            }
        }

        // One delegated listener for sidebar navigation (including History).
        sidebar.addEventListener('click', (event) => {
            const button = event.target.closest('button[data-view]');
            if (button && sidebar.contains(button) && (!button.hidden) && (button.dataset.view !== 'settings' || settingsAvailable)) selectView(button.dataset.view);
        });

        // One delegated list listener: History never calls activate/{file_id}.
        fileList.addEventListener('click', async (event) => {
            const decisionButton = event.target.closest('button[data-record-index][data-decision]');
            if (decisionButton && fileList.contains(decisionButton)) {
                const record = state.items[Number(decisionButton.dataset.recordIndex)];
                if (record) void decide(record, decisionButton.dataset.decision);
                return;
            }
            const historyButton = event.target.closest('button[data-history-index]');
            if (historyButton && state.view === 'history') {
                const record = state.items[Number(historyButton.dataset.historyIndex)];
                if (record) showHistoryRecord(record);
                return;
            }
            const button = event.target.closest('button[data-file-index]');
            if (!button || state.view === 'history' || state.busy) return;
            const file = state.items[Number(button.dataset.fileIndex)];
            if (!file || !file.file_id) return;
            const view = state.view;
            const fileId = String(file.file_id);
            state.busy = true;
            button.disabled = true;
            state.currentFileId = fileId;
            showPreview(fileId);
            state.currentFile = file;
            state.currentSuggestion = null;
            suggestionEl.classList.add('hidden');
            reanalyzeButton.disabled = true;
            setStatus('Opening file…', 'working');
            try {
                const context = await api(`api/dashboard/activate/${encodeURIComponent(fileId)}`,
                    {method: 'POST'});
                if (view === 'failed') {
                    showFailedRecord(file);
                    await analyze(true);
                } else if (view === 'review') {
                    const savedEtag = String(file.etag || '').replaceAll('"', '');
                    const liveEtag = String(context.etag || '').replaceAll('"', '');
                    if (savedEtag && liveEtag && savedEtag !== liveEtag) {
                        reanalyzeButton.disabled = false;
                        setStatus('The file changed after its suggestion was saved. Re-analyze before applying.', 'error');
                    } else {
                        renderSuggestion(context, file);
                        setStatus('Saved suggestion loaded. Nothing changes until you click Apply.', 'success');
                    }
                } else {
                    await analyze(false);
                }
            } catch (error) {
                const saved = await recordProcessingError(error, fileId, file.path, 'activate');
                clearSelection('Could not open file.');
                setStatus(`<strong>Unable to open file:</strong> ${escapeHtml(error.message)}${saved ? ' · Saved to Failed.' : ''}`, 'error');
                if (saved) void refreshDashboard();
            } finally {
                state.busy = false;
                button.disabled = false;
            }
        });

        fileList.addEventListener('change', (event) => {
            if (event.target.id !== 'history-filter' || state.view !== 'history') return;
            if (!HISTORY_STATUSES.includes(event.target.value)) return;
            state.historyFilter = event.target.value;
            state.offset = 0;
            clearSelection('Select a History entry to inspect its recorded details.');
            void loadView();
        });

        moreButton.addEventListener('click', () => {
            if (!moreButton.disabled && state.view !== 'history') void loadView(true);
        });
        previousButton.addEventListener('click', () => {
            if (state.view !== 'history' || previousButton.disabled) return;
            state.offset = Math.max(0, state.offset - PAGE_SIZE);
            clearSelection('Select a History entry to inspect its recorded details.');
            void loadView();
        });
        nextButton.addEventListener('click', () => {
            if (state.view !== 'history' || nextButton.disabled) return;
            state.offset += PAGE_SIZE;
            clearSelection('Select a History entry to inspect its recorded details.');
            void loadView();
        });
        function addManualTags() {
            const input = suggestionEl.querySelector('#manual-tag-input');
            const choices = suggestionEl.querySelector('.tag-options');
            if (!input || !choices) return true;
            const tags = input.value.split(',').map((text) => text.trim()).filter(Boolean);
            if (!tags.length) return true;
            // Validate all input before creating any checkbox or applying changes.
            if (tags.some((tag) => tag.length > 60 || /[\x00-\x1f\x7f]/.test(tag))) {
                setStatus('Tag must be at most 60 characters and cannot contain control characters.', 'error');
                return false;
            }
            for (const tag of tags) {
                const existing = Array.from(choices.querySelectorAll('input[name="suggested-tags"]'))
                    .find((checkbox) => checkbox.value.toLocaleLowerCase() === tag.toLocaleLowerCase());
                if (existing) { existing.checked = true; continue; }
                const label = document.createElement('label');
                label.className = 'tag-option';
                const checkbox = document.createElement('input');
                checkbox.type = 'checkbox';
                checkbox.name = 'suggested-tags';
                checkbox.value = tag;
                checkbox.checked = true;
                const name = document.createElement('span');
                name.textContent = tag;
                label.append(checkbox, name);
                choices.appendChild(label);
            }
            const hint = choices.querySelector('.hint');
            if (hint) hint.remove();
            input.value = '';
            return true;
        }

        suggestionEl.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' && event.target.id === 'manual-tag-input') {
                event.preventDefault();
                addManualTags();
            }
        });

        suggestionEl.addEventListener('click', (event) => {
            const button = event.target.closest('button');
            if (!button || !suggestionEl.contains(button)) return;
            if (button.hasAttribute('data-add-tag')) {
                addManualTags();
                return;
            }
            if (state.view === 'history') return;
            if (button.dataset.decision) {
                const record = {
                    ...(state.currentFile || {}),
                    file_id: state.currentFileId,
                    suggestion_id: state.currentSuggestion?.suggestion_id,
                    path: state.currentFile?.path || ''
                };
                void decide(record, button.dataset.decision);
                return;
            }
            if (button.hasAttribute('data-failed-retry')) {
                void analyze(true);
                return;
            }
            if (button.id === 'apply-paperless') {
                void apply(['folder'], 'paperless');
            } else if (APPLY_ACTIONS.includes(button.dataset.action) || button.dataset.action === 'all') {
                void apply(button.dataset.action === 'all' ? APPLY_ACTIONS : [button.dataset.action]);
            }
        });
        reanalyzeButton.addEventListener('click', () => {
            if (state.view === 'history') void reanalyzeHistorical();
            else void analyze(true);
        });

        const fileIds = (new URLSearchParams(window.location.search).get('fileIds') || '')
            .split(',').map((value) => value.trim()).filter(Boolean);
        clearSelection('Select an Unprocessed file to analyze, or switch to Review, Failed or History.');
        void refreshDashboard();
        // Nextcloud AppAPI checks ADMIN access. A 403 never exposes the Settings tab.
        void api('api/settings').then(() => {
            settingsAvailable = true;
            const button = sidebar.querySelector('[data-view="settings"]');
            if (button) button.hidden = false;
        }).catch((error) => {
            if (error.status !== 403 && error.status !== 401) {
                console.warn('AI Organizer: Settings availability check failed', error);
            }
        });
        if (fileIds.length > 1) {
            setStatus('Select a single file and choose AI Organize. Multiple-file analysis is not supported.', 'error');
        } else if (fileIds.length === 1) {
            state.currentFileId = fileIds[0];
            showPreview(fileIds[0]);
            void analyze(false);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize, {once: true});
    } else {
        initialize();
    }
})();
