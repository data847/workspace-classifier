#!/usr/bin/env python3
"""Org-wide Google Workspace inventory pipeline.

For every active user in the org:
  Phase A  Concurrent Drive metadata scan  (all users, parallel threads)
  Phase B  Per-user LLM classify            (pass-1 fast + pass-2 low-conf re-run)
  Phase C  Per-user full file download      (skipped with --classify-only)
  Phase D  Per-user Gmail fetch             (skipped with --classify-only)
  Phase E  S3 upload + local delete         (runs after EVERY user when S3 is enabled)

Output
------
  out/org_inventory.csv          Combined CSV — all users, all classified files
  out/workspace_file_count.json  Total Workspace file count + per-user counts
  out/<user_slug>/inventory.csv  Per-user CSV (also uploaded to S3)
  out/<user_slug>/inventory.xlsx Per-user XLSX (uploaded to S3, then deleted)

CSV columns
-----------
  user_email | Item Name | File Type | Content Summary | PII Flag | Size
  Word Count | LLM Tokens | Page Count | Modality | Content Type | Quality tier
  What is contained | Source | Path / Subsection | bucket_number | bucket_name
  Sub-category | confidence | rationale

Resume behaviour
----------------
  Progress is tracked in out/run_state.json per user:
    pending → scan_done → done → s3_synced/local_done  (or error)
  Re-running the same command resumes from the last saved state.

Usage
-----
  python run_org_classify.py --admin admin@company.com --s3-bucket my-bucket

  # Classify only (no file downloads) — still uploads to S3 unless --local-only is used
  python run_org_classify.py --admin admin@company.com --s3-bucket my-bucket --classify-only

  # Keep outputs local, no S3 upload/delete
  python run_org_classify.py --admin admin@company.com --local-only

  # Count files directly from the Drive API, then exit
  python run_org_classify.py --admin admin@company.com --count-files-only

  # Last 30 days
  python run_org_classify.py --admin admin@company.com --s3-bucket my-bucket --since-days 30

  # Dry run: list users, don't process
  python run_org_classify.py --admin admin@company.com --dry-run

  # Resume a stopped run — re-run the exact same command
  python run_org_classify.py --admin admin@company.com --s3-bucket my-bucket
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Project root (all modules are local — no external lib path needed)
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent

import env_loader  # noqa: F401  loads .env from _ROOT

# ---------------------------------------------------------------------------
# Library imports (all local to workspace-classifier/)
# ---------------------------------------------------------------------------

from gdrive.credentials import (
    build_drive_service_dwd,
    get_dwd_credentials,
    default_sa_path,
    list_org_users,
)
from gdrive.scan import walk_drive_folder
from gdrive.pipeline_1tb import build_inventory_from_drive_1tb
from excel_io import evidence_display_dataframe
from llm_provider import default_llm_model
from s3_sync import S3Syncer
from checkpoint import (
    checkpoint_path_for_output,
    drive_artifact_checkpoint_path,
    load_checkpoint,
    load_drive_artifact_checkpoint,
)
from subcategory_classifier import subcategory_checkpoint_path

try:
    from dump.full_download import download_all_by_bucket
    from dump.sample_selector import select_and_download_samples
    _DOWNLOAD_AVAILABLE = True
except ImportError:
    _DOWNLOAD_AVAILABLE = False

try:
    from gmail.fetch import build_gmail_service, fetch_and_export_emails
    _GMAIL_AVAILABLE = True
except ImportError:
    _GMAIL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def _banner(title: str, width: int = 60) -> None:
    """Print a clearly visible section header."""
    bar = "─" * width
    _log(bar)
    _log(f"  {title}")
    _log(bar)


def _phase_log(pfx: str, phase: str, msg: str) -> None:
    """Structured log line: prefix + phase tag + message."""
    _log(f"{pfx} [{phase}] {msg}")


def _elapsed(t0: float) -> str:
    s = time.time() - t0
    if s < 60:
        return f"{s:.1f}s"
    return f"{s / 60:.1f}min"


# ---------------------------------------------------------------------------
# Run-state checkpoint
# ---------------------------------------------------------------------------

class RunState:
    PENDING   = "pending"
    SCAN_DONE = "scan_done"   # Drive metadata scan complete, rows cached
    DONE      = "done"        # classify (+ download) complete, ready for S3/local finalize
    S3_SYNCED = "s3_synced"   # uploaded to S3 and local data deleted
    LOCAL_DONE = "local_done" # complete with local output retained
    ERROR     = "error"

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict] = {}
        if path.is_file():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
                _log(f"[state] loaded run state ({len(self._data)} users)")
            except Exception as e:
                _log(f"[state] warn: could not load state ({e}), starting fresh")

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def ensure_users(self, users: list[dict]) -> None:
        changed = False
        for u in users:
            email = u["email"]
            if email not in self._data:
                self._data[email] = {
                    "status": self.PENDING,
                    "name": u.get("name", ""),
                    "org_unit": u.get("org_unit", "/"),
                }
                changed = True
        if changed:
            self._save()

    def set(self, email: str, status: str, *, error: str = "") -> None:
        entry = self._data.setdefault(email, {})
        entry["status"] = status
        ts = datetime.utcnow().isoformat() + "Z"
        if status in (self.DONE, self.ERROR, self.S3_SYNCED, self.SCAN_DONE, self.LOCAL_DONE):
            entry["updated_at"] = ts
        if error:
            entry["error"] = error
        self._save()

    def set_scan_counts(self, email: str, *, file_count: int, total_rows: int) -> None:
        entry = self._data.setdefault(email, {})
        entry["file_count"] = int(file_count)
        entry["folder_count"] = max(0, int(total_rows) - int(file_count))
        entry["total_rows"] = int(total_rows)
        entry["counts_updated_at"] = datetime.utcnow().isoformat() + "Z"
        self._save()

    def scan_counts(self, email: str) -> dict[str, int] | None:
        entry = self._data.get(email, {})
        try:
            return {
                "file_count": int(entry["file_count"]),
                "folder_count": int(entry.get("folder_count", 0)),
                "total_rows": int(entry.get("total_rows", entry["file_count"])),
            }
        except Exception:
            return None

    def get_status(self, email: str) -> str:
        return self._data.get(email, {}).get("status", self.PENDING)

    def is_complete(self, email: str, *, local_only: bool = False) -> bool:
        status = self.get_status(email)
        if local_only:
            return status in (self.DONE, self.LOCAL_DONE, self.S3_SYNCED)
        return status == self.S3_SYNCED

    def needs_s3_sync(self, email: str) -> bool:
        return self.get_status(email) in (self.DONE, self.LOCAL_DONE)

    def scan_already_done(self, email: str) -> bool:
        return self.get_status(email) in (self.SCAN_DONE, self.DONE, self.S3_SYNCED, self.LOCAL_DONE)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for v in self._data.values():
            s = v.get("status", "?")
            counts[s] = counts.get(s, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_dir(out_dir: Path, email: str) -> Path:
    return out_dir / email.replace("@", "_at_").replace(".", "_")


def _scan_cache_path(user_dir: Path) -> Path:
    return user_dir / "scan_rows.jsonl"


def _load_scan_rows(user_dir: Path) -> list[dict[str, Any]]:
    cache = _scan_cache_path(user_dir)
    if not cache.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in cache.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _save_scan_rows(user_dir: Path, rows: list[dict[str, Any]]) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    cache = _scan_cache_path(user_dir)
    tmp = cache.with_suffix(cache.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(cache)


def _scan_file_count(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if not row.get("is_folder"))


def _record_scan_counts(state: RunState, email: str, rows: list[dict[str, Any]]) -> None:
    state.set_scan_counts(email, file_count=_scan_file_count(rows), total_rows=len(rows))


def _write_workspace_file_count(
    out_dir: Path,
    users: list[dict],
    state: RunState,
) -> Path:
    """Write total Workspace file counts gathered from scan state/caches."""
    csv_counts: dict[str, int] = {}
    combined_csv = out_dir / "org_inventory.csv"
    if combined_csv.is_file():
        try:
            with combined_csv.open("r", newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    email = str(row.get("user_email") or "").strip()
                    if email:
                        csv_counts[email] = csv_counts.get(email, 0) + 1
        except Exception:
            csv_counts = {}

    per_user: list[dict[str, Any]] = []
    missing: list[str] = []
    total_files = 0
    total_folders = 0
    total_rows = 0

    for u in users:
        email = u["email"]
        counts = state.scan_counts(email)
        if counts is None:
            rows = _load_scan_rows(_user_dir(out_dir, email))
            if rows:
                _record_scan_counts(state, email, rows)
                counts = state.scan_counts(email)
        source = "scan"
        if counts is None and email in csv_counts:
            counts = {
                "file_count": int(csv_counts[email]),
                "folder_count": 0,
                "total_rows": int(csv_counts[email]),
            }
            source = "org_inventory.csv"
        if counts is None:
            missing.append(email)
            continue

        file_count = int(counts["file_count"])
        folder_count = int(counts["folder_count"])
        row_count = int(counts["total_rows"])
        total_files += file_count
        total_folders += folder_count
        total_rows += row_count
        per_user.append({
            "email": email,
            "name": u.get("name", ""),
            "org_unit": u.get("org_unit", "/"),
            "file_count": file_count,
            "folder_count": folder_count,
            "total_rows": row_count,
            "source": source,
        })

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "total_files": total_files,
        "total_folders": total_folders,
        "total_rows": total_rows,
        "counted_users": len(per_user),
        "missing_users": missing,
        "per_user": per_user,
    }
    out_path = out_dir / "workspace_file_count.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Dependency-path filter
# ---------------------------------------------------------------------------

# Directories that are unambiguously dependency/build caches — safe to always skip.
_DEFINITE_DEP_SEGMENTS: frozenset[str] = frozenset({
    # Python
    "site-packages",
    "dist-packages",
    "__pycache__",
    ".eggs",
    # Node / JS / TS
    "node_modules",
    "bower_components",
    # Java / Kotlin (Maven local repo)
    ".m2",
    # iOS (CocoaPods)
    "Pods",
    # Python compiled cache
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    # JS build caches
    ".next",
    ".nuxt",
    ".turbo",
    ".parcel-cache",
})

# Directories that are dependency-like ONLY when paired with a build-artifact extension.
_MAYBE_DEP_SEGMENTS: frozenset[str] = frozenset({
    "venv",
    ".venv",
    "env",          # common Python virtualenv name
    "target",       # Java/Rust build output
    "vendor",       # Go modules / PHP Composer (but also legitimate "vendor" folders)
    ".bundle",      # Ruby Bundler
    "Carthage",     # iOS Carthage
})

# Extensions that only appear inside compiled/dependency trees, never in real documents.
_DEP_EXTENSIONS: frozenset[str] = frozenset({
    ".pyc", ".pyo", ".pyd",          # Python bytecode
    ".class",                         # Java bytecode
    ".jar", ".war", ".ear",           # Java archives
    ".o", ".a", ".so", ".dylib",      # C/C++ compiled objects
    ".dll", ".pdb", ".exp", ".lib",   # Windows compiled objects
    ".csi",                           # .NET build artefact (also seen in terminal output)
})


def _is_dependency_path(path: str, extension: str) -> bool:
    """Return True if this file lives inside a language dependency / build directory.

    Uses a two-tier approach:
    - Definite segments: always skip (node_modules, site-packages, __pycache__, …)
    - Maybe segments: only skip when the file extension is a compiled build artefact
      (venv, target, vendor, …) — avoids false-positives on legitimate business folders.
    """
    parts = frozenset(path.replace("\\", "/").split("/"))
    if parts & _DEFINITE_DEP_SEGMENTS:
        return True
    if (parts & _MAYBE_DEP_SEGMENTS) and (("." + extension.lstrip(".")) in _DEP_EXTENSIONS or extension in _DEP_EXTENSIONS):
        return True
    return False


def _filter_dependency_rows(rows: list[dict[str, Any]], log_prefix: str) -> list[dict[str, Any]]:
    """Remove dependency/build-cache files from scan rows before classification."""
    kept: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        path = str(row.get("path") or row.get("name") or "")
        ext  = ("." + str(row.get("extension") or "")).lstrip(".")
        if not ext:
            # derive from filename
            ext = Path(str(row.get("name") or "")).suffix.lstrip(".")
        if _is_dependency_path(path, ext):
            skipped += 1
        else:
            kept.append(row)
    if skipped:
        _log(f"{log_prefix} [scan] skipped {skipped} dependency/build-cache files")
    return kept


# ---------------------------------------------------------------------------
# Phase A: Concurrent Drive metadata scan
# ---------------------------------------------------------------------------

_SCAN_MAX_ATTEMPTS = 5
_SCAN_RETRY_ERRORS = ("timed out", "timeout", "read operation", "connection reset",
                      "remote end closed", "broken pipe", "503", "500")


def _is_retryable_scan_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(kw in msg for kw in _SCAN_RETRY_ERRORS)


def _scan_one_user(
    email: str,
    *,
    user_dir: Path,
    sa_file: Path,
    modified_after: datetime | None,
    max_files: int | None,
    log_prefix: str,
) -> tuple[str, list[dict[str, Any]] | Exception]:
    """Scan one user's Drive metadata with retry + backoff. Safe to call from a thread pool."""
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(_SCAN_MAX_ATTEMPTS):
        try:
            service = build_drive_service_dwd(email, sa_file=sa_file)
            rows = walk_drive_folder(
                service,
                "root",
                max_files=max_files,
                progress_log=lambda m: _log(f"{log_prefix} {m}"),
                scan_cache_path=None,  # we do our own per-user caching
            )
            rows = _filter_dependency_rows(rows, log_prefix)
            _save_scan_rows(user_dir, rows)
            file_count = _scan_file_count(rows)
            _log(f"{log_prefix} [scan] {file_count} files found (after dependency filter)")
            return email, rows
        except Exception as e:
            last_exc = e
            if _is_retryable_scan_error(e) and attempt < _SCAN_MAX_ATTEMPTS - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s, 8s
                _log(f"{log_prefix} [scan] attempt {attempt + 1}/{_SCAN_MAX_ATTEMPTS} failed "
                     f"({e}) — retrying in {wait}s")
                time.sleep(wait)
            else:
                break
    return email, last_exc


