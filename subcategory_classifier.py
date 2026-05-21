from __future__ import annotations

import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

import pandas as pd

from checkpoint import append_checkpoint, load_checkpoint
from excel_io import BUCKETS
from segmenter import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_RETRY_BASE_SECONDS,
    DEFAULT_RETRY_MAX_SECONDS,
)
from llm_provider import call_forced_tool, is_retryable_llm_error, llm_api_configured
from subcategory_taxonomy import labels_for_bucket

SUBCATEGORY_SYSTEM = """You assign **sub-categories** for data-inventory artifacts.

Rules:
- Each artifact already has a **bucket** (1–7) and a short **rationale** from an earlier classifier.
- For each artifact you MUST choose **exactly one** sub-category string from the **allowed list** provided in the user message for that batch.
- Base your choice primarily on the **rationale**; use filename and snippet only as supporting hints.
- Copy the sub-category text **verbatim** (including parentheses) from the allowed list.
- If nothing fits well, use **Other / General** when present in that list.

Do not invent new labels. Do not combine buckets."""

SUBCATEGORY_TOOL: dict[str, Any] = {
    "name": "record_subcategories",
    "description": (
        "Return exactly one sub_category per row_id. Labels must match the batch allowed list verbatim."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "row_id": {"type": "integer"},
                        "sub_category": {"type": "string"},
                    },
                    "required": ["row_id", "sub_category"],
                },
            }
        },
        "required": ["items"],
    },
}


def _bucket_display_name(num: int) -> str:
    for n, name in BUCKETS:
        if n == num:
            return name
    return str(num)


def _normalize_label(raw: str, allowed: frozenset[str]) -> str:
    s = (raw or "").strip()
    if s in allowed:
        return s
    # tolerate minor whitespace / newline drift
    for a in allowed:
        if s.lower() == a.lower():
            return a
    # fallback
    for a in allowed:
        if a.startswith("Other"):
            return a
    return next(iter(allowed)) if allowed else ""


def _parse_tool_items(tool_input: dict[str, Any]) -> list[dict[str, Any]]:
    raw = tool_input.get("items")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            _log.warning("Failed to parse tool items as JSON; returning empty: %r", raw[:200])
            raw = []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for el in raw:
        if isinstance(el, dict):
            out.append(el)
    return out


def _call_subcategory_batch(
    *,
    bucket_num: int,
    payload_items: list[dict[str, Any]],
    model: str,
) -> list[dict[str, Any]]:
    labels = labels_for_bucket(bucket_num)
    if not labels:
        return []
    allowed = frozenset(labels)
    numbered = "\n".join(f"{i + 1}. {lab}" for i, lab in enumerate(labels))
    bn_name = _bucket_display_name(bucket_num)
    user_msg = (
        f"Bucket {bucket_num} — {bn_name}.\n\n"
        f"Allowed sub-categories (you MUST copy one of these strings exactly for each row):\n"
        f"{numbered}\n\n"
        "Artifacts to label:\n```json\n"
        f"{json.dumps(payload_items, ensure_ascii=False, indent=2)}\n```"
    )

    last_err: Exception | None = None
    tool_input: dict[str, Any] | None = None
    for attempt in range(DEFAULT_MAX_RETRIES + 1):
        try:
            tool_input = call_forced_tool(
                model=model,
                max_tokens=DEFAULT_MAX_TOKENS,
                system=SUBCATEGORY_SYSTEM,
                tools=[SUBCATEGORY_TOOL],
                tool_name="record_subcategories",
                user_content=user_msg,
            )
            break
        except Exception as e:
            if not is_retryable_llm_error(e):
                raise
            last_err = e
            if attempt >= DEFAULT_MAX_RETRIES:
                raise
            base = DEFAULT_RETRY_BASE_SECONDS * (2**attempt)
            jitter = random.uniform(0.0, base * 0.25)
            sleep_s = min(DEFAULT_RETRY_MAX_SECONDS, base + jitter)
            _log.debug("subcategory_batch bucket=%d attempt %d/%d failed (%s), retrying in %.1fs", bucket_num, attempt + 1, DEFAULT_MAX_RETRIES + 1, type(e).__name__, sleep_s)
            time.sleep(sleep_s)
    else:
        raise last_err or RuntimeError("subcategory batch failure")

    if tool_input is None:
        raise RuntimeError("subcategory batch failure")

    raw_items = _parse_tool_items(tool_input)
    cleaned: list[dict[str, Any]] = []
    for row in raw_items:
        rid_raw = row.get("row_id")
        if rid_raw is None:
            _log.debug("Skipping subcategory row missing row_id: %r", row)
            continue
        try:
            rid = int(rid_raw)
        except Exception:
            _log.debug("Skipping subcategory row with non-integer row_id: %r", rid_raw)
            continue
        sub = _normalize_label(str(row.get("sub_category", "")), allowed)
        cleaned.append({"row_id": rid, "sub_category": sub})
    return cleaned


def subcategory_checkpoint_path(output_path: str) -> Path:
    """Sidecar checkpoint: ``<stem>.subcat.jsonl`` next to the workbook."""
    p = Path(output_path).expanduser().resolve()
    return p.parent / f"{p.stem}.subcat.jsonl"


def _fill_fallback_subcategories(out: pd.DataFrame) -> None:
    """Assign ``Other / General`` (last allowed label) when bucket exists but sub_category empty."""
    needs_fill = ~out["sub_category"].astype(str).str.strip().astype(bool)
    if not needs_fill.any():
        return
    # Pre-compute the fallback label per bucket number (at most 7 lookups).
    fallback_map = {bn: labs[-1] for bn in range(1, 8) if (labs := labels_for_bucket(bn))}
    bn_numeric = pd.to_numeric(out["bucket_number"], errors="coerce")
    fallback_series = bn_numeric.map(
        lambda bn: fallback_map.get(int(bn)) if pd.notna(bn) else None
    )
    fill_mask = needs_fill & fallback_series.notna()
    out.loc[fill_mask, "sub_category"] = fallback_series[fill_mask]


