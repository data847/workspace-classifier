# workspace-classifier

Org-wide Google Workspace inventory pipeline. Scans every active user's Drive, classifies files using an LLM, and exports a single combined CSV. Results can be uploaded to S3 with local cleanup, or kept entirely local with `--local-only`.

---

## How it works

For every active user in the org the pipeline runs five phases in order:

| Phase | What it does |
|---|---|
| **A — Scan** | Concurrent Drive metadata walk for all users (parallel threads). Dependency files (`node_modules`, `venv`, `site-packages`, etc.) are filtered out automatically. |
| **B — Classify** | Two-pass LLM classification per user. Pass 1 uses a fast/cheap model on all files; Pass 2 re-runs a full model on any low-confidence results. |
| **C — Download** | Full file download per user *(skipped with `--classify-only`)* |
| **D — Gmail** | Fetch + export emails per user *(skipped with `--classify-only`)* |
| **E — Output finalize** | Upload results to S3 and delete local data, or keep user outputs locally when running with `--local-only` / no S3 bucket. |

### Resume behaviour

Progress is tracked in `out/run_state.json` per user (`pending → scan_done → done → s3_synced` for S3 runs, or `local_done` for local-only runs). Re-running the exact same command resumes from where it stopped — already-completed users are skipped.

---

## Output

```
out/
  org_inventory.csv              ← combined CSV, all users (also uploaded to S3 unless local-only)
  <user_slug>/inventory.csv      ← per-user CSV
  <user_slug>/inventory.xlsx     ← per-user XLSX (uploaded to S3, then deleted locally unless local-only)
  run_state.json                 ← resume state
```

### CSV columns

`user_email` · `Item Name` · `File Type` · `Content Summary` · `PII Flag` · `Size` · `Word Count` · `LLM Tokens` · `Page Count` · `Modality` · `Content Type` · `Quality tier` · `What is contained` · `Source` · `Path / Subsection` · `bucket_number` · `bucket_name` · `Sub-category` · `confidence` · `rationale`

---

## Setup

### 1. Prerequisites

- Python 3.10+
- A Google Cloud **service account** with **Domain-Wide Delegation** enabled and the following scopes granted in Google Admin:
  - `https://www.googleapis.com/auth/drive.readonly`
  - `https://www.googleapis.com/auth/admin.directory.user.readonly`
  - `https://www.googleapis.com/auth/admin.reports.usage.readonly`
  - `https://www.googleapis.com/auth/gmail.readonly` *(only needed for full mode)*
- An Anthropic or OpenAI API key
- AWS credentials with `s3:PutObject` access *(only needed for `--s3-bucket`)*

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```env
# LLM — pick one
ANTHROPIC_API_KEY=sk-ant-...
# or
OPENAI_API_KEY=sk-proj-...
LLM_PROVIDER=openai

# Google service account (place the JSON file in this directory)
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json

# AWS (only needed for --s3-bucket)
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
```

### 4. Add service account key

Place your service account JSON file in the `workspace-classifier/` directory and name it `service_account.json` (or set `GOOGLE_SERVICE_ACCOUNT_FILE` in `.env` to its path).

### 5. Generate and authorize a service account

This project uses a Google service account with Domain-Wide Delegation (DWD). The service account JSON key is used locally to mint runtime OAuth access tokens while impersonating users in your Google Workspace domain.

#### Required Google Cloud APIs

Enable these APIs in the Google Cloud project that owns the service account:

- Google Drive API
- Admin SDK API
- Gmail API *(only needed for full mode / Gmail extraction)*

#### Required Google Cloud permissions

The person setting this up needs enough Google Cloud IAM permissions to enable APIs, create service accounts, and create service account keys. Typical roles are:

- `Service Usage Admin`
- `Service Account Admin`
- `Service Account Key Admin`

A project `Owner` can also perform these steps.

#### Required Google Workspace permissions

The person authorizing Domain-Wide Delegation in Google Admin Console must be a Google Workspace Super Admin.

The `--admin` account used when running the tool should be a Super Admin or delegated admin with read access to users and reports.

#### Service account setup steps

