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
- **Optional:** The OCR dependencies included in the OCR-enabled Docker build, for image-only/scanned PDFs.

Ollama can run on another host on your LAN. Use an address reachable **from the ExApp container**, not `localhost` unless Ollama actually runs inside that same network namespace. Your files' extracted text is sent to whichever Ollama endpoint you configure, so use a server you trust.

## Installation and configuration

> This is a configuration guide, not a one-command installer. ExApp registration and networking depend on your Nextcloud AppAPI deployment. Use the Docker/AppAPI deployment files from this repository and the instructions appropriate to your deployment method. The OCR-enabled release requires rebuilding the image to include its system dependencies; replacing Python files alone is not sufficient.

1. Clone the repository and configure your Nextcloud AppAPI deployment to run the ExApp. Give the container network access to Nextcloud and Ollama.
2. Create a private `config.yaml` using the sanitized example in this README, then set your real Nextcloud address, username, **app password**, scan paths, and Ollama address. Never commit the populated file.
3. Configure the AppAPI-provided ExApp secret and Nextcloud URL using private environment settings. Do not invent an `APP_SECRET` or commit it to Git.
4. Mount or otherwise persist the directory containing the configured SQLite database. Back up an existing database before upgrading versions that change its schema.
5. Build/deploy the current ExApp image, register/enable it through AppAPI, and confirm its heartbeat succeeds. If you are using the OCR version, rebuild the image so its OCR dependencies are installed.
6. Open **AI Organizer** in Nextcloud. Configure **Settings → Paperless**, **Settings → OCR**, and **Settings → File Types**, then try a noncritical document in your scan folder before applying recommendations to important files.

### Example configuration (`config.yaml`)

The addresses and paths below are examples, **not** values for your own environment. Use this example to create `config.yaml` locally and populate your private credentials.

```yaml
nextcloud:
  url: "http://192.168.1.2:8080/"
  username: "YOUR_NEXTCLOUD_USER"
  app_password: "REPLACE_WITH_NEXTCLOUD_APP_PASSWORD"
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
  url: "http://192.168.1.3:11434"
  model: "qwen2.5:7b"
  timeout: 180
  temperature: 0

classifier:
  max_content_chars: 8000
  max_folder_entries: 500
  max_tag_entries: 200
  minimum_confidence: 0.70

paperless:
  enabled: true
  inbox_path: "/inbox"
  never_send:
    - resume
    - cv
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
  path: "./data/organizer.db"
```

The example reflects the project's YAML-based connection and scan configuration. Settings saved through the ExApp's UI, including newer file-type and routing preferences, are stored in SQLite; do not assume changing this example alone replaces choices already saved through the UI. The example does not include real credentials.

### ExApp environment variables

The existing FastAPI entry point reads the following settings:

| Variable | Purpose |
| --- | --- |
| `NEXTCLOUD_URL` | Base URL used by the ExApp to communicate with Nextcloud/AppAPI; required by the current entry point. |
| `APP_SECRET` | Secret provided for ExApp–AppAPI communication; keep private. |
| `APP_ID` | ExApp identifier; current default: `ai_nextcloud_organizer`. |
| `APP_VERSION` | App version; current default: `0.1.0`. |
| `AA_VERSION` | AppAPI protocol version declared by the entry point; current default: `4.0.0`. |
| `APP_USER` | Registration user; current default: `admin`. |
| `AI_ORGANIZER_CONFIG` | Path to your private YAML config; default: `config.yaml`. |
| `LOG_LEVEL` | Logging verbosity, for example `INFO` or `DEBUG`. |

For local Python development, install the dependencies from the repository's requirements file, provide the same config and environment settings, and start the FastAPI app with:

```bash
uvicorn exapp.main:app --host 0.0.0.0 --port 23001
```

This launches the development service; Nextcloud integration still requires working AppAPI registration and networking. Binding to `0.0.0.0` exposes the development service on reachable network interfaces: use a suitable firewall and do not publish it directly to the internet.

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

**GNU Affero General Public License v3.0 or later (`AGPL-3.0-or-later`).** See the repository's `LICENSE` file for the full license text. Add the full license file to the repository before publication if it is not already present. Third-party dependencies and any reused third-party code retain their respective licenses.

This is an independent project and is not an official Nextcloud, Ollama, or Paperless-ngx product.
