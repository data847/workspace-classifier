from __future__ import annotations

import env_loader 

import json
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

_log = logging.getLogger(__name__)

import pandas as pd

from llm_provider import call_forced_tool, default_llm_model, is_retryable_llm_error
from prompt import SEGMENTATION_SYSTEM_PROMPT

DEFAULT_MODEL = default_llm_model()
DEFAULT_BATCH_SIZE = int(os.environ.get("SEGMENTER_BATCH_SIZE", "25"))
DEFAULT_MAX_TOKENS = 4096
DEFAULT_SNIPPET_CHARS = int(os.environ.get("SEGMENTER_SNIPPET_CHARS", "1200"))
DEFAULT_MAX_RETRIES = int(os.environ.get("SEGMENTER_MAX_RETRIES", "8"))
DEFAULT_RETRY_BASE_SECONDS = float(os.environ.get("SEGMENTER_RETRY_BASE_SECONDS", "0.75"))
DEFAULT_RETRY_MAX_SECONDS = float(os.environ.get("SEGMENTER_RETRY_MAX_SECONDS", "90"))

CLASSIFY_TOOL: dict[str, Any] = {
    "name": "record_classifications",
    "description": (
        "Record the bucket assignment for every inventory item in the batch. "
        "Return one classification per input row, preserving row_id exactly."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "row_id": {"type": "integer"},
                        "bucket_number": {"type": "integer", "minimum": 1, "maximum": 7},
                        "bucket_name": {"type": "string"},
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "row_id",
                        "bucket_number",
                        "bucket_name",
                        "confidence",
                        "rationale",
                    ],
                },
            }
        },
        "required": ["classifications"],
    },
}


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _build_user_message(items: list[dict[str, Any]]) -> str:
    payload = json.dumps(items, ensure_ascii=False, indent=2)
    return (
        "Classify each of the following inventory items. Return one "
        "classification per item, preserving the row_id field exactly.\n\n"
        f"```json\n{payload}\n```"
    )


def _classify_batch(items: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    last_err: Exception | None = None
    tool_input: dict[str, Any] | None = None
    for attempt in range(DEFAULT_MAX_RETRIES + 1):
        try:
            tool_input = call_forced_tool(
                model=model,
                max_tokens=DEFAULT_MAX_TOKENS,
                system=SEGMENTATION_SYSTEM_PROMPT,
                tools=[CLASSIFY_TOOL],
                tool_name="record_classifications",
                user_content=_build_user_message(items),
            )
            break
        except Exception as e:
            if not is_retryable_llm_error(e):
                raise
            last_err = e
            if attempt >= DEFAULT_MAX_RETRIES:
                raise
            # Exponential backoff with jitter; allow longer sleeps for overload (529).
            base = DEFAULT_RETRY_BASE_SECONDS * (2**attempt)
            jitter = random.uniform(0.0, base * 0.25)
            sleep_s = min(DEFAULT_RETRY_MAX_SECONDS, base + jitter)
            _log.debug("classify_batch attempt %d/%d failed (%s), retrying in %.1fs", attempt + 1, DEFAULT_MAX_RETRIES + 1, type(e).__name__, sleep_s)
            time.sleep(sleep_s)
    else:
        # Defensive: loop should always break or raise.
        raise last_err or RuntimeError("Unknown classification retry failure")

    if tool_input is None:
        raise RuntimeError("Unknown classification retry failure")

    raw = tool_input.get("classifications")
    # Defensive: even with tool_choice, some models occasionally return
    # stringified JSON or mixed element types.
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception as e:
            raise RuntimeError(f"Tool returned non-JSON classifications string: {raw[:200]!r}") from e
    if isinstance(raw, dict) and "classifications" in raw:
        raw = raw.get("classifications")

    if not isinstance(raw, list):
        raise RuntimeError(f"Tool returned unexpected classifications type: {type(raw).__name__}")

    out: list[dict[str, Any]] = []
    for el in raw:
        if isinstance(el, dict):
            out.append(el)
            continue
        if isinstance(el, str):
            try:
                parsed = json.loads(el)
            except Exception:
                _log.debug("Skipping non-JSON string element in classifications: %r", el[:100])
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
    return out


def classify_items(
    items: list[dict[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    workers: int = 1,
) -> list[dict[str, Any]]:
    """Classify arbitrary items that contain at least row_id + description.

    Each item should look like:
      { "row_id": int, "description": str, "source": str | None, ... }
    Extra fields are allowed and will help the model if short.
    Workers > 1 sends batches in parallel for higher throughput.
    """
    normalized: list[dict[str, Any]] = []
    for it in items:
        row_id = int(it["row_id"])
        desc = str(it.get("description") or "")
        src = it.get("source")
        norm: dict[str, Any] = {"row_id": row_id, "description": _truncate(desc, DEFAULT_SNIPPET_CHARS)}
        if src:
            norm["source"] = str(src)
        for k in ("filename", "extension", "path_hint", "snippet"):
            if k in it and it[k]:
                norm[k] = _truncate(str(it[k]), 400)
        normalized.append(norm)

    batches = [normalized[i : i + batch_size] for i in range(0, len(normalized), batch_size)]

    if workers <= 1 or len(batches) <= 1:
        out: list[dict[str, Any]] = []
        for b in batches:
            out.extend(_classify_batch(b, model))
        return out

    results: list[list[dict[str, Any]]] = [[] for _ in batches]
    with ThreadPoolExecutor(max_workers=min(workers, len(batches))) as executor:
        futs = {executor.submit(_classify_batch, b, model): i for i, b in enumerate(batches)}
        for fut in as_completed(futs):
            results[futs[fut]] = fut.result()

    out = []
    for r in results:
        out.extend(r)
    return out


def classify_dataframe(
    df: pd.DataFrame,
    description_column: str,
    source_column: str | None,
    model: str = DEFAULT_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> pd.DataFrame:
    """Classify every row in df. Returns a copy with bucket columns appended."""
    if "row_id" not in df.columns:
        raise ValueError("DataFrame must have a row_id column. Use load_inventory().")

    records: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        rec: dict[str, Any] = {
            "row_id": int(row["row_id"]),
            "description": (
                str(row[description_column]) if pd.notna(row[description_column]) else ""
            ),
        }
        if source_column and source_column in df.columns:
            rec["source"] = (
                str(row[source_column]) if pd.notna(row[source_column]) else ""
            )
        records.append(rec)

    classifications = classify_items(records, model=model, batch_size=batch_size)

    by_id = {c["row_id"]: c for c in classifications}
    out = df.copy()
    out["bucket_number"] = out["row_id"].map(
        lambda rid: by_id.get(rid, {}).get("bucket_number")
    )
    out["bucket_name"] = out["row_id"].map(
        lambda rid: by_id.get(rid, {}).get("bucket_name")
    )
    out["confidence"] = out["row_id"].map(
        lambda rid: by_id.get(rid, {}).get("confidence")
    )
    out["rationale"] = out["row_id"].map(
        lambda rid: by_id.get(rid, {}).get("rationale")
    )

    return out
