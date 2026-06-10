# workspace-classifier

**Repository:** https://github.com/data847/workspace-classifier

Google Workspace data pipeline for org-wide or single-user runs. It can:

- **Classify** every user's Drive files with an LLM and export inventory CSVs
- **Export** raw Drive files and Gmail to S3 (no LLM)
- **Upload** results to S3 with optional local cleanup

---

## Modes at a glance

| Mode | Command | Classify? | Download Drive? | Gmail? | S3? | LLM key needed? |
|------|---------|-----------|-----------------|--------|-----|-----------------|
| **Full classify** | `--s3-bucket` | Yes | Yes (classified files) | Yes | Yes | Yes |
| **Export only** | `--export-only --s3-bucket` | No | Yes (all scanned files) | Yes | Yes | No |
| **Classify only** | `--classify-only --s3-bucket` | Yes | No | No | Yes (metadata only) | Yes |
| **Local** | `--local-only` | Yes | Yes | With `--gmail` / `--user` | No | Yes |

**Most common for data migration (Drive + email → S3, no classification):**

```bash
python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --user user@yourdomain.com \
  --export-only \
  --s3-bucket your-s3-bucket
```

---

## How it works

### Full classify mode (default with `--s3-bucket`)

For every active user in the org:

| Phase | What it does |
|---|---|
| **A — Scan** | Concurrent Drive metadata walk (parallel threads). Dependency folders (`node_modules`, `venv`, etc.) are filtered out. |
| **B — Classify** | Two-pass LLM classification. Pass 1 uses a fast model; Pass 2 re-runs a full model on low-confidence results. |
| **C — Download** | Full Drive file download, organised by LLM bucket. |
| **D — Gmail** | Fetch emails, attachments, and metadata. Auto-enabled when `--s3-bucket` or `--user` is set. |
| **E — S3 sync** | Upload user output to S3, then delete local copies. |

### Export-only mode (`--export-only`)

Skips LLM classification entirely. For each user:

1. **Scan** Drive metadata
2. **Download** all scanned Drive files → `dump/files/` (preserves folder paths)
3. **Fetch** Gmail → `dump/emails/`
4. **Upload** to S3 and delete local data

No `inventory.csv`, no `org_inventory.csv`, no Anthropic/OpenAI API key required.

### Resume behaviour

Progress is tracked in `out/run_state.json` per user:

```text
pending → scan_done → done → s3_synced   (S3 runs)
pending → scan_done → done → local_done  (local-only runs)
```

Re-running the exact same command resumes from where it stopped — already-completed users are skipped.

---

## Output

### Classify mode

```text
out/
  org_inventory.csv              ← combined CSV, all users
  workspace_file_count.json      ← total file count + per-user counts
  workspace_storage_size.json    ← storage usage (from --size-only)
  <user_slug>/inventory.csv      ← per-user classified inventory
  <user_slug>/inventory.xlsx     ← per-user XLSX
  <user_slug>/dump/files/        ← Drive downloads (by LLM bucket)
  <user_slug>/dump/emails/       ← Gmail exports
  run_state.json                 ← resume state
```

### Export-only mode

```text
out/
  workspace_file_count.json      ← file counts
  <user_slug>/dump/files/        ← all Drive files (by folder path)
  <user_slug>/dump/emails/       ← Gmail exports
    emails_all.json              ← all messages with full body text
    emails_metadata.csv          ← id, subject, sender, date, attachments
    email_<n>.txt                ← individual email bodies
    attachments/<msg_id>/        ← raw attachment files
  run_state.json
```

### S3 layout

When `--s3-bucket` is set, the S3 prefix is auto-generated as `<org>_<YYYY-MM-DD>`:

```text
s3://<bucket>/<org>_<date>/org_inventory.csv          ← classify mode only
s3://<bucket>/<org>_<date>/workspace_file_count.json
s3://<bucket>/<org>_<date>/<user_slug>/inventory.csv  ← classify mode only
s3://<bucket>/<org>_<date>/<user_slug>/dump/files/...
s3://<bucket>/<org>_<date>/<user_slug>/dump/emails/...
```

Local user directories are deleted after a successful S3 upload. Use `--local-only` to keep files on disk.

### CSV columns (classify mode)

`user_email` · `Item Name` · `File Type` · `Content Summary` · `PII Flag` · `Size` · `Word Count` · `LLM Tokens` · `Page Count` · `Modality` · `Content Type` · `Quality tier` · `What is contained` · `Source` · `Path / Subsection` · `bucket_number` · `bucket_name` · `Sub-category` · `confidence` · `rationale`

---

## Setup

### 1. Prerequisites

- Python 3.10+
- Google Cloud **service account** with **Domain-Wide Delegation** and these Admin-authorized scopes:
  - `https://www.googleapis.com/auth/drive.readonly` *(required)*
  - `https://www.googleapis.com/auth/gmail.readonly` *(Gmail export)*
  - `https://www.googleapis.com/auth/admin.directory.user.readonly` *(org-wide runs; optional for `--user` without `--verify-user`)*
  - `https://www.googleapis.com/auth/admin.reports.usage.readonly` *(`--size-only` only)*
- Anthropic or OpenAI API key *(classify mode only — not needed for `--export-only`)*
- AWS credentials with `s3:PutObject` access *(needed for `--s3-bucket`)*

### 2. Clone and install

```bash
git clone https://github.com/data847/workspace-classifier.git
cd workspace-classifier
pip install -r requirements.txt
```

