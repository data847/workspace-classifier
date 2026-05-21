from __future__ import annotations

import json
import os
import random
import time
from collections import Counter, defaultdict
from typing import Any

import pandas as pd

from excel_io import BUCKETS
from llm_provider import call_forced_tool, is_retryable_llm_error


DEFAULT_SUMMARY_MODEL = None  # if None, caller should pass DEFAULT_MODEL from segmenter
DEFAULT_SUMMARY_MAX_TOKENS = 1400
DEFAULT_SUMMARY_MAX_RETRIES = int(os.environ.get("SEGMENTER_SUMMARY_MAX_RETRIES", "8"))
DEFAULT_SUMMARY_RETRY_BASE_SECONDS = float(
    os.environ.get("SEGMENTER_SUMMARY_RETRY_BASE_SECONDS", "0.75")
)
DEFAULT_SUMMARY_RETRY_MAX_SECONDS = float(
    os.environ.get("SEGMENTER_SUMMARY_RETRY_MAX_SECONDS", "90")
)


SUMMARY_TOOL: dict[str, Any] = {
    "name": "record_bucket_summaries",
    "description": "Return exactly one summary row per bucket (1-7).",
    "input_schema": {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "bucket_number": {"type": "integer", "minimum": 1, "maximum": 7},
                        "category": {"type": "string"},
                        "what_it_includes": {"type": "string"},
                        "where_to_find_it": {"type": "string"},
                        "how_hard_to_pull": {"type": "string"},
                    },
                    "required": [
                        "bucket_number",
                        "category",
                        "what_it_includes",
                        "where_to_find_it",
                        "how_hard_to_pull",
                    ],
                },
            }
        },
        "required": ["rows"],
    },
}


def _bucket_name(num: int) -> str:
    for n, name in BUCKETS:
        if n == num:
            return name
    return str(num)