1. In Google Cloud Console, create or select the project for this tool.
2. Enable the required APIs listed above.
3. Go to `IAM & Admin > Service Accounts`.
4. Create a service account, for example `workspace-classifier`.
5. Open the service account details and enable Domain-Wide Delegation.
6. Copy the service account OAuth Client ID.
7. Go to `Keys > Add Key > Create new key > JSON`.
8. Download the JSON key and save it securely as `service_account.json`, or set `GOOGLE_SERVICE_ACCOUNT_FILE` to its path.

#### Domain-Wide Delegation authorization

In Google Admin Console, go to:

```text
Security > Access and data control > API controls > Domain-wide delegation
```

Add a new API client using the service account OAuth Client ID, then authorize these scopes:

```text
https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/admin.directory.user.readonly,https://www.googleapis.com/auth/admin.reports.usage.readonly,https://www.googleapis.com/auth/gmail.readonly
```

If Gmail extraction is not required, omit:

```text
https://www.googleapis.com/auth/gmail.readonly
```

#### Security notes

Do not commit these files to git:

```text
service_account.json
credentials.json
.env
tokens
```

Rotate the service account key if it is exposed, and keep the authorized scope list as small as your workflow allows.

---

## Usage

```bash
cd workspace-classifier/

# Classify only (fast — no file downloads, still uploads to S3)
python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --s3-bucket your-s3-bucket \
  --classify-only

# Local-only mode — no S3 upload, keeps per-user output directories
python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --local-only

# Full mode (classify + download files + fetch Gmail)
python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --s3-bucket your-s3-bucket

# Dry run — list users only, no processing
python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --dry-run

# Resume a stopped run — just re-run the exact same command
python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --s3-bucket your-s3-bucket \
  --classify-only
```

### All flags

| Flag | Default | Description |
|---|---|---|
| `--admin EMAIL` | *(required)* | Google Workspace admin email used for user listing |
| `--s3-bucket NAME` | *(optional)* | S3 bucket name. Omit to skip S3 upload |
| `--local-only` | off | Explicitly keep outputs local; cannot be combined with `--s3-bucket` |
| `--classify-only` | off | Skip file downloads and Gmail; classify metadata only |
| `--skip EMAIL` | — | Skip a specific user (repeatable) |
| `--since-days N` | 0 (all time) | Only process files modified in the last N days |
| `--modified-after YYYY-MM-DD` | — | Only process files modified after this date |
| `--extract-workers N` | 8 | Parallel threads for snippet extraction / LLM batching per user |
| `--scan-workers N` | 4 | Parallel threads for Drive metadata scan phase |
| `--max-files N` | 0 (unlimited) | Cap Drive file count per user |
| `--snippet-bytes N` | 2048 | Bytes downloaded per file for LLM classification |
| `--out DIR` | `./out` | Output directory |
| `--sa-file PATH` | `service_account.json` | Path to service account JSON |
| `--dry-run` | off | List users only — do not process |

---

## Project structure

```
workspace-classifier/
├── run_org_classify.py       ← main entry point
├── requirements.txt
├── .env.example              ← environment variable template
│
├── gdrive/
│   ├── credentials.py        ← DWD service account auth + user listing
│   ├── scan.py               ← Drive folder walker
│   ├── fetch.py              ← file download helpers
│   └── pipeline_1tb.py       ← parallel extract + two-pass LLM classify
│
├── extractors/
│   ├── core.py               ← snippet extraction (PDF, DOCX, PPTX, images, …)
│   ├── content_type.py
│   ├── content_inventory.py
│   ├── modality.py
│   └── quality_tier.py
│
├── dump/
│   ├── full_download.py      ← full file download by bucket
│   └── sample_selector.py    ← sample-based download
│
├── gmail/
│   └── fetch.py              ← Gmail export
│
├── llm_provider.py           ← Anthropic / OpenAI abstraction
├── segmenter.py              ← LLM prompt batching
├── prompt.py                 ← classification prompts
├── pii.py                    ← PII detection
├── ingest.py                 ← file ingestion helpers
├── checkpoint.py             ← incremental save helpers
├── inventory_writer.py       ← XLSX / CSV writer
├── excel_io.py               ← evidence DataFrame formatter
├── artifact_summary.py
├── subcategory_classifier.py
├── subcategory_taxonomy.py
├── s3_sync.py                ← S3 upload + local cleanup
└── env_loader.py             ← loads .env from project root
```
