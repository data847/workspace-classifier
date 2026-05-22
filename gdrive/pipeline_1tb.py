"""Large-scale Google Drive inventory pipeline with two-pass LLM classification."""

from __future__ import annotations

import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from artifact_summary import summarize_artifact
from checkpoint import (
    append_checkpoint,
    append_drive_artifact_checkpoint,
    checkpoint_path_for_output,
    drive_artifact_checkpoint_path,
    load_checkpoint,
    load_drive_artifact_checkpoint,
)
from excel_io import write_company_inventory_workbook
from extractors.content_inventory import format_content_inventory_cell, gather_content_inventory
from extractors.content_type import infer_content_types_cell
from extractors.core import (
    MAX_CHARS_DEFAULT,
    count_llm_tokens,
    count_words,
    extract_full_text,
    is_title_only_media,
    is_video_file,
    media_title_text,
    page_count_from_file,
)
from extractors.modality import format_modalities_cell, infer_modalities, modality_from_mime
from extractors.quality_tier import infer_quality_tier
from gdrive.fetch import (
    canonical_extension,
    fetch_drive_file_to_path,
    is_binary_skip_mime,
    resolve_shortcut_target,
    suggested_local_suffix,
)
from gdrive.scan import normalize_folder_id, walk_drive_folder
from ingest import infer_source_system
from inventory_writer import summarize_inventory_from_evidence
from llm_provider import llm_api_configured
from pii import detect_pii
from segmenter import DEFAULT_BATCH_SIZE, DEFAULT_MODEL, classify_items
from subcategory_classifier import attach_subcategories

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Bytes to export from Google Workspace docs / PDFs.  2 KB gives ~300-500 words
# — enough for the classifier and PII detection.  Increase if you want richer
# quality_tier scores (at the cost of more bandwidth/time).
SNIPPET_EXPORT_BYTES: int = int(os.environ.get("PIPELINE_1TB_SNIPPET_BYTES", str(2 * 1024)))

# Pass-1 model + batch size (cheap, fast).
PASS1_MODEL_ENV = "PIPELINE_1TB_PASS1_MODEL"
PASS1_BATCH_SIZE: int = int(os.environ.get("PIPELINE_1TB_PASS1_BATCH", "100"))

# Pass-2 only re-classifies low-confidence items from pass 1.
PASS2_BATCH_SIZE: int = int(os.environ.get("PIPELINE_1TB_PASS2_BATCH", str(DEFAULT_BATCH_SIZE)))


_tl = threading.local()
_ckpt_lock = threading.Lock()


def _default_pass1_model() -> str:
    from llm_provider import llm_provider
    explicit = os.environ.get(PASS1_MODEL_ENV, "").strip()
    if explicit:
        return explicit
    # Default: cheapest available model per provider
    if llm_provider() == "openai":
        return "gpt-4o-mini"
    return "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Helpers (mirrors pipeline_build.py private helpers — kept local to avoid
# coupling; changes to the quality of evidence are intentional to only happen
# here or in pipeline_build.py, not both).
# ---------------------------------------------------------------------------

def _parse_drive_time_unix(s: str | None) -> float:
    if not s or not isinstance(s, str):
        return 0.0
    try:
        t = s.rstrip("Z") + "+00:00" if s.endswith("Z") else s
        return float(datetime.fromisoformat(t).timestamp())
    except Exception:
        return 0.0


def _int_size(v: object) -> int | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
        if x != x or x < 0:
            return None
        return int(x)
    except Exception:
        return None


