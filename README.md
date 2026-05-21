# workspace-classifier

Org-wide Google Workspace inventory pipeline. Scans every active user's Drive, classifies files using an LLM, and exports a single combined CSV. Results are automatically uploaded to S3 and local data is cleaned up after each user.

---

## How it works

For every active user in the org the pipeline runs five phases in order:

| Phase | What it does |
|---|---|
| **A — Scan** | Concurrent Drive metadata walk for all users (parallel threads). Dependency files (`node_modules`, `venv`, `site-packages`, etc.) are filtered out automatically. |
| **B — Classify** | Two-pass LLM classification per user. Pass 1 uses a fast/cheap model on all files; Pass 2 re-runs a full model on any low-confidence results. |
| **C — Download** | Full file download per user *(skipped with `--classify-only`)* |
| **D — Gmail** | Fetch + export emails per user *(skipped with `--classify-only`)* |
| **E — S3 sync** | Upload results to S3 and delete local data. Runs after **every** user in both full and classify-only mode. |

### Resume behaviour

Progress is tracked in `out/run_state.json` per user (`pending → scan_done → done → s3_synced`). Re-running the exact same command resumes from where it stopped — already-completed users are skipped.

---

## Output

```
out/
  org_inventory.csv              ← combined CSV, all users (also uploaded to S3)
  <user_slug>/inventory.csv      ← per-user CSV
  <user_slug>/inventory.xlsx     ← per-user XLSX (uploaded to S3, then deleted locally)
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

---

## Usage

```bash
cd workspace-classifier/

# Classify only (fast — no file downloads, still uploads to S3)
python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --s3-bucket your-s3-bucket \
  --classify-only

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
