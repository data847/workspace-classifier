"""Download complete Drive files organised by LLM bucket.

After the classify phase produces an evidence DataFrame with
``bucket_number`` and ``bucket_name`` columns, this module downloads
the full content of every file (no snippet cap) into:

  out_dir/files/bucket_<n>_<name>/
    original_filename.pdf
    another_doc.docx
    ...

Binary files that cannot be exported (images, video, audio) get a
``.stub.txt`` placeholder with their metadata rather than being silently
skipped — so the folder count reflects the true file count.

Google Workspace native files (Docs, Sheets, Slides) are exported to
their nearest open format:
  Docs  → .docx
  Sheets → .xlsx
  Slides → .pptx
"""

from __future__ import annotations

import io
import re
import time
from pathlib import Path
from typing import Any

import pandas as pd
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

# ---------------------------------------------------------------------------
# MIME mappings for full-fidelity export (vs. text/plain used for snippets)
# ---------------------------------------------------------------------------

_GOOGLE_EXPORT_MIME: dict[str, tuple[str, str]] = {
    # mime_type → (export_mime, extension)
    "application/vnd.google-apps.document":
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet":
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation":
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing":
        ("image/png", ".png"),
}

_SKIP_MIME: frozenset[str] = frozenset({
    "application/vnd.google-apps.folder",
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.site",
})

_BINARY_PREFIXES: tuple[str, ...] = ("image/", "video/", "audio/")
_BINARY_EXACT: frozenset[str] = frozenset({
    "application/octet-stream",
    "application/zip", "application/x-zip-compressed",
    "application/x-tar", "application/x-gzip",
    "application/x-7z-compressed", "application/x-rar-compressed",
    "image/vnd.adobe.photoshop", "application/x-photoshop",
    "application/illustrator", "application/x-msdownload",
    "application/x-apple-diskimage",
})


def _is_binary(mime: str) -> bool:
    if mime in _BINARY_EXACT:
        return True
    return any(mime.startswith(p) for p in _BINARY_PREFIXES)


def _safe_name(name: str, max_len: int = 200) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip(". ")
    return name[:max_len] or "unnamed"


def _backoff(attempt: int) -> None:
    time.sleep(min(120.0, 0.75 * (2 ** attempt)))


def _bucket_folder_name(bucket_number: Any, bucket_name: Any) -> str:
    num = str(bucket_number).strip() if pd.notna(bucket_number) else "0"
    name = _safe_name(str(bucket_name).strip() if pd.notna(bucket_name) else "uncategorised")
    return f"bucket_{num}_{name}"


# ---------------------------------------------------------------------------
# Core download
# ---------------------------------------------------------------------------

def _download_one(
    service: Any,
    *,
    file_id: str,
    mime_type: str,
    display_name: str,
    dest: Path,
    max_retries: int = 6,
) -> str | None:
    """Download/export file to ``dest``. Returns error string or None on success."""
    if mime_type in _SKIP_MIME:
        return f"skip_mime:{mime_type}"

    export_info = _GOOGLE_EXPORT_MIME.get(mime_type)

    for attempt in range(max_retries + 1):
        fh = io.BytesIO()
        try:
            if export_info:
                export_mime, _ = export_info
                req = service.files().export_media(fileId=file_id, mimeType=export_mime)
            else:
                req = service.files().get_media(fileId=file_id, supportsAllDrives=True)

            dl = MediaIoBaseDownload(fh, req, chunksize=4 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk()

            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(fh.getvalue())
            return None

        except HttpError as e:
            if e.resp.status in (403, 429, 500, 503) and attempt < max_retries:
                _backoff(attempt)
                continue
            return f"HttpError {e.resp.status}: {e}"
        except OSError as e:
            return f"OSError: {e}"

    return "max_retries_exceeded"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def download_all_by_bucket(
    service: Any,
    evidence_df: pd.DataFrame,
    *,
    out_dir: Path,
    log: Any = None,
) -> dict[str, int]:
    """Download every file in ``evidence_df`` into bucket-organised folders.

    Parameters
    ----------
    service
        Authenticated Drive API v3 service (already impersonating the right user).
    evidence_df
        DataFrame with at minimum: ``drive_file_id`` (or ``artifact_id``),
        ``filename``, ``mime_type``, ``bucket_number``, ``bucket_name``,
        ``path``, ``size_bytes``.
    out_dir
        Root output directory. Files are written to
        ``out_dir/files/bucket_<n>_<name>/``.
    log
        Optional callable for progress messages.

    Returns
    -------
    dict mapping bucket folder name → number of files downloaded.
    """
    files_dir = out_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    def _log(msg: str) -> None:
        if log:
            log(msg)

    counts: dict[str, int] = {}
    total = len(evidence_df)

    for i, row in enumerate(evidence_df.itertuples(index=False), 1):
        file_id   = str(getattr(row, "drive_file_id", "") or "")
        mime      = str(getattr(row, "mime_type", "") or "")
        filename  = str(getattr(row, "filename",  "") or "file")
        bnum      = getattr(row, "bucket_number", 0)
        bname     = getattr(row, "bucket_name",   "uncategorised")
        size_b    = getattr(row, "size_bytes", None)

        if i % 100 == 0:
            _log(f"[dump] {i}/{total} files processed")

        bucket_folder = _bucket_folder_name(bnum, bname)
        dest_dir = files_dir / bucket_folder
        counts.setdefault(bucket_folder, 0)

        if not file_id:
            # Write stub
            stub = dest_dir / f"{_safe_name(filename)}.stub.txt"
            stub.parent.mkdir(parents=True, exist_ok=True)
            stub.write_text(f"missing_file_id\nfilename: {filename}\nmime: {mime}\n", encoding="utf-8")
            counts[bucket_folder] += 1
            continue

        if _is_binary(mime):
            # Binary stub — saves disk space, preserves presence in dump
            size_str = f"{int(size_b):,} bytes" if size_b else "unknown size"
            stub = dest_dir / f"{_safe_name(filename)}.stub.txt"
            stub.parent.mkdir(parents=True, exist_ok=True)
            stub.write_text(
                f"[binary file — not downloaded]\n"
                f"filename : {filename}\n"
                f"mime_type: {mime}\n"
                f"size     : {size_str}\n"
                f"drive_id : {file_id}\n",
                encoding="utf-8",
            )
            counts[bucket_folder] += 1
            continue

        # Determine destination filename + extension
        export_info = _GOOGLE_EXPORT_MIME.get(mime)
        if export_info:
            _, ext = export_info
            base = Path(filename).stem
        else:
            base = Path(filename).stem
            ext  = Path(filename).suffix or ".bin"

        safe_base = _safe_name(base)
        dest = dest_dir / f"{safe_base}{ext}"

        # Deduplicate filenames within the same bucket folder
        if dest.exists():
            dest = dest_dir / f"{safe_base}_{file_id[:8]}{ext}"

        err = _download_one(
            service,
            file_id=file_id,
            mime_type=mime,
            display_name=filename,
            dest=dest,
        )
        if err:
            _log(f"[dump] warn: {filename} ({file_id[:8]}) — {err}")
            stub = dest_dir / f"{safe_base}.error.txt"
            stub.parent.mkdir(parents=True, exist_ok=True)
            stub.write_text(f"download_error: {err}\nfile_id: {file_id}\nmime: {mime}\n", encoding="utf-8")

        counts[bucket_folder] += 1

    _log(f"[dump] full download complete — {sum(counts.values())} files across {len(counts)} buckets")
    return counts