def _effective_file_for_row(service: Any, row: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (file_id, mime_type, display_name, err)."""
    if row.get("is_folder"):
        return None, None, None, "folder"
    if row.get("is_shortcut"):
        tid = row.get("shortcut_target_id")
        if not tid:
            return None, None, None, "shortcut_without_target"
        tgt = resolve_shortcut_target(service, str(tid))
        if not tgt:
            return None, None, None, "shortcut_target_resolve_failed"
        mid = str(tgt.get("mimeType") or "")
        if mid == "application/vnd.google-apps.folder":
            return None, None, None, "shortcut_target_is_folder"
        return str(tgt.get("id")), mid, str(tgt.get("name") or row.get("name") or "file"), None
    fid = row.get("drive_file_id")
    if not fid:
        return None, None, None, "missing_drive_file_id"
    return str(fid), str(row.get("mime_type") or ""), str(row.get("name") or ""), None


def _extra_cols_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Pull the new attribution + URL columns out of a scan row."""
    return {
        "drive_file_id":   str(row.get("drive_file_id") or ""),
        "mime_type":       str(row.get("mime_type") or ""),
        "drive_url":       str(row.get("web_view_link") or ""),
        "owner_email":     str(row.get("owner_email") or ""),
        "last_modified_by": str(row.get("last_modified_by") or ""),
    }


def _build_evidence_from_temp_file(
    *,
    temp_path: Path,
    rel_path: str,
    filename: str,
    size_bytes: int | None,
    mtime_unix: float,
    artifact_id: int,
    extra: dict[str, Any],
    mime_type: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Full evidence + classifier item from a downloaded/exported file."""
    extension = canonical_extension(mime_type, filename)
    source_guess = infer_source_system(rel_path) or "Google Drive"
    stat_size = int(temp_path.stat().st_size) if temp_path.is_file() else (size_bytes or 0)

    if is_title_only_media(temp_path):
        title = media_title_text(temp_path)
        snippet_str = (title or "")[:800]
        word_count = len(title.split()) if title else 0
        token_count = count_llm_tokens(title) if title else 0
        page_count = 0
        modalities = infer_modalities(temp_path)
        inv_counts = gather_content_inventory(temp_path)
        kind = "video" if is_video_file(temp_path) else "audio"
        desc = (
            f"{filename} ({extension or 'noext'}, {kind} file). "
            "Only the file title/name is read for classification (no media content).\n"
            f"Title: {title}\nPath: {rel_path}"
        )
        pii = detect_pii(f"{title}\n{rel_path}")
        content_summary = summarize_artifact(
            filename=filename, rel_path=rel_path, extension=extension, snippet=title or None
        )
    else:
        full_text = extract_full_text(str(temp_path))
        snippet = (full_text[:MAX_CHARS_DEFAULT] + "\n…(truncated)…") if full_text and len(full_text) > MAX_CHARS_DEFAULT else full_text
        word_count = count_words(full_text) if full_text else 0
        token_count = count_llm_tokens(full_text) if full_text else 0
        page_count = page_count_from_file(temp_path, word_count=word_count)
        modalities = infer_modalities(temp_path)
        inv_counts = gather_content_inventory(temp_path)
        desc = f"{filename} ({extension or 'noext'}, {int(stat_size)} bytes)"
        if snippet:
            desc = desc + "\n" + snippet
        pii = detect_pii(snippet)
        content_summary = summarize_artifact(
            filename=filename, rel_path=rel_path, extension=extension, snippet=snippet
        )
        snippet_str = (snippet or "")[:800]

    ev = {
        "artifact_id": artifact_id,
        "path": rel_path,
        "filename": filename,
        "extension": extension,
        "size_bytes": int(stat_size) if stat_size is not None else None,
        "modified_time_unix": float(mtime_unix),
        "source_guess": source_guess,
        "snippet": snippet_str,
        "content_summary": content_summary,
        "pii_flag": "Yes" if pii.has_pii else "No",
        "pii_types": ", ".join(pii.types),
        "word_count": int(word_count),
        "token_count": int(token_count),
        "page_count": int(page_count),
        "modality": format_modalities_cell(modalities),
        "content_inventory": format_content_inventory_cell(inv_counts),
        "bucket_number": "",
        "bucket_name": "",
        "confidence": "",
        "rationale": "",
        "llm_pass": "",
        **extra,
    }
    pending = {
        "row_id": artifact_id,
        "description": desc,
        "source": source_guess,
        "filename": filename,
        "extension": extension,
        "path_hint": rel_path,
        "snippet": snippet_str,
    }
    return ev, pending


def _metadata_only_evidence(
    *,
    artifact_id: int,
    rel_path: str,
    filename: str,
    extension: str,
    size_bytes: int | None,
    mtime_unix: float,
    reason: str,
    extra: dict[str, Any],
    mime_type: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Evidence built purely from path + filename (no byte download).

    Used for binary files (images, video, audio) where downloading adds no
    classification value — the LLM classifier still gets a full description
    with path, name, extension, and size so rationale quality is maintained.
    """
    source_guess = infer_source_system(rel_path) or "Google Drive"
    snippet_str = f"[{reason}]\nFilename: {filename}\nPath: {rel_path}"[:800]
    desc = (
        f"{filename} ({extension or 'noext'}, {_human_size(size_bytes)}). "
        f"Path: {rel_path}\n"
        f"Note: {reason} — classified from filename/path/extension only."
    )
    pii = detect_pii(snippet_str)
    content_summary = summarize_artifact(
        filename=filename, rel_path=rel_path, extension=extension, snippet=None
    )
    modalities = modality_from_mime(mime_type)
    ev = {
        "artifact_id": artifact_id,
        "path": rel_path,
        "filename": filename,
        "extension": extension,
        "size_bytes": int(size_bytes) if size_bytes is not None else None,
        "modified_time_unix": float(mtime_unix),
        "source_guess": source_guess,
        "snippet": snippet_str,
        "content_summary": content_summary,
        "pii_flag": "Yes" if pii.has_pii else "No",
        "pii_types": ", ".join(pii.types),
        "word_count": 0,
        "token_count": 0,
        "page_count": 0,
        "modality": format_modalities_cell(modalities),
        "content_inventory": "",
        "bucket_number": "",
        "bucket_name": "",
        "confidence": "",
        "rationale": "",
        "llm_pass": "",
        **extra,
    }
    pending = {
        "row_id": artifact_id,
        "description": desc,
        "source": source_guess,
        "filename": filename,
        "extension": extension,
        "path_hint": rel_path,
        "snippet": snippet_str,
    }
    return ev, pending


def _human_size(b: int | None) -> str:
    if b is None:
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f} {unit}"
        b /= 1024  # type: ignore[assignment]
    return f"{b:.1f} TB"


# ---------------------------------------------------------------------------
# Phase helpers
# ---------------------------------------------------------------------------

def _run_classify_pass(
    pending: list[dict[str, Any]],
    *,
    model: str,
    batch_size: int,
    pass_num: int,
    ckpt_path: Path,
    evidence_rows: list[dict[str, Any]],
    progress_log: Callable[[str], None] | None,
    workers: int = 1,
) -> dict[int, dict[str, Any]]:
    """Classify ``pending`` items in batches; append results to checkpoint.

    Returns a dict {row_id: classification_dict} for all items processed.
    Workers > 1 sends batches to the LLM in parallel for higher throughput.
    """
    classified: dict[int, dict[str, Any]] = {}
    total = len(pending)
    done = 0

    already_classified = set(load_checkpoint(ckpt_path).keys())

    # Split into (batch_index, batch) pairs, skipping fully-done batches upfront.
    work: list[tuple[int, list[dict[str, Any]]]] = []
    for batch_start in range(0, total, batch_size):
        batch = pending[batch_start: batch_start + batch_size]
        if all(item["row_id"] in already_classified for item in batch):
            done += len(batch)
            if batch_start % (batch_size * 10) == 0 and progress_log:
                progress_log(f"[pass{pass_num}] batch {batch_start//batch_size + 1} skipped (already classified)")
            continue
        work.append((batch_start, batch))

    if not work:
        return classified

    lock = threading.Lock()

    def _process(batch_start: int, batch: list[dict[str, Any]]) -> None:
        nonlocal done
        try:
            results = classify_items(batch, model=model)
        except Exception as exc:
            print(
                f"[pass{pass_num}] batch {batch_start//batch_size + 1} FAILED — {exc}",
                flush=True,
            )
            if progress_log:
                progress_log(f"[pass{pass_num}] batch {batch_start//batch_size + 1} FAILED — {exc}")
            with lock:
                done += len(batch)
            return

        cleaned: list[dict[str, Any]] = []
        for c in (results or []):
            if not isinstance(c, dict):
                continue
            rid = c.get("row_id")
            try:
                rid_i = int(rid)
            except Exception:
                continue
            c["row_id"] = rid_i
            c["llm_pass"] = pass_num
            cleaned.append(c)

        with lock:
            for c in cleaned:
                classified[c["row_id"]] = c
            append_checkpoint(ckpt_path, cleaned)
            for c in cleaned:
                idx = c["row_id"] - 1
                if 0 <= idx < len(evidence_rows):
                    evidence_rows[idx]["llm_pass"] = pass_num
            done += len(batch)
            msg = (
                f"[pass{pass_num}] batch {batch_start//batch_size + 1} "
                f"done={done}/{total} classified={len(cleaned)} model={model}"
            )
            print(msg, flush=True)
            if progress_log:
                progress_log(msg)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futs = [executor.submit(_process, bs, b) for bs, b in work]
        for fut in as_completed(futs):
            fut.result()  # re-raise any unexpected exception

    return classified


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _extract_one(
    aid: int,
    row: dict[str, Any],
    creds: Any,
    td_path: Path,
    snippet_export_bytes: int,
    arti_path: Path,
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    """Download + extract one file. Thread-safe. Returns (aid, ev, pend)."""
    if not hasattr(_tl, "service"):
        from gdrive.credentials import build_drive_service
        _tl.service = build_drive_service(creds)
    svc = _tl.service

    rel_path = str(row.get("path") or "").replace("\\", "/")
    filename = str(row.get("name") or "")
    mtime_unix = _parse_drive_time_unix(row.get("modified_time"))
    size_b = _int_size(row.get("size_bytes"))
    extra = _extra_cols_from_row(row)

    fid, mime, display_name, err0 = _effective_file_for_row(svc, row)

    # Use canonical_extension so Google Workspace files get gsheet/gdoc/gslide
    # instead of empty string (their filenames have no suffix in Drive).
    extension = canonical_extension(mime or "", filename)

    if err0:
        ev, pend = _metadata_only_evidence(
            artifact_id=aid, rel_path=rel_path, filename=filename,
            extension=extension, size_bytes=size_b, mtime_unix=mtime_unix,
            reason=err0, extra=extra, mime_type=mime or "",
        )
    elif mime and is_binary_skip_mime(mime):
        ev, pend = _metadata_only_evidence(
            artifact_id=aid, rel_path=rel_path, filename=filename,
            extension=extension, size_bytes=size_b, mtime_unix=mtime_unix,
            reason=f"binary_skip:{mime}", extra=extra, mime_type=mime,
        )
    elif (
        mime in ("application/vnd.google-apps.spreadsheet", "text/csv", "text/tab-separated-values")
        and size_b is not None
        and size_b > 200 * 1024
    ):
        ev, pend = _metadata_only_evidence(
            artifact_id=aid, rel_path=rel_path, filename=filename,
            extension=extension, size_bytes=size_b, mtime_unix=mtime_unix,
            reason="large_sheet_skip", extra=extra, mime_type=mime,
        )
    else:
        suffix = suggested_local_suffix(mime or "", display_name or filename)
        tmp = td_path / f"a{aid}{suffix}"
        err = fetch_drive_file_to_path(
            svc,
            file_id=fid,
            mime_type=mime or "",
            display_name=display_name or filename,
            dest=tmp,
            max_export_bytes=snippet_export_bytes if snippet_export_bytes > 0 else None,
        )
        if err or not tmp.is_file() or tmp.stat().st_size == 0:
            ev, pend = _metadata_only_evidence(
                artifact_id=aid, rel_path=rel_path, filename=filename,
                extension=extension, size_bytes=size_b, mtime_unix=mtime_unix,
                reason=err or "empty_download", extra=extra, mime_type=mime or "",
            )
        else:
            ev, pend = _build_evidence_from_temp_file(
                temp_path=tmp, rel_path=rel_path, filename=filename,
                size_bytes=size_b, mtime_unix=mtime_unix,
                artifact_id=aid, extra=extra, mime_type=mime or "",
            )
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

    with _ckpt_lock:
        append_drive_artifact_checkpoint(arti_path, aid, ev)

    return aid, ev, pend


def build_inventory_from_drive_1tb(
    *,
    service: Any,
    folder_id: str,
    output_path: str,
    # Pass-1 (fast, cheap) — all artifacts
    pass1_model: str | None = None,
    pass1_batch_size: int = PASS1_BATCH_SIZE,
    # Pass-2 (full quality) — confidence=low from pass 1 only
    pass2_model: str = DEFAULT_MODEL,
    pass2_batch_size: int = PASS2_BATCH_SIZE,
    # Subcategory / downstream enrichment model
    enrich_model: str = DEFAULT_MODEL,
    # Scan
    max_files: int | None = None,
    scan_cache_path: str | Path | None = None,
    scan_rows: list[dict[str, Any]] | None = None,
    # Download budget: bytes fetched per file for snippet (0 = metadata-only for all)
    snippet_export_bytes: int = SNIPPET_EXPORT_BYTES,
    progress_log: Callable[[str], None] | None = None,
    progress_every: int = 500,
    ingest_log_every: int = 100,
    # Parallel extract workers (needs creds to create per-thread services)
    workers: int = 1,
    creds: Any = None,
) -> str:
    """1TB-scale Drive inventory pipeline.

    Phases
    ------
    1. Scan — walk Drive API (or load from ``scan_cache_path``)
    2. Extract — for each file, choose download strategy:
         - Binary (image/video/audio/psd/zip) → metadata-only, no download
         - Google Workspace doc/sheet/slide    → export leading ``snippet_export_bytes``
         - PDF / Office doc                    → download leading ``snippet_export_bytes``
         - Small text / other                  → full download up to 2 MB
    3. Pass-1 classify — fast model, large batches, all artifacts
    4. Pass-2 classify — full model, standard batches, ``confidence=low`` only
    5. Enrich — subcategory, content_type, quality_tier
    6. Write XLSX — new tabs: Low Confidence, PII Flagged; new cols: drive_url, owner_email, llm_pass
    """
    if not llm_api_configured():
        raise RuntimeError(
            "No LLM API key configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY."
        )

    _pass1_model = pass1_model or _default_pass1_model()

    out_path = Path(output_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ckpt_path = checkpoint_path_for_output(str(out_path))
    arti_path = drive_artifact_checkpoint_path(str(out_path))

    existing_class = load_checkpoint(ckpt_path)
    existing_rows = load_drive_artifact_checkpoint(arti_path)

    # -----------------------------------------------------------------------
    # Phase 1: Scan
    # -----------------------------------------------------------------------
    if scan_rows is not None:
        rows = scan_rows
    else:
        _cache = Path(scan_cache_path) if scan_cache_path else out_path.with_name(out_path.stem + ".scan_cache.jsonl")
        rows = walk_drive_folder(
            service,
            normalize_folder_id(folder_id),
            max_files=max_files,
            progress_log=progress_log,
            progress_every=progress_every,
            scan_cache_path=_cache,
        )

    file_rows = [r for r in rows if not r.get("is_folder")]
    total_files = len(file_rows)
    msg = f"[scan] total file rows: {total_files}"
    print(msg, flush=True)
    if progress_log:
        progress_log(msg)

    if total_files == 0:
        if progress_log:
            progress_log("[classify] SKIP — no Drive files found")
        empty_df = pd.DataFrame()
        write_company_inventory_workbook(
            inventory_df=pd.DataFrame(),
            evidence_df=empty_df,
            output_path=str(out_path),
        )
        return empty_df

    # -----------------------------------------------------------------------
    # Phase 2: Extract (download / metadata-only)
    # -----------------------------------------------------------------------
    evidence_rows: list[dict[str, Any]] = []
    pending_classify: list[dict[str, Any]] = []   # items not yet in checkpoint

    _use_parallel = workers > 1 and creds is not None
    if _use_parallel and progress_log:
        progress_log(f"[extract] parallel mode workers={workers}")

    # Collect (aid, row) pairs that still need downloading
    new_file_rows = [(aid, row) for aid, row in enumerate(file_rows, start=1) if aid not in existing_rows]

    # -- Parallel extract of new files --
    new_results: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
    with tempfile.TemporaryDirectory(prefix="drive-1tb-") as td:
        td_path = Path(td)

        if _use_parallel and new_file_rows:
            done_count = 0
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {
                    ex.submit(_extract_one, aid, row, creds, td_path, snippet_export_bytes, arti_path): aid
                    for aid, row in new_file_rows
                }
                for fut in as_completed(futs):
                    orig_aid = futs[fut]
                    try:
                        aid_r, ev, pend = fut.result()
                    except Exception as exc:
                        # fallback: metadata-only so the artifact_id slot is filled
                        row = file_rows[orig_aid - 1]
                        rel_path = str(row.get("path") or "").replace("\\", "/")
                        filename = str(row.get("name") or "")
                        ev, pend = _metadata_only_evidence(
                            artifact_id=orig_aid,
                            rel_path=rel_path,
                            filename=filename,
                            extension=Path(filename).suffix.lower().lstrip("."),
                            size_bytes=_int_size(row.get("size_bytes")),
                            mtime_unix=_parse_drive_time_unix(row.get("modified_time")),
                            reason=f"extract_error:{exc}",
                            extra=_extra_cols_from_row(row),
                        )
                        with _ckpt_lock:
                            append_drive_artifact_checkpoint(arti_path, orig_aid, ev)
                        aid_r = orig_aid
                    new_results[aid_r] = (ev, pend)
                    done_count += 1
                    if done_count % ingest_log_every == 0 and progress_log:
                        progress_log(
                            f"[extract] done={done_count}/{len(new_file_rows)} total={total_files}"
                        )
        else:
            # Sequential path (original behaviour)
            for aid, row in new_file_rows:
                rel_path = str(row.get("path") or "").replace("\\", "/")
                filename = str(row.get("name") or "")
                size_b = _int_size(row.get("size_bytes"))

                fid, mime, display_name, err0 = _effective_file_for_row(service, row)

                if err0:
                    ev, pend = _metadata_only_evidence(
                        artifact_id=aid, rel_path=rel_path, filename=filename,
                        extension=Path(filename).suffix.lower().lstrip("."),
                        size_bytes=size_b, mtime_unix=_parse_drive_time_unix(row.get("modified_time")),
                        reason=err0, extra=_extra_cols_from_row(row),
                    )
                elif mime and is_binary_skip_mime(mime):
                    ev, pend = _metadata_only_evidence(
                        artifact_id=aid, rel_path=rel_path, filename=filename,
                        extension=Path(filename).suffix.lower().lstrip("."),
                        size_bytes=size_b, mtime_unix=_parse_drive_time_unix(row.get("modified_time")),
                        reason=f"binary_skip:{mime}", extra=_extra_cols_from_row(row),
                    )
                elif (
                    mime in ("application/vnd.google-apps.spreadsheet", "text/csv", "text/tab-separated-values")
                    and size_b is not None
                    and size_b > 200 * 1024
                ):
                    ev, pend = _metadata_only_evidence(
                        artifact_id=aid, rel_path=rel_path, filename=filename,
                        extension=Path(filename).suffix.lower().lstrip("."),
                        size_bytes=size_b, mtime_unix=_parse_drive_time_unix(row.get("modified_time")),
                        reason="large_sheet_skip", extra=_extra_cols_from_row(row),
                    )
                else:
                    suffix = suggested_local_suffix(mime or "", display_name or filename)
                    tmp = td_path / f"a{aid}{suffix}"
                    err = fetch_drive_file_to_path(
                        service,
                        file_id=fid,
                        mime_type=mime or "",
                        display_name=display_name or filename,
                        dest=tmp,
                        max_export_bytes=snippet_export_bytes if snippet_export_bytes > 0 else None,
                    )
                    if err or not tmp.is_file() or tmp.stat().st_size == 0:
                        ev, pend = _metadata_only_evidence(
                            artifact_id=aid, rel_path=rel_path, filename=filename,
                            extension=Path(filename).suffix.lower().lstrip("."),
                            size_bytes=size_b, mtime_unix=_parse_drive_time_unix(row.get("modified_time")),
                            reason=err or "empty_download", extra=_extra_cols_from_row(row),
                        )
                    else:
                        ev, pend = _build_evidence_from_temp_file(
                            temp_path=tmp, rel_path=rel_path, filename=filename,
                            size_bytes=size_b, mtime_unix=_parse_drive_time_unix(row.get("modified_time")),
                            artifact_id=aid, extra=_extra_cols_from_row(row),
                        )
                    try:
                        tmp.unlink(missing_ok=True)
                    except Exception:
                        pass

                new_results[aid] = (ev, pend)
                append_drive_artifact_checkpoint(arti_path, aid, ev)

                if aid % ingest_log_every == 0 and progress_log:
                    progress_log(
                        f"[extract] artifact_id={aid}/{total_files} fn={filename[:80]!r} "
                        f"mime={mime or 'n/a'} size={_human_size(size_b)}"
                    )

    # -- Merge existing + new in artifact_id order --
    for aid, row in enumerate(file_rows, start=1):
        rel_path = str(row.get("path") or "").replace("\\", "/")
        filename = str(row.get("name") or "")
        extra = _extra_cols_from_row(row)

        if aid in existing_rows:
            ev = existing_rows[aid].copy()
            ev.setdefault("drive_url", extra.get("drive_url", ""))
            ev.setdefault("owner_email", extra.get("owner_email", ""))
            ev.setdefault("last_modified_by", extra.get("last_modified_by", ""))
            ev.setdefault("llm_pass", "")
            for k in ("bucket_number", "bucket_name", "confidence", "rationale"):
                ev.setdefault(k, "")
            if aid in existing_class:
                c = existing_class[aid]
                ev["bucket_number"] = c.get("bucket_number", "")
                ev["bucket_name"] = c.get("bucket_name", "")
                ev["confidence"] = c.get("confidence", "")
                ev["rationale"] = c.get("rationale", "")
                ev["llm_pass"] = c.get("llm_pass", 1)
            else:
                desc = (
                    f"{ev.get('filename')} ({ev.get('extension') or 'noext'})\n"
                    f"{ev.get('content_summary', '')}\n{ev.get('snippet', '')}"
                )[:12000]
                pending_classify.append({
                    "row_id": aid,
                    "description": desc,
                    "source": ev.get("source_guess") or "Google Drive",
                    "filename": ev.get("filename") or "",
                    "extension": ev.get("extension") or "",
                    "path_hint": rel_path,
                    "snippet": str(ev.get("snippet") or "")[:800],
                })
            evidence_rows.append(ev)
            if aid % ingest_log_every == 0 and progress_log:
                progress_log(f"[resume] artifact_id={aid} fn={filename[:80]!r}")
        elif aid in new_results:
            ev, pend = new_results[aid]
            existing_rows[aid] = ev
            evidence_rows.append(ev)
            pending_classify.append(pend)

    msg = f"[extract] done. evidence_rows={len(evidence_rows)} pending_classify={len(pending_classify)}"
    print(msg, flush=True)
    if progress_log:
        progress_log(msg)

    # -----------------------------------------------------------------------
    # Phase 3: Pass-1 classification (fast model, large batches, all pending)
    # -----------------------------------------------------------------------
    if pending_classify:
        msg = f"[pass1] classifying {len(pending_classify)} items with {_pass1_model} batch={pass1_batch_size}"
        print(msg, flush=True)
        if progress_log:
            progress_log(msg)

        pass1_results = _run_classify_pass(
            pending_classify,
            model=_pass1_model,
            batch_size=pass1_batch_size,
            pass_num=1,
            ckpt_path=ckpt_path,
            evidence_rows=evidence_rows,
            progress_log=progress_log,
            workers=workers,
        )
        existing_class.update(pass1_results)

    # -----------------------------------------------------------------------
    # Phase 4: Pass-2 classification (full model, low-confidence only)
    # -----------------------------------------------------------------------
    low_conf_pending: list[dict[str, Any]] = []
    for aid_int, c in existing_class.items():
        if str(c.get("confidence", "")).strip().lower() == "low" and c.get("llm_pass", 1) == 1:
            idx = aid_int - 1
            if 0 <= idx < len(evidence_rows):
                ev = evidence_rows[idx]
                desc = (
                    f"{ev.get('filename')} ({ev.get('extension') or 'noext'})\n"
                    f"{ev.get('content_summary', '')}\n{ev.get('snippet', '')}"
                )[:12000]
                low_conf_pending.append({
                    "row_id": aid_int,
                    "description": desc,
                    "source": ev.get("source_guess") or "Google Drive",
                    "filename": ev.get("filename") or "",
                    "extension": ev.get("extension") or "",
                    "path_hint": str(ev.get("path") or ""),
                    "snippet": str(ev.get("snippet") or "")[:800],
                })

    if low_conf_pending:
        msg = (
            f"[pass2] re-classifying {len(low_conf_pending)} low-confidence items "
            f"with {pass2_model} batch={pass2_batch_size}"
        )
        print(msg, flush=True)
        if progress_log:
            progress_log(msg)

        pass2_results = _run_classify_pass(
            low_conf_pending,
            model=pass2_model,
            batch_size=pass2_batch_size,
            pass_num=2,
            ckpt_path=ckpt_path,
            evidence_rows=evidence_rows,
            progress_log=progress_log,
            workers=workers,
        )
        existing_class.update(pass2_results)
    else:
        print("[pass2] no low-confidence items to re-classify", flush=True)

    # -----------------------------------------------------------------------
    # Apply final classifications to evidence_df
    # -----------------------------------------------------------------------
    evidence_df = pd.DataFrame(evidence_rows)

    def _apply_cls(col: str, default: Any = "") -> pd.Series:
        return evidence_df["artifact_id"].map(
            lambda aid: existing_class.get(int(aid), {}).get(col, default)
        )

    evidence_df["bucket_number"] = _apply_cls("bucket_number")
    evidence_df["bucket_name"] = _apply_cls("bucket_name")
    evidence_df["confidence"] = _apply_cls("confidence")
    evidence_df["rationale"] = _apply_cls("rationale")
    evidence_df["llm_pass"] = _apply_cls("llm_pass", "")

    # -----------------------------------------------------------------------
    # Phase 5: Enrich — subcategory, content_type, quality_tier
    # -----------------------------------------------------------------------
    evidence_df = attach_subcategories(
        evidence_df,
        model=enrich_model,
        batch_size=pass2_batch_size,
        output_path_for_checkpoint=str(out_path),
        progress_log=progress_log,
        workers=workers,
    )

    def _s(col: str) -> pd.Series:
        if col not in evidence_df.columns:
            return pd.Series("", index=evidence_df.index)
        return evidence_df[col].fillna("").astype(str).replace({"nan": ""})

    evidence_df["content_type"] = [
        infer_content_types_cell(
            rationale=r, content_summary=cs, snippet=sn, filename=fn, path=ph,
        )
        for r, cs, sn, fn, ph in zip(
            _s("rationale"), _s("content_summary"), _s("snippet"), _s("filename"), _s("path"),
        )
    ]

    tok = pd.to_numeric(evidence_df.get("token_count", pd.Series(0, index=evidence_df.index)), errors="coerce").fillna(0).astype("int64")
    wct = pd.to_numeric(evidence_df.get("word_count", pd.Series(0, index=evidence_df.index)), errors="coerce").fillna(0).astype("int64")

    evidence_df["quality_tier"] = [
        infer_quality_tier(
            token_count=int(t), word_count=int(w), pii_flag=str(pi),
            modality=str(m), content_type=str(ct), extension=str(ex), confidence=str(cf),
        )
        for t, w, pi, m, ct, ex, cf in zip(
            tok, wct, _s("pii_flag"), _s("modality"), _s("content_type"), _s("extension"), _s("confidence"),
        )
    ]

    # -----------------------------------------------------------------------
    # Phase 6: Write XLSX
    # -----------------------------------------------------------------------
    inventory_df = summarize_inventory_from_evidence(evidence_df, model=enrich_model)

    write_company_inventory_workbook(
        inventory_df=inventory_df,
        evidence_df=evidence_df,
        output_path=str(out_path),
    )

    # Bucket distribution summary for the log
    bn = pd.to_numeric(evidence_df.get("bucket_number", pd.Series([], dtype=object)), errors="coerce")
    dist = bn.dropna().astype(int).value_counts().sort_index().to_dict()
    p1_count = int((evidence_df.get("llm_pass", pd.Series([], dtype=object)) == 1).sum())
    p2_count = int((evidence_df.get("llm_pass", pd.Series([], dtype=object)) == 2).sum())

    lines = [
        f"Wrote 1TB inventory to {out_path}",
        f"Evidence rows: {len(evidence_df)}",
        f"  Pass-1 ({_pass1_model}): {p1_count}",
        f"  Pass-2 ({pass2_model}):  {p2_count}",
        f"Bucket distribution: {dist}",
        f"Classification checkpoint: {ckpt_path}",
        f"Extract checkpoint (resume): {arti_path}",
    ]
    result = "\n".join(lines)
    print(result, flush=True)
    return result