def _count_files_for_user(
    email: str,
    *,
    sa_file: Path,
    max_files: int | None,
    log_prefix: str,
) -> tuple[str, dict[str, int] | Exception]:
    """Count one user's Drive files directly from the Drive API."""
    try:
        service = build_drive_service_dwd(email, sa_file=sa_file)
        rows = walk_drive_folder(
            service,
            "root",
            include_folders=True,
            max_files=max_files,
            progress_log=lambda m: _log(f"{log_prefix} {m}"),
            scan_cache_path=None,
        )
        file_count = _scan_file_count(rows)
        folder_count = max(0, len(rows) - file_count)
        _log(f"{log_prefix} [count] {file_count} files, {folder_count} folders")
        return email, {
            "file_count": file_count,
            "folder_count": folder_count,
            "total_rows": len(rows),
        }
    except Exception as e:
        return email, e


def run_api_file_count(
    users: list[dict],
    *,
    out_dir: Path,
    sa_file: Path,
    max_files: int | None,
    scan_workers: int,
    state: RunState,
) -> Path:
    """Count Workspace files from Drive API only and write workspace_file_count.json."""
    total = len(users)
    _log(f"[count] starting API file count for {total} users with {scan_workers} workers")
    log_prefix_map = {
        u["email"]: f"[{i}/{total}][{u['email']}]"
        for i, u in enumerate(users, 1)
    }
    counts_by_email: dict[str, dict[str, int]] = {}
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=scan_workers) as pool:
        futures = {
            pool.submit(
                _count_files_for_user,
                u["email"],
                sa_file=sa_file,
                max_files=max_files,
                log_prefix=log_prefix_map.get(u["email"], f"[{u['email']}]"),
            ): u["email"]
            for u in users
        }
        done = 0
        for fut in as_completed(futures):
            email = futures[fut]
            done += 1
            email_ret, result = fut.result()
            if isinstance(result, Exception):
                errors[email] = str(result)
                _log(f"[count] ERROR {email}: {result}")
            else:
                counts_by_email[email_ret] = result
                state.set_scan_counts(
                    email_ret,
                    file_count=result["file_count"],
                    total_rows=result["total_rows"],
                )
                _log(f"[count] {done}/{total} done: {email_ret}")

    per_user: list[dict[str, Any]] = []
    total_files = 0
    total_folders = 0
    total_rows = 0
    for u in users:
        email = u["email"]
        counts = counts_by_email.get(email)
        if not counts:
            continue
        file_count = int(counts["file_count"])
        folder_count = int(counts["folder_count"])
        row_count = int(counts["total_rows"])
        total_files += file_count
        total_folders += folder_count
        total_rows += row_count
        per_user.append({
            "email": email,
            "name": u.get("name", ""),
            "org_unit": u.get("org_unit", "/"),
            "file_count": file_count,
            "folder_count": folder_count,
            "total_rows": row_count,
            "source": "drive_api",
        })

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "source": "drive_api",
        "total_files": total_files,
        "total_folders": total_folders,
        "total_rows": total_rows,
        "counted_users": len(per_user),
        "error_users": errors,
        "per_user": per_user,
    }
    out_path = out_dir / "workspace_file_count.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _log(f"[count] total files={total_files}, counted_users={len(per_user)}, errors={len(errors)}")
    _log(f"[count] wrote {out_path}")
    return out_path