def _effort_estimate(bucket_df: pd.DataFrame) -> str:
    """Heuristic: map evidence signals to Low/Medium/High."""
    if len(bucket_df) == 0:
        return "Unknown — no evidence found in dump"

    exts = Counter(str(x).lower() for x in bucket_df.get("extension", pd.Series([], dtype=str)).fillna(""))
    sources = Counter(str(x) for x in bucket_df.get("source_guess", pd.Series([], dtype=str)).fillna(""))

    has_structured = any(exts.get(e, 0) for e in ("csv", "json", "xlsx"))
    has_many_pdfs = sum(exts.get(e, 0) for e in ("pdf",)) >= max(10, len(bucket_df) // 3)
    has_export_markers = any(sources.get(s, 0) for s in ("Slack", "Notion", "Jira", "GitHub", "Google Drive"))

    if has_structured or has_export_markers:
        if has_many_pdfs:
            return "Medium — mix of exports + many document PDFs"
        return "Low — structured exports / system dumps present"
    if has_many_pdfs:
        return "Medium — mostly document artifacts (likely manual cleanup)"
    return "Medium — unstructured dump; may require manual interpretation"


def _top_paths(paths: list[str], *, k: int = 8) -> list[str]:
    # Keep higher-level folder prefixes for readability.
    prefixes = []
    for p in paths:
        parts = p.replace("\\", "/").split("/")
        prefixes.append("/".join(parts[-4:-1]) if len(parts) >= 4 else "/".join(parts[:-1]))
    c = Counter([x for x in prefixes if x])
    return [p for p, _ in c.most_common(k)]


def summarize_inventory_from_evidence(
    evidence_df: pd.DataFrame,
    *,
    model: str,
) -> pd.DataFrame:
    """Return the desired 7-row inventory table as a DataFrame."""
    # Build compact per-bucket evidence summary for the LLM.
    per_bucket: dict[int, dict[str, Any]] = {}
    for num, name in BUCKETS:
        bdf = evidence_df[evidence_df.get("bucket_number") == num]
        sources = sorted({str(x) for x in bdf.get("source_guess", []).dropna().tolist() if str(x).strip()})
        exts = Counter(str(x).lower() for x in bdf.get("extension", []).fillna("").tolist())
        paths = bdf.get("path", pd.Series([], dtype=str)).astype(str).tolist()
        per_bucket[num] = {
            "bucket_number": num,
            "category": name,
            "count": int(len(bdf)),
            "sources_inferred": sources[:20],
            "top_extensions": [e for e, _ in exts.most_common(12) if e],
            "top_locations": _top_paths(paths),
            "effort_hint": _effort_estimate(bdf),
            "sample_items": [
                {
                    "filename": str(r.get("filename", "")),
                    "source_guess": r.get("source_guess"),
                    "snippet": str(r.get("snippet", ""))[:300],
                }
                for _, r in bdf.head(8).iterrows()
            ],
        }

    system_prompt = (
        "You are generating a company-specific data inventory summary in a strict spreadsheet format.\n"
        "You will be given evidence of artifacts found in a company dump, already grouped into 7 categories.\n"
        "Your job: produce exactly 7 rows (one per category) with:\n"
        "- what_it_includes: summarize what is present (artifact types + themes), company-specific.\n"
        "- where_to_find_it: list likely systems AND the observed locations (folder hints), concise.\n"
        "- how_hard_to_pull: use the effort_hint unless evidence suggests otherwise.\n"
        "Keep each field compact but information-dense. Use semicolons to separate items.\n"
    )

    payload = json.dumps([per_bucket[n] for n, _ in BUCKETS], ensure_ascii=False, indent=2)
    user_msg = (
        "Evidence (one object per bucket):\n\n"
        f"```json\n{payload}\n```\n\n"
        "Return the final 7-row inventory table."
    )

    def fallback_inventory_df() -> pd.DataFrame:
        """Deterministic fallback when the LLM tool output is missing/malformed."""
        out_rows: list[dict[str, str]] = []
        for num, name in BUCKETS:
            b = per_bucket.get(num, {})
            sources = b.get("sources_inferred") or []
            exts = b.get("top_extensions") or []
            locs = b.get("top_locations") or []
            effort = b.get("effort_hint") or "Unknown"

            what_parts: list[str] = []
            if exts:
                what_parts.append("Artifact types: " + ", ".join(exts[:8]))
            if b.get("count") is not None:
                what_parts.append(f"Observed items: {int(b.get('count') or 0)}")
            if b.get("sample_items"):
                sample_names = [
                    str(x.get("filename", "")).strip()
                    for x in (b.get("sample_items") or [])
                    if str(x.get("filename", "")).strip()
                ][:4]
                if sample_names:
                    what_parts.append("Examples: " + "; ".join(sample_names))

            where_parts: list[str] = []
            if sources:
                where_parts.append("Systems: " + ", ".join(sources[:8]))
            if locs:
                where_parts.append("Folders: " + "; ".join(locs[:8]))

            out_rows.append(
                {
                    "Category": name,
                    "What it includes": "; ".join(what_parts).strip(),
                    "Where to find it": "; ".join(where_parts).strip(),
                    "How hard to pull": str(effort).strip(),
                }
            )

        return pd.DataFrame(out_rows)

    tool_input: dict[str, Any] | None = None
    last_err: Exception | None = None
    for attempt in range(DEFAULT_SUMMARY_MAX_RETRIES + 1):
        try:
            tool_input = call_forced_tool(
                model=model,
                max_tokens=DEFAULT_SUMMARY_MAX_TOKENS,
                system=system_prompt,
                tools=[SUMMARY_TOOL],
                tool_name="record_bucket_summaries",
                user_content=user_msg,
            )
            break
        except Exception as e:
            if not is_retryable_llm_error(e):
                raise
            last_err = e
            if attempt >= DEFAULT_SUMMARY_MAX_RETRIES:
                raise
            base = DEFAULT_SUMMARY_RETRY_BASE_SECONDS * (2**attempt)
            jitter = random.uniform(0.0, base * 0.25)
            time.sleep(min(DEFAULT_SUMMARY_RETRY_MAX_SECONDS, base + jitter))
    else:
        raise last_err or RuntimeError("Unknown summary retry failure")

    if tool_input is None:
        return fallback_inventory_df()

    tool_rows: list[dict[str, Any]] | None = None
    tool_input_dict = tool_input
    if isinstance(tool_input_dict, dict):
        candidate = None
        for key in ("rows", "summaries", "bucket_rows", "items"):
            if key in tool_input_dict:
                candidate = tool_input_dict.get(key)
                break

        if candidate is None:
            return fallback_inventory_df()
        if not isinstance(candidate, list):
            return fallback_inventory_df()

        tool_rows = list(candidate)

    if not tool_rows:
        return fallback_inventory_df()

    # Normalize into the desired columns.
    by_num = {int(r["bucket_number"]): r for r in tool_rows}
    out_rows = []
    for num, name in BUCKETS:
        r = by_num.get(num)
        if not r:
            out_rows.append(
                {
                    "Category": name,
                    "What it includes": "",
                    "Where to find it": "",
                    "How hard to pull": "",
                }
            )
            continue
        out_rows.append(
            {
                "Category": r.get("category") or _bucket_name(num),
                "What it includes": r.get("what_it_includes", ""),
                "Where to find it": r.get("where_to_find_it", ""),
                "How hard to pull": r.get("how_hard_to_pull", ""),
            }
        )

    return pd.DataFrame(out_rows)