For **export-only** runs (`--export-only`), you only need the Google + AWS + core packages. LLM and file-extractor packages are required for classify mode.

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
# LLM — only needed for classify mode (not --export-only)
ANTHROPIC_API_KEY=sk-ant-...
# or
OPENAI_API_KEY=sk-proj-...
LLM_PROVIDER=openai

# Google service account
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json

# AWS — only needed for --s3-bucket
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
```

### 4. Service account setup

1. Enable **Google Drive API**, **Admin SDK API**, and **Gmail API** in Google Cloud Console.
2. Create a service account with **Domain-Wide Delegation** enabled.
3. Download the JSON key as `service_account.json`.
4. In Google Admin Console → **Security > API controls > Domain-wide delegation**, authorize the service account Client ID with the scopes listed above.

The `--admin` account must be a Workspace Super Admin (or delegated admin with user/reports read access).

#### Security

Do not commit these files:

```text
service_account.json
credentials.json
.env
tokens
```

---

## Usage

```bash
cd workspace-classifier/

# ── Export only: Drive + Gmail → S3 (no LLM) ──────────────────────────────

# Single user
python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --user user@yourdomain.com \
  --export-only \
  --s3-bucket your-s3-bucket

# Whole org
python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --export-only \
  --s3-bucket your-s3-bucket

# Last 30 days only
python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --user user@yourdomain.com \
  --export-only \
  --s3-bucket your-s3-bucket \
  --since-days 30

# ── Full classify: LLM + Drive + Gmail → S3 ──────────────────────────────

python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --s3-bucket your-s3-bucket

# Single user with classification
python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --user user@yourdomain.com \
  --s3-bucket your-s3-bucket

# ── Classify only (metadata, no file downloads) ──────────────────────────

python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --s3-bucket your-s3-bucket \
  --classify-only

# ── Local output (no S3) ─────────────────────────────────────────────────

python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --user user@yourdomain.com \
  --export-only \
  --local-only

# ── Utility commands ─────────────────────────────────────────────────────

# Count Drive files across the org, then exit
python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --count-files-only

# Fetch total Workspace storage size, then exit
python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --size-only

# List users without processing
python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --dry-run

# Resume a stopped run — re-run the exact same command
python3 run_org_classify.py \
  --admin admin@yourdomain.com \
  --user user@yourdomain.com \
  --export-only \
  --s3-bucket your-s3-bucket
```

### All flags

| Flag | Default | Description |
|---|---|---|
| `--admin EMAIL` | *(required)* | Workspace admin email for user listing / DWD |
| `--user EMAIL` | — | Process one user only; auto-enables Gmail (no Admin SDK lookup) |
| `--verify-user` | off | With `--user`, validate account via Admin SDK |
| `--s3-bucket NAME` | — | S3 bucket name; auto-enables Gmail and triggers upload + local cleanup |
| `--s3-prefix PREFIX` | auto | Override S3 key prefix (default: `<org>_<YYYY-MM-DD>`) |
| `--export-only` | off | Download Drive + Gmail to S3; skip LLM classification |
| `--classify-only` | off | Classify metadata only; skip downloads and Gmail |
| `--local-only` | off | Keep outputs local; no S3 upload (cannot combine with `--s3-bucket`) |
| `--gmail` | off | Fetch Gmail (auto-enabled with `--s3-bucket`, `--user`, or `--export-only`) |
| `--no-gmail` | off | Skip Gmail even when it would otherwise be enabled |
| `--since-days N` | 0 (all time) | Only process files/emails from the last N days |
| `--modified-after YYYY-MM-DD` | — | Only process files/emails modified after this date |
| `--skip EMAIL` | — | Skip a specific user (repeatable) |
| `--only EMAIL` | — | Process only this user (repeatable; use `--user` for single-user lookup) |
| `--max-files N` | 0 (unlimited) | Cap Drive file count per user |
| `--scan-workers N` | 4 | Parallel threads for Drive metadata scan |
| `--extract-workers N` | 8 | Parallel threads for LLM snippet extraction (classify mode) |
| `--snippet-bytes N` | 2048 | Bytes per file for LLM classification snippets |
| `--pass1-model MODEL` | default | Fast model for classify pass 1 |
| `--pass2-model MODEL` | default | Full model for classify pass 2 (low-confidence re-run) |
| `--count-files-only` | off | Count Drive files, write `workspace_file_count.json`, exit |
| `--size-only` | off | Fetch storage usage, write `workspace_storage_size.json`, exit |
| `--out DIR` | `./out` | Output directory |
| `--sa-file PATH` | `service_account.json` | Path to service account JSON |
| `--dry-run` | off | List users only — do not process |

---

## Project structure

```text
workspace-classifier/
├── run_org_classify.py       ← main entry point
├── s3_sync.py                ← S3 upload + local cleanup
├── requirements.txt
├── .env.example
│
├── gdrive/
│   ├── credentials.py        ← DWD auth + org/single-user lookup
│   ├── scan.py               ← Drive folder walker
│   ├── fetch.py              ← snippet download helpers
│   └── pipeline_1tb.py       ← parallel extract + two-pass LLM classify
│
├── dump/
│   ├── full_download.py      ← Drive download (by bucket or by scan path)
│   └── sample_selector.py    ← sample-based download
│
├── gmail/
│   └── fetch.py              ← Gmail + attachment export
│
├── extractors/               ← file parsers (PDF, DOCX, PPTX, …)
├── llm_provider.py             ← Anthropic / OpenAI abstraction
├── segmenter.py                ← LLM prompt batching
├── checkpoint.py               ← incremental save helpers
├── inventory_writer.py         ← XLSX / CSV writer
└── env_loader.py               ← loads .env from project root
```