def run_concurrent_scan(
    users_to_scan: list[dict],
    *,
    out_dir: Path,
    sa_file: Path,
    modified_after: datetime | None,
    max_files: int | None,
    scan_workers: int,
    state: RunState,
    log_prefix_map: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """Scan all users concurrently. Returns {email: rows}."""
    results: dict[str, list[dict[str, Any]]] = {}
    total = len(users_to_scan)
    _log(f"[scan] starting concurrent scan of {total} users with {scan_workers} workers")

    with ThreadPoolExecutor(max_workers=scan_workers) as pool:
        futures = {
            pool.submit(
                _scan_one_user,
                u["email"],
                user_dir=_user_dir(out_dir, u["email"]),
                sa_file=sa_file,
                modified_after=modified_after,
                max_files=max_files,
                log_prefix=log_prefix_map.get(u["email"], f"[{u['email']}]"),
            ): u["email"]
            for u in users_to_scan
        }

        done = 0
        for fut in as_completed(futures):
            email = futures[fut]
            done += 1
            email_ret, result = fut.result()
            if isinstance(result, Exception):
                _log(f"[scan] ERROR {email}: {result}")
                state.set(email, RunState.ERROR, error=str(result))
            else:
                results[email] = result
                _record_scan_counts(state, email, result)
                state.set(email, RunState.SCAN_DONE)
                _log(f"[scan] {done}/{total} done: {email}")

    _log(f"[scan] complete: {done} users scanned, {len(results)} succeeded")
    return results


# ---------------------------------------------------------------------------
# Phase B helpers
# ---------------------------------------------------------------------------

def _load_raw_evidence_df(out_path: str) -> pd.DataFrame:
    """Reconstruct raw evidence DataFrame from the three checkpoint sidecars.

    Merges:
      - drive_artifacts.jsonl  → raw file metadata (drive_file_id, filename, mime_type, …)
      - checkpoint.jsonl       → LLM classification (bucket_number, bucket_name, confidence, …)
      - subcat.jsonl           → sub_category
    """
    arti_path  = drive_artifact_checkpoint_path(out_path)
    ckpt_path  = checkpoint_path_for_output(out_path)
    subcat_path = subcategory_checkpoint_path(out_path)

    raw_rows = load_drive_artifact_checkpoint(arti_path)   # {aid: row_dict}
    cls_data  = load_checkpoint(ckpt_path)                  # {row_id: cls_dict}
    sub_data  = load_checkpoint(subcat_path)                # {row_id: {"sub_category": …}}

    if not raw_rows:
        return pd.DataFrame()

    rows = []
    for aid, row in sorted(raw_rows.items()):
        combined = dict(row)
        # Merge classification results
        for k, v in cls_data.get(aid, {}).items():
            if k != "row_id":
                combined[k] = v
        # Merge sub_category
        combined["sub_category"] = sub_data.get(aid, {}).get("sub_category", "")
        rows.append(combined)

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Phase B: LLM classification
# ---------------------------------------------------------------------------

def _phase_classify(
    email: str,
    scan_rows: list[dict[str, Any]],
    *,
    user_dir: Path,
    sa_file: Path,
    pass1_model: str | None,
    pass2_model: str,
    max_files: int | None,
    snippet_bytes: int,
    modified_after: datetime | None,
    extract_workers: int,
    log_prefix: str,
) -> pd.DataFrame:
    """Run LLM classify pipeline on pre-scanned rows. Returns evidence DataFrame."""
    t0 = time.time()
    user_dir.mkdir(parents=True, exist_ok=True)
    out_path = str(user_dir / "inventory.xlsx")

    file_rows = [r for r in scan_rows if not r.get("is_folder")]
    _phase_log(log_prefix, "classify", f"START — {len(scan_rows)} total rows, {len(file_rows)} files")
    _phase_log(log_prefix, "classify", f"models: pass1={pass1_model or 'default'}, pass2={pass2_model}")
    _phase_log(log_prefix, "classify", f"extract_workers={extract_workers}, snippet_bytes={snippet_bytes}")
    _phase_log(log_prefix, "classify", f"output → {out_path}")

    creds = get_dwd_credentials(email, sa_file=sa_file)
    service = build_drive_service_dwd(email, sa_file=sa_file)
    _phase_log(log_prefix, "classify", "Drive DWD service ready — starting LLM classify pipeline")

    build_inventory_from_drive_1tb(
        service=service,
        folder_id="root",
        output_path=out_path,
        scan_rows=scan_rows,
        pass1_model=pass1_model,
        pass2_model=pass2_model,
        max_files=max_files,
        snippet_export_bytes=snippet_bytes,
        progress_log=lambda m: _log(f"{log_prefix} {m}"),
        progress_every=500,
        ingest_log_every=200,
        workers=extract_workers,
        creds=creds,
    )

    df = _load_raw_evidence_df(out_path)
    if not df.empty:
        _phase_log(log_prefix, "classify", f"DONE — {len(df)} evidence rows classified in {_elapsed(t0)}")
        return df

    try:
        df = pd.read_excel(out_path, sheet_name="Evidence", engine="openpyxl")
        _phase_log(log_prefix, "classify", f"DONE — {len(df)} evidence rows (XLSX fallback) in {_elapsed(t0)}")
        return df
    except Exception as e:
        _phase_log(log_prefix, "classify", f"WARN — could not load evidence after {_elapsed(t0)}: {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Phase C: Full file download
# ---------------------------------------------------------------------------

def _phase_download(
    email: str,
    evidence_df: pd.DataFrame,
    *,
    user_dir: Path,
    sa_file: Path,
    log_prefix: str,
) -> None:
    if not _DOWNLOAD_AVAILABLE:
        _phase_log(log_prefix, "download", "SKIP — dump.full_download not available")
        return
    if evidence_df.empty:
        _phase_log(log_prefix, "download", "SKIP — no evidence rows")
        return
    t0 = time.time()
    _phase_log(log_prefix, "download", f"START — {len(evidence_df)} files → {user_dir / 'dump'}")
    service = build_drive_service_dwd(email, sa_file=sa_file)
    download_all_by_bucket(
        service,
        evidence_df,
        out_dir=user_dir / "dump",
        log=lambda m: _log(f"{log_prefix} {m}"),
    )
    dump_dir = user_dir / "dump"
    if dump_dir.exists():
        n_files = sum(1 for _ in dump_dir.rglob("*") if _.is_file())
        total_mb = sum(f.stat().st_size for f in dump_dir.rglob("*") if f.is_file()) / 1_048_576
        _phase_log(log_prefix, "download", f"DONE — {n_files} files, {total_mb:.1f} MB in {_elapsed(t0)}")
    else:
        _phase_log(log_prefix, "download", f"DONE in {_elapsed(t0)}")


# ---------------------------------------------------------------------------
# Phase D: Gmail fetch
# ---------------------------------------------------------------------------

def _phase_gmail(
    email: str,
    *,
    user_dir: Path,
    sa_file: Path,
    modified_after: datetime | None,
    log_prefix: str,
) -> None:
    if not _GMAIL_AVAILABLE:
        _phase_log(log_prefix, "gmail", "SKIP — gmail.fetch not available")
        return
    t0 = time.time()
    date_note = f" after {modified_after.date()}" if modified_after else " (all time)"
    _phase_log(log_prefix, "gmail", f"START — fetching emails + attachments{date_note}")
    try:
        gmail_svc = build_gmail_service(email, sa_file=sa_file)
        _phase_log(log_prefix, "gmail", "Gmail service ready — listing messages")
        try:
            count = fetch_and_export_emails(
                gmail_svc,
                out_dir=user_dir / "dump" / "emails",
                modified_after=modified_after,
                log=lambda m: _log(f"{log_prefix} {m}"),
                mailbox=email,
            )
        except TypeError:
            count = fetch_and_export_emails(
                gmail_svc,
                out_dir=user_dir / "dump" / "emails",
                modified_after=modified_after,
                log=lambda m: _log(f"{log_prefix} {m}"),
            )
        _phase_log(log_prefix, "gmail", f"DONE — {count} emails exported in {_elapsed(t0)}")
    except Exception as e:
        _phase_log(log_prefix, "gmail", f"ERROR after {_elapsed(t0)}: {e} — continuing without emails")


# ---------------------------------------------------------------------------
# Phase E: S3 upload + local delete
# ---------------------------------------------------------------------------

def _phase_s3_sync(
    email: str,
    *,
    user_dir: Path,
    syncer: S3Syncer | None,
    log_prefix: str,
) -> None:
    if syncer is None:
        _phase_log(log_prefix, "s3", "SKIP — no bucket configured")
        return
    t0 = time.time()
    if user_dir.exists():
        n_files = sum(1 for f in user_dir.rglob("*") if f.is_file())
        total_mb = sum(f.stat().st_size for f in user_dir.rglob("*") if f.is_file()) / 1_048_576
        _phase_log(log_prefix, "s3", f"START — uploading {n_files} files ({total_mb:.1f} MB) → S3, then deleting local")
    else:
        _phase_log(log_prefix, "s3", "START — user_dir not found, nothing to upload")
    syncer.sync_user(user_dir, email=email, delete_after=True)
    _phase_log(log_prefix, "s3", f"DONE in {_elapsed(t0)}")


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

_CSV_COLUMNS = [
    "user_email",
    "Item Name",
    "File Type",
    "Content Summary",
    "PII Flag",
    "Size",
    "Word Count",
    "LLM Tokens",
    "Page Count",
    "Modality",
    "Content Type",
    "Quality tier",
    "What is contained",
    "Source",
    "Path / Subsection",
    "bucket_number",
    "bucket_name",
    "Sub-category",
    "confidence",
    "rationale",
]


def _write_user_csv(
    email: str,
    evidence_df: pd.DataFrame,
    user_dir: Path,
    combined_csv: Path,
) -> Path | None:
    """Write per-user CSV and append to combined CSV. Returns per-user CSV path."""
    if evidence_df.empty:
        _log(f"[csv] {email}: no evidence rows — skipping CSV")
        return None

    # If already in display format (read back from XLSX Evidence sheet),
    # skip transformation — calling evidence_display_dataframe again would
    # look for raw column names (filename, extension, …) which no longer exist.
    if "Item Name" in evidence_df.columns:
        display = evidence_df.copy()
    else:
        display = evidence_display_dataframe(evidence_df)
    display.insert(0, "user_email", email)

    # Ensure all expected columns exist (fill missing with empty string)
    for col in _CSV_COLUMNS:
        if col not in display.columns:
            display[col] = ""
    display = display[_CSV_COLUMNS]

    # Per-user CSV
    user_csv = user_dir / "inventory.csv"
    user_dir.mkdir(parents=True, exist_ok=True)
    display.to_csv(str(user_csv), index=False, encoding="utf-8-sig")
    _log(f"[csv] {email}: wrote {len(display)} rows → {user_csv}")

    # Append to combined CSV (write header only if file doesn't exist yet)
    write_header = not combined_csv.is_file() or combined_csv.stat().st_size == 0
    combined_csv.parent.mkdir(parents=True, exist_ok=True)
    display.to_csv(
        str(combined_csv),
        mode="a",
        header=write_header,
        index=False,
        encoding="utf-8-sig",
    )

    return user_csv


# ---------------------------------------------------------------------------
# Zip output
# ---------------------------------------------------------------------------

def _zip_output(out_dir: Path) -> Path | None:
    """Zip everything inside out_dir into a timestamped archive next to it."""
    if not out_dir.exists():
        _log("[zip] out_dir not found — skipping zip")
        return None

    files = [f for f in out_dir.rglob("*") if f.is_file()]
    if not files:
        _log("[zip] out_dir is empty — skipping zip")
        return None

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    zip_path = out_dir.parent / f"workspace_inventory_{ts}.zip"

    _log(f"[zip] creating {zip_path} ({len(files)} files)...")
    t0 = time.time()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            arcname = f.relative_to(out_dir.parent)
            zf.write(f, arcname)

    zip_mb = zip_path.stat().st_size / 1_048_576
    _log(f"[zip] DONE — {zip_path.name} ({zip_mb:.1f} MB) in {_elapsed(t0)}")
    return zip_path


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_modified_after(since_days: int, modified_after_str: str) -> datetime | None:
    if since_days > 0 and modified_after_str:
        print("Error: use either --since-days or --modified-after, not both.", file=sys.stderr)
        sys.exit(1)
    if since_days > 0:
        return datetime.now(timezone.utc) - timedelta(days=since_days)
    if modified_after_str:
        try:
            return datetime.strptime(modified_after_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"Error: --modified-after must be YYYY-MM-DD, got {modified_after_str!r}", file=sys.stderr)
            sys.exit(1)
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Org-wide Workspace inventory: scan → classify → local/S3 output.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full pipeline (classify + download + gmail) → S3
  python run_org_classify.py --admin admin@co.com --s3-bucket my-bucket

  # Classify only (no file downloads) — still uploads results to S3
  python run_org_classify.py --admin admin@co.com --s3-bucket my-bucket --classify-only

  # Local-only output (no S3 upload/delete)
  python run_org_classify.py --admin admin@co.com --local-only

  # Count files directly from the Drive API, then exit
  python run_org_classify.py --admin admin@co.com --count-files-only

  # Last 30 days
  python run_org_classify.py --admin admin@co.com --s3-bucket my-bucket --since-days 30

  # No S3 — keep files local
  python run_org_classify.py --admin admin@co.com

  # Dry run — list users only
  python run_org_classify.py --admin admin@co.com --dry-run

  # Resume a stopped run — re-run the exact same command
  python run_org_classify.py --admin admin@co.com --s3-bucket my-bucket
        """,
    )
    p.add_argument("--admin",             required=True,               help="Admin email for org user listing")
    p.add_argument("--out",               default="out",               help="Output root directory (default: out)")
    p.add_argument("--sa-file",           default="",                  help="Path to service_account.json (auto-detected if omitted)")
    p.add_argument("--s3-bucket",         default="",                  help="S3 bucket name (omit to keep files local)")
    p.add_argument("--s3-prefix",         default="",                  help="S3 key prefix (auto-generated if omitted)")
    p.add_argument("--local-only",        action="store_true",         help="Keep outputs local; do not upload to S3 or delete user directories")
    p.add_argument("--pass1-model",       default="",                  help="Fast model for pass-1 (all files)")
    p.add_argument("--pass2-model",       default=default_llm_model(), help="Full model for pass-2 (low-confidence re-run)")
    p.add_argument("--max-files",         type=int, default=0,         help="Cap Drive file count per user (0=unlimited)")
    p.add_argument("--snippet-bytes",     type=int, default=2048,      help="Bytes exported per file for classification (default: 2048)")
    p.add_argument("--since-days",        type=int, default=0,         metavar="N",
                   help="Only process files modified in the last N days")
    p.add_argument("--modified-after",    type=str, default="",        metavar="YYYY-MM-DD",
                   help="Only process files modified after this date (UTC)")
    p.add_argument("--skip",              action="append", default=[], metavar="EMAIL",
                   help="Skip this user email (repeatable)")
    p.add_argument("--only",              action="append", default=[], metavar="EMAIL",
                   help="Process ONLY this user email (repeatable); all others are skipped")
    p.add_argument("--count-files-only",  action="store_true",
                   help="Count Drive files directly from the API, write workspace_file_count.json, then exit")
    p.add_argument("--classify-only",     action="store_true",
                   help="Skip file downloads and Gmail; classify metadata only")
    p.add_argument("--gmail",             action="store_true",
                   help="Fetch Gmail and store raw exports with the run output (off by default; no LLM classification of emails)")
    p.add_argument("--scan-workers",      type=int, default=4,         metavar="N",
                   help="Parallel threads for Drive metadata scan phase (default: 4)")
    p.add_argument("--extract-workers",  type=int, default=8,         metavar="N",
                   help="Parallel threads for snippet extraction / LLM batching per user (default: 8)")
    p.add_argument("--dry-run",           action="store_true",         help="List users only — do not process")
    args = p.parse_args(argv)

    if args.local_only and args.s3_bucket:
        p.error("--local-only cannot be used with --s3-bucket")

    sa_file        = Path(args.sa_file).expanduser().resolve() if args.sa_file else default_sa_path()
    out_dir        = (_ROOT / args.out).resolve()
    state_path     = out_dir / "run_state.json"
    combined_csv   = out_dir / "org_inventory.csv"
    max_files      = None if args.max_files == 0 else args.max_files
    skip_set       = set(args.skip)
    only_set       = set(args.only)
    modified_after = _parse_modified_after(args.since_days, args.modified_after)
    classify_only  = args.classify_only
    fetch_gmail    = args.gmail and not classify_only
    local_only     = args.local_only or not bool(args.s3_bucket)

    _log("=" * 60)
    _log(f"workspace-classifier  admin={args.admin}")
    if args.count_files_only:
        mode_parts = ["count-files-only"]
    else:
        mode_parts = ["classify-only"] if classify_only else ["classify + download"]
    if fetch_gmail:
        mode_parts.append("gmail")
    if not args.count_files_only:
        mode_parts.append("local-only" if local_only else "s3")
    _log(f"mode={' + '.join(mode_parts) if classify_only else ', '.join(mode_parts)}")
    _log(f"sa_file={sa_file}")
    _log(f"out_dir={out_dir}")
    _log(f"s3_bucket={args.s3_bucket or '(none — local only)'}")
    _log(f"modified_after={modified_after.date() if modified_after else 'all time'}")
    _log(f"scan_workers={args.scan_workers}")
    if args.only:
        _log(f"only={', '.join(args.only)}")
    _log("=" * 60)

    # ── S3 syncer ─────────────────────────────────────────────────────────────
    syncer: S3Syncer | None = None
    if args.s3_bucket:
        syncer = S3Syncer(
            bucket=args.s3_bucket,
            admin_email=args.admin,
            prefix=args.s3_prefix,
            log=_log,
        )
        _log(f"S3 prefix: {syncer.prefix}/")

    # ── List org users ─────────────────────────────────────────────────────────
    _log("listing org users via Admin SDK...")
    users = list_org_users(args.admin, sa_file=sa_file)
    users = [u for u in users if u["email"] not in skip_set]
    if only_set:
        users = [u for u in users if u["email"] in only_set]
    _log(f"found {len(users)} active users (after skips/only filter)")

    if args.dry_run:
        for u in users:
            print(f"  {u['email']}  ({u['name']})  {u['org_unit']}")
        return 0

    # ── Load run state ─────────────────────────────────────────────────────────
    state = RunState(state_path)
    state.ensure_users(users)
    _log(f"run state: {state.summary()}")

    if args.count_files_only:
        file_count_path = run_api_file_count(
            users,
            out_dir=out_dir,
            sa_file=sa_file,
            max_files=max_files,
            scan_workers=args.scan_workers,
            state=state,
        )
        _log(f"file count JSON: {file_count_path}")
        if syncer and file_count_path.is_file():
            _log("[s3] uploading workspace_file_count.json")
            syncer.upload_file(file_count_path, s3_sub_path="")
        return 0

    to_process = [u for u in users if not state.is_complete(u["email"], local_only=local_only)]
    _log(f"{len(to_process)} users to process")

    if not to_process:
        _log("all users already complete — nothing to do")
        file_count_path = _write_workspace_file_count(out_dir, users, state)
        try:
            file_count_data = json.loads(file_count_path.read_text(encoding="utf-8"))
            _log(f"workspace file count: {file_count_data.get('total_files', 0)} files")
        except Exception:
            _log(f"workspace file count: {file_count_path}")
        _log(f"combined CSV: {combined_csv}")
        _log(f"file count JSON: {file_count_path}")
        return 0

    # ── Build prefix map for logging ───────────────────────────────────────────
    total = len(to_process)
    log_prefix_map = {
        u["email"]: f"[{i}/{total}][{u['email']}]"
        for i, u in enumerate(to_process, 1)
    }

    # ── Phase A: Concurrent Drive scan ────────────────────────────────────────
    users_needing_scan = [u for u in to_process if not state.scan_already_done(u["email"])]

    if users_needing_scan:
        _log(f"[scan] {len(users_needing_scan)} users need scanning")
        run_concurrent_scan(
            users_needing_scan,
            out_dir=out_dir,
            sa_file=sa_file,
            modified_after=modified_after,
            max_files=max_files,
            scan_workers=args.scan_workers,
            state=state,
            log_prefix_map=log_prefix_map,
        )
    else:
        _log("[scan] all users already scanned — skipping scan phase")

    # ── Phase B/C/D/E: Sequential per-user processing ─────────────────────────
    for i, u in enumerate(to_process, 1):
        email    = u["email"]
        pfx      = log_prefix_map[email]
        user_dir = _user_dir(out_dir, email)

        # Skip if scan failed
        if state.get_status(email) == RunState.ERROR:
            _log(f"{pfx} scan failed — skipping classify")
            continue

        # Already classified locally but not yet S3-synced (e.g. previous run was local-only)
        if state.needs_s3_sync(email) and syncer:
            _log(f"{pfx} already classified — uploading to S3")
            _phase_s3_sync(email, user_dir=user_dir, syncer=syncer, log_prefix=pfx)
            state.set(email, RunState.S3_SYNCED)
            continue

        # Already complete
        if state.is_complete(email, local_only=local_only):
            continue

        _banner(f"USER {i}/{total}: {email}  ({u['name']})")
        t0 = time.time()

        try:
            # Load pre-scanned rows
            scan_rows = _load_scan_rows(user_dir)
            if not scan_rows:
                _phase_log(pfx, "scan", "WARN — scan cache empty, re-scanning inline")
                service = build_drive_service_dwd(email, sa_file=sa_file)
                scan_rows = walk_drive_folder(
                    service, "root",
                    max_files=max_files,
                    progress_log=lambda m: _log(f"{pfx} {m}"),
                    scan_cache_path=None,
                )
                scan_rows = _filter_dependency_rows(scan_rows, pfx)
                _save_scan_rows(user_dir, scan_rows)
                _record_scan_counts(state, email, scan_rows)
            else:
                file_count = _scan_file_count(scan_rows)
                _record_scan_counts(state, email, scan_rows)
                _phase_log(pfx, "scan", f"loaded from cache — {file_count} files, {len(scan_rows)} total rows")

            # Phase B — classify
            evidence_df = _phase_classify(
                email,
                scan_rows,
                user_dir=user_dir,
                sa_file=sa_file,
                pass1_model=args.pass1_model or None,
                pass2_model=args.pass2_model,
                max_files=max_files,
                snippet_bytes=args.snippet_bytes,
                modified_after=modified_after,
                extract_workers=args.extract_workers,
                log_prefix=pfx,
            )

            # Write CSVs immediately after classify (before download, so we have
            # data even if downloads fail or S3 runs out of space)
            _phase_log(pfx, "csv", f"writing per-user CSV + appending to combined CSV")
            _write_user_csv(email, evidence_df, user_dir, combined_csv)

            if not evidence_df.empty and not classify_only:
                # Phase C — full file download
                _phase_download(
                    email, evidence_df,
                    user_dir=user_dir,
                    sa_file=sa_file,
                    log_prefix=pfx,
                )

                # Phase D — Gmail (only when --gmail flag is passed)
                if fetch_gmail:
                    _phase_gmail(
                        email,
                        user_dir=user_dir,
                        sa_file=sa_file,
                        modified_after=modified_after,
                        log_prefix=pfx,
                    )

            _phase_log(pfx, "user", f"all phases done in {_elapsed(t0)}")
            state.set(email, RunState.DONE)

            # Phase E — finalize output
            if syncer:
                _phase_s3_sync(email, user_dir=user_dir, syncer=syncer, log_prefix=pfx)
                state.set(email, RunState.S3_SYNCED)
            else:
                _phase_log(pfx, "local", f"DONE — output kept at {user_dir}")
                state.set(email, RunState.LOCAL_DONE)

        except KeyboardInterrupt:
            _phase_log(pfx, "user", f"INTERRUPTED after {_elapsed(t0)} — re-run to resume")
            state.set(email, RunState.ERROR, error="KeyboardInterrupt")
            raise
        except Exception as e:
            _phase_log(pfx, "user", f"ERROR after {_elapsed(t0)}: {e}")
            state.set(email, RunState.ERROR, error=str(e))

    # ── Final summary ─────────────────────────────────────────────────────────
    _log("=" * 60)
    _log(f"all users processed: {state.summary()}")
    file_count_path = _write_workspace_file_count(out_dir, users, state)
    try:
        file_count_data = json.loads(file_count_path.read_text(encoding="utf-8"))
        _log(f"workspace file count: {file_count_data.get('total_files', 0)} files")
    except Exception:
        _log(f"workspace file count: {file_count_path}")
    _log(f"combined CSV: {combined_csv}")
    _log(f"file count JSON: {file_count_path}")
    if syncer:
        _log(f"S3 output:   s3://{args.s3_bucket}/{syncer.prefix}/")
    _log("=" * 60)

    # Upload combined CSV to S3 root
    if syncer and combined_csv.is_file():
        _log("[s3] uploading org_inventory.csv")
        syncer.upload_file(combined_csv, s3_sub_path="")
    if syncer and file_count_path.is_file():
        _log("[s3] uploading workspace_file_count.json")
        syncer.upload_file(file_count_path, s3_sub_path="")

    # Zip everything remaining in out_dir (combined CSV + run_state + any kept user dirs)
    _banner("Creating output zip archive")
    zip_path = _zip_output(out_dir)
    if zip_path:
        _log(f"[zip] archive ready: {zip_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
