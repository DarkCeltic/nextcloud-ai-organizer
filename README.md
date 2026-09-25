# AI Organizer for Nextcloud

**Organize your Nextcloud files with a local AI, without handing the final decision to the model.**

AI Organizer is a self-hosted [Nextcloud](https://nextcloud.com/) external app (ExApp) that uses [Ollama](https://ollama.com/) to read supported files and propose meaningful filenames, destination folders, tags, and optional routing to [Paperless-ngx](https://docs.paperless-ngx.com/). Review and edit each recommendation before applying it. A SQLite-backed history preserves previous decisions and file details so that reanalysis does not erase the past.

> **Project status:** Early development (app metadata: `0.1.0`). This project has been developed and tested in a personal self-hosted environment; it is not presented as a turnkey or production-hardened release. Review recommendations and back up your Nextcloud data and organizer database before using it on important files.

## Highlights

- **Local AI analysis:** Connect to an Ollama server you control. The app uses the file's content, existing Nextcloud folders, and available tags to propose an organization plan with a confidence score and explanation.
- **Human approval:** Preview suggestions before changes are made. Edit a filename or folder, choose suggested tags with checkboxes, and apply individual actions or all applicable actions.
- **Paperless-ngx routing:** Recommend eligible documents for a configurable Paperless consume/inbox folder. For a Paperless recommendation, choose to send it to Paperless or keep it in Nextcloud; Nextcloud-only recommendations use **Apply all**.
- **Routing policy:** Configure document categories that must stay in Nextcloud (`never_send`) and categories preferred for Paperless (`prefer_send`). `never_send` takes precedence. For example, resumes and project files can be kept in Nextcloud even if the model recommends otherwise.
- **Four separate workspace views:** Unprocessed, Review, History, and Failed, with a selected-file detail pane below the active list. Only one queue appears at a time.
- **Preserved history:** Keep previous applied, rejected, ignored, and unavailable-file records, including recorded paths, suggested changes, and decision details. Reanalyzing a previously rejected file creates a new suggestion for Review while retaining its earlier decision in History.
- **Missing-file handling:** If a file is no longer found in Nextcloud, remove its actionable suggestion from Review but preserve its last-known details and an unavailability note in History. Temporary Nextcloud/API failures are not treated as proof of deletion.
- **OCR fallback:** Try normal PDF text extraction first; for PDFs without readable text, use local OCR when enabled. OCR-derived suggestions still require manual review. Files that cannot yield usable text go to Failed for investigation or retry.
- **File-type controls:** Choose supported file types by readable name under **Settings → File Types**; the app maps those choices to the appropriate extensions/MIME types. Spreadsheet `.xlsx` content is extracted for analysis rather than sent as raw binary.

## How it works

```text
Nextcloud Files
      |
      v
Discover supported files in configured scan folders
      |
      v
Extract text (PDF / Office / text / spreadsheet; OCR fallback for PDFs)
      |
      v
Ollama suggests a filename, folder, tags, and Paperless eligibility
      |
      v
Review in the AI Organizer workspace
      |
      +---- Keep in Nextcloud ---> Apply selected changes
      |
      +---- Paperless candidate -> Send to the configured consume folder
      |
      +---- Reject / Ignore -----> Preserve decision in History
      |
      +---- Failure -------------> Failed: inspect, retry, or ignore
```

Analysis produces **suggestions**, not automatic file moves. Applying a recommendation is a separate user action. A Paperless recommendation moves the file to a configured consume folder; Paperless-ngx handles ingestion from there. The ExApp does not replace Paperless's document-management functionality.

## Workspace

| View | Purpose |
| --- | --- |
| **Unprocessed** | Find eligible files that have not yet been analyzed. Select a file and click **Analyze**. |
| **Review** | Open saved recommendations without unnecessarily querying the LLM again; edit or apply them, or reject/ignore them. Reanalyze a changed file before applying an out-of-date recommendation. |
| **History** | Browse completed decisions and archived/unavailable records. Inspect original, last-known, and applied paths where recorded. History is read-only; reanalysis is a separate action that creates a new Review record rather than rewriting an old decision. |
| **Failed** | See analysis failures and retry them. An unreadable scanned PDF may be recoverable through OCR; otherwise it remains available for manual attention. |

The sidebar switches the active view. The right side displays that view's file list, and the selected file's recommendation or record details appear below it. History supports status filtering and pagination.

## Requirements

- A supported Nextcloud installation with **AppAPI** configured to run ExApps. The included `info.xml` currently declares Nextcloud versions **33–34**; compatibility outside that range has not been verified here.
- A deployment environment for the ExApp (the project's Docker/AppAPI setup, or Python for local development).
- A reachable Ollama server with a downloaded model, such as `qwen2.5:7b`.
- A persistent location for the organizer's SQLite database.
- **Optional:** Paperless-ngx and a consume folder accessible through the configured Nextcloud path.
- **Optional:** `ocrmypdf` and its system dependencies for image-only/scanned PDFs. The current base Dockerfile does not install that OCR toolchain yet, so OCR will report a clear Failed-state error until an OCR-capable image is built.

Ollama can run on another host on your LAN. Use an address reachable **from the ExApp container**, not `localhost` unless Ollama actually runs inside that same network namespace. Your files' extracted text is sent to whichever Ollama endpoint you configure, so use a server you trust.

## Installation and configuration

The repository separates **deployment settings** from **organizer behavior**:

- `.env` / container environment: service addresses, user credentials, AppAPI credentials, and the initial Paperless enable/disable choice.
- `config.yaml`: non-secret behavior defaults such as scan paths, timeouts, classifier limits, Paperless category policy, and the SQLite path.
- SQLite-backed Settings UI: administrator changes made after installation. Saved UI values take precedence over the corresponding initial Ollama/Paperless defaults on later restarts.

### 1. Create your private `.env`

Copy `.env.example` to `.env` and fill in your own values. The real `.env` is intentionally excluded by both `.gitignore` and `.dockerignore`.

```env
# Required for a new installation. Neither value has a model/server default.
OLLAMA_URL=http://192.168.1.2:11434
OLLAMA_MODEL=YOUR_INSTALLED_MODEL

# Optional. If omitted, the initial value is false.
PAPERLESS_ENABLED=false

# Current direct WebDAV/OCS client authentication.
NEXTCLOUD_URL=http://192.168.1.2:8080
NEXTCLOUD_USERNAME=YOUR_NEXTCLOUD_USER
NEXTCLOUD_APP_PASSWORD=YOUR_NEXTCLOUD_APP_PASSWORD

# AppAPI identity/secret. APP_SECRET is NOT the user's Nextcloud app password.
# AppAPI normally supplies these for a managed ExApp deployment.
APP_SECRET=YOUR_EXISTING_APPAPI_SECRET
APP_USER=admin
```

`OLLAMA_URL` should be an address reachable **from inside the ExApp container**. `localhost` points back to the AI Organizer container itself, so it is normally incorrect when Ollama runs on another host or container.

The project deliberately does **not** choose an Ollama model for the user. `OLLAMA_MODEL` must name a model that already exists on the configured Ollama server.

### 2. Create non-secret `config.yaml`

For a repository/Compose deployment, copy `config.example.yaml` to `config.yaml`. Do not put passwords, app secrets, or deployment URLs in this file.

```yaml
nextcloud:
  verify_ssl: true
  timeout: 60
  folder_tree_paths:
    - "/"

scanner:
  scan_paths:
    - "/AI Inbox"
  exclude_paths:
    - "/paperless-media"
    - "/inbox"
    - "/Photos"
    - "/AI Ignored"
  allowed_extensions:
    - pdf
    - txt
    - md
    - docx
    - odt
    - rtf
    - log
    - csv
    - json
    - xml
    - xlsx

ollama:
  timeout: 180
  temperature: 0

classifier:
  max_content_chars: 8000
  max_folder_entries: 500
  max_tag_entries: 200
  minimum_confidence: 0.70

paperless:
  inbox_path: "/inbox"
  never_send:
    - resume
    - cv
    - curriculum vitae
    - cover letter
    - portfolio
    - source_code
    - project
    - template
  prefer_send:
    - receipt
    - invoice
    - statement
    - tax
    - insurance
    - contract
    - warranty

database:
  path: "/app/data/python_organizer_local_llm.db"
```

The Docker image copies the sanitized `config.example.yaml` to `/app/config.yaml`, so a clean GitHub/Docker build never depends on the ignored private `config.yaml` file. A manual Compose deployment may mount your own non-secret `config.yaml` over that path.

### 3. Build and test the container

```bash
docker build -t ai-nextcloud-organizer:test .
docker run --rm --env-file .env ai-nextcloud-organizer:test
```

For manual Compose/Portainer deployment, the default mount uses `config.example.yaml`. If you want a customized non-secret file, copy it to `config.yaml` and set `AI_ORGANIZER_CONFIG_FILE=./config.yaml` in `.env`, then run:

```bash
docker compose up -d
```

The Compose file keeps `/app/data` on the `ai_organizer_data` volume. Back up that volume/database before upgrades that change storage behavior or schema.

### Environment variables

All direct environment access is centralized in `python_organizer_local_llm/settings.py`.

| Variable | Purpose | Default |
| --- | --- | --- |
| `OLLAMA_URL` | Ollama server reachable from the ExApp container. Required for a new install unless an existing saved/legacy configuration supplies it. | none |
| `OLLAMA_MODEL` | Ollama model to use. Required for a new install unless already saved/configured. | none |
| `PAPERLESS_ENABLED` | Initial Paperless integration state. The Settings UI can later persist a different value in SQLite. | `false` |
| `NEXTCLOUD_URL` | Nextcloud base URL used by AppAPI calls and the direct Nextcloud client. | none |
| `NEXTCLOUD_USERNAME` | Nextcloud user used by the current direct WebDAV/OCS client. | none |
| `NEXTCLOUD_APP_PASSWORD` | Nextcloud **user app password** for WebDAV/OCS. Keep private. | none |
| `APP_SECRET` | AppAPI shared secret for ExApp-to-AppAPI calls. This is not `NEXTCLOUD_APP_PASSWORD`. | none |
| `APP_USER` | AppAPI registration user used by the current registration request. | `admin` |
| `APP_ID` | ExApp identifier. | `ai_nextcloud_organizer` |
| `APP_VERSION` | ExApp version used by the FastAPI/AppAPI headers. | `0.1.0` |
| `AA_VERSION` | AppAPI protocol header version. | `4.0.0` |
| `AI_ORGANIZER_CONFIG` | Non-secret YAML configuration path inside the container. | `config.yaml` |
| `LOG_LEVEL` | Logging verbosity. `DEBUG` enables debug logging; other values use normal INFO logging. | `INFO` |

For local Python development, install `requirements.txt`, provide the same environment variables, and start:

```bash
uvicorn exapp.main:app --host 0.0.0.0 --port 23000
```

Binding to `0.0.0.0` exposes the development service on reachable interfaces. Use a suitable firewall and do not publish the ExApp directly to the internet.

## Paperless behavior

Paperless integration is **optional**. When enabled, the organizer suggests routing document-like files to your configured consume directory. The routing policy is designed to keep working files such as resumes and source code in Nextcloud and prioritize document categories you designate for Paperless.

For a **Paperless recommendation**, the review UI offers a Paperless action and a **Keep in Nextcloud** alternative, alongside applicable apply controls. For a **Nextcloud-only recommendation**, the main combined action is simply **Apply all**; there is no redundant Keep in Nextcloud button. In either case, review the proposed destination before applying it.

The `never_send` list takes priority over `prefer_send`. Configure and adjust preferred categories in **Settings → Paperless**. The selected policy is not a guarantee that AI classification will always be correct; manual review remains important.

## PDF OCR and file types

For PDFs, AI Organizer first tries to extract embedded text. If none is readable and OCR is enabled, it runs local OCR, subject to the configured page limit (the initial OCR setting uses **10 pages**). OCR-derived suggestions are held for manual review. If extraction and OCR still cannot provide useful text, the failure appears in **Failed** instead of silently treating the file as analyzed.

Use **Settings → File Types** to choose the file formats to process. The app presents human-readable file-type names and applies the corresponding extension/MIME-type rules to scans and manual analysis. The project's supported-format work includes PDF, modern Word documents (`.docx`), text-based formats, and Excel workbooks (`.xlsx`). Legacy Word `.doc` files should not be assumed to have a working extractor merely because a MIME type can appear in file-action registration.

OCR quality depends on the scan, language data, page count, and installed image-processing tools. The LLM may still suggest an incorrect name, folder, or destination.

## How History protects your work

- **Rejected and ignored:** Decisions remain visible after they leave the actionable queues; rejecting or ignoring does not delete the Nextcloud file.
- **Reanalyzed:** Reanalyzing a previously rejected file creates a new suggestion ID and puts the new recommendation in Review. The old rejection stays in History; it is not overwritten.
- **Applied:** History records the recommendation and available actual action details, such as original/applied paths and tags. Fields missing from older records may be shown as not recorded instead of being invented.
- **Unavailable:** When an authoritative Nextcloud lookup cannot find a file, the pending Review item is archived with a detection time and note. A timeout, permission error, or other API failure is **not** treated as a confirmed deletion.

History is an audit trail of the organizer's records, **not** a backup or a way to restore deleted files. Back up your files and the SQLite database separately.

## Privacy and safety

This project is designed for self-hosted Nextcloud and a local/self-hosted Ollama endpoint. It does **not** require a commercial cloud LLM, but document contents are transmitted to your **configured** Ollama server, and Paperless-bound files are transferred to the **configured** consume folder. You control those hosts and network paths.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| AI Organizer does not appear in Nextcloud | ExApp heartbeat, AppAPI registration, enabled state, registered top-menu/file action, and browser cache. |
| Ollama connection fails | The Ollama URL must be reachable from the ExApp host/container; check firewalls and listen address. |
| `.xlsx` file is not offered for analysis | Enable the spreadsheet file type and check MIME registration, extension filtering, and the actual extractor. |
| Scanned PDF reports no readable text | Enable OCR and verify the OCR-capable image was rebuilt with its dependencies; inspect Failed if extraction still fails. |
| Review fails to verify files | Check Nextcloud/WebDAV connectivity and permissions. Do not archive files merely because the lookup errored. |
| Suggestion seems outdated after a file changed | Re-analyze to create a current suggestion rather than applying one based on the previous file version. |
| A Paperless candidate should stay in Nextcloud | Select **Keep in Nextcloud** and review your `never_send`/`prefer_send` policies. |

## Development and contributions

This project was developed using OpenAI's ChatGPT for code generation and debugging, with requirements, design choices, integration, and testing directed by the maintainer. AI-generated suggestions in the **application** are likewise intended to be reviewed by a human before changes are applied.

Issues and pull requests are welcome. When reporting a problem, include a description of the behavior, the relevant version, and **redacted** logs or configuration. Never include API secrets, app passwords, document contents, or a copy of your live organizer database in public issues.

## License

**GNU Affero General Public License v3.0 or later (`AGPL-3.0-or-later`).** See the repository's `LICENSE.md` file for the full license text. Add the full license file to the repository before publication if it is not already present. Third-party dependencies and any reused third-party code retain their respective licenses.

This is an independent project and is not an official Nextcloud, Ollama, or Paperless-ngx product.