def attach_subcategories(
    evidence_df: pd.DataFrame,
    *,
    model: str = DEFAULT_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    checkpoint_path: Path | str | None = None,
    output_path_for_checkpoint: str | None = None,
    progress_log: Any = None,
    workers: int = 1,
) -> pd.DataFrame:
    """Add ``sub_category`` column using rationale + bucket (API). Resumes from checkpoint."""
    if "artifact_id" not in evidence_df.columns:
        raise ValueError("evidence_df must have artifact_id")

    ck_path = (
        Path(checkpoint_path)
        if checkpoint_path is not None
        else (
            subcategory_checkpoint_path(output_path_for_checkpoint)
            if output_path_for_checkpoint
            else None
        )
    )

    existing: dict[int, dict[str, Any]] = load_checkpoint(ck_path) if ck_path else {}

    out = evidence_df.copy()
    if "sub_category" not in out.columns:
        out["sub_category"] = ""

    # Seed from checkpoint
    for i in out.index:
        aid = out.at[i, "artifact_id"]
        try:
            aid_i = int(aid)
        except Exception:
            continue
        prev = existing.get(aid_i, {}).get("sub_category")
        if prev:
            out.at[i, "sub_category"] = str(prev)

    client_ok = llm_api_configured()

    pending_rows: list[tuple[Any, int, int]] = []
    if client_ok:
        for i in out.index:
            try:
                aid = int(out.at[i, "artifact_id"])
            except Exception:
                continue
            if str(out.at[i, "sub_category"] or "").strip():
                continue
            bn = pd.to_numeric(out.at[i, "bucket_number"], errors="coerce")
            if pd.isna(bn):
                continue
            bn_i = int(bn)
            labs = labels_for_bucket(bn_i)
            if not labs:
                continue
            rationale = str(out.at[i, "rationale"] or "").strip()
            if not rationale:
                continue
            pending_rows.append((i, aid, bn_i))

    by_bucket: dict[int, list[tuple[Any, int]]] = {}
    for idx, aid, bn_i in pending_rows:
        by_bucket.setdefault(bn_i, []).append((idx, aid))

    total_pending = len(pending_rows)
    done_pending = 0

    if not client_ok:
        _fill_fallback_subcategories(out)
        return out

    # Pre-build all work items (read-only access to `out`, safe before threading)
    WorkItem = tuple  # (bn_i, payload_items, index_by_rid, chunk)
    work_items: list[WorkItem] = []
    for bn_i in sorted(by_bucket.keys()):
        pairs = by_bucket[bn_i]
        for start in range(0, len(pairs), batch_size):
            chunk = pairs[start : start + batch_size]
            payload_items: list[dict[str, Any]] = []
            index_by_rid: dict[int, Any] = {}
            for idx, aid in chunk:
                row = out.loc[idx]
                payload_items.append(
                    {
                        "row_id": aid,
                        "rationale": str(row.get("rationale", "") or "")[:2000],
                        "filename": str(row.get("filename", "") or "")[:400],
                        "snippet": str(row.get("snippet", "") or "")[:800],
                    }
                )
                index_by_rid[aid] = idx
            work_items.append((bn_i, payload_items, index_by_rid, chunk))

    lock = threading.Lock()

    def _run_batch(item: WorkItem) -> tuple:
        bn_i, payload_items, index_by_rid, chunk = item
        try:
            batch_out = _call_subcategory_batch(
                bucket_num=bn_i, payload_items=payload_items, model=model
            )
            return bn_i, batch_out, index_by_rid, chunk, None
        except Exception as e:
            return bn_i, None, index_by_rid, chunk, e

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futs = {executor.submit(_run_batch, item): item for item in work_items}
        for fut in as_completed(futs):
            bn_i, batch_out, index_by_rid, chunk, err = fut.result()
            labs_fb = labels_for_bucket(bn_i)
            fb = labs_fb[-1] if labs_fb else ""

            with lock:
                if err is not None or batch_out is None:
                    _log.warning("subcategory batch failed for bucket=%d (%s), using fallback label", bn_i, type(err).__name__ if err else "no output")
                    for idx, _aid in chunk:
                        out.at[idx, "sub_category"] = fb
                else:
                    ck_lines: list[dict[str, Any]] = []
                    for row in batch_out:
                        rid = row.get("row_id")
                        sub = row.get("sub_category", "")
                        if rid is None:
                            continue
                        try:
                            rid_i = int(rid)
                        except Exception:
                            _log.debug("Skipping batch result row with non-integer row_id: %r", rid)
                            continue
                        if rid_i in index_by_rid:
                            out.at[index_by_rid[rid_i], "sub_category"] = str(sub)
                        ck_lines.append({"row_id": rid_i, "sub_category": str(sub)})

                    for idx, _aid in chunk:
                        if not str(out.at[idx, "sub_category"] or "").strip():
                            out.at[idx, "sub_category"] = fb

                    if ck_path and ck_lines:
                        append_checkpoint(ck_path, ck_lines)

                done_pending += len(chunk)
                if progress_log and done_pending % (batch_size * 10) == 0:
                    progress_log(
                        f"[subcat] done={done_pending}/{total_pending} bucket={bn_i} model={model}"
                    )

    _fill_fallback_subcategories(out)
    return out
