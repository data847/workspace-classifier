"""Select and download the top-N highest-quality files per LLM bucket.

Selection criteria (in order of priority):
  1. ``quality_tier``  — mapped to a numeric score (higher = better)
  2. ``word_count``    — tiebreaker (more content = richer sample)
  3. ``confidence``    — prefer high-confidence classifications

Files are downloaded into:
  out_dir/samples/bucket_<n>_<name>/
    top_01_filename.docx
    top_02_filename.pdf
    ...
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd

from dump.full_download import (
    _GOOGLE_EXPORT_MIME,
    _backoff,
    _bucket_folder_name,
    _download_one,
    _is_binary,
    _safe_name,
)

# ---------------------------------------------------------------------------
# Quality tier scoring
# ---------------------------------------------------------------------------

_QUALITY_SCORE: dict[str, int] = {
    "high":   3,
    "medium": 2,
    "low":    1,
    "":       0,
}

_CONFIDENCE_SCORE: dict[str, int] = {
    "high":   3,
    "medium": 2,
    "low":    1,
    "":       0,
}


def _quality_score(tier: Any) -> int:
    return _QUALITY_SCORE.get(str(tier).strip().lower(), 0)


def _confidence_score(conf: Any) -> int:
    return _CONFIDENCE_SCORE.get(str(conf).strip().lower(), 0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def select_and_download_samples(
    service: Any,
    evidence_df: pd.DataFrame,
    *,
    out_dir: Path,
    top_n: int = 10,
    log: Any = None,
) -> dict[str, int]:
    """Select top ``top_n`` files per bucket and download them.

    Parameters
    ----------
    service
        Authenticated Drive API v3 service.
    evidence_df
        DataFrame with columns including ``bucket_number``, ``bucket_name``,
        ``quality_tier``, ``word_count``, ``confidence``,
        ``drive_file_id``, ``filename``, ``mime_type``.
    out_dir
        Root output directory. Samples written to
        ``out_dir/samples/bucket_<n>_<name>/``.
    top_n
        Number of samples to select per bucket (default: 10).
    log
        Optional callable for progress messages.

    Returns
    -------
    dict mapping bucket folder name → number of samples downloaded.
    """
    samples_dir = out_dir / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)

    def _log(msg: str) -> None:
        if log:
            log(msg)

    df = evidence_df.copy()

    # Add sort keys
    df["_q_score"]    = df.get("quality_tier", pd.Series("", index=df.index)).apply(_quality_score)
    df["_conf_score"] = df.get("confidence",   pd.Series("", index=df.index)).apply(_confidence_score)
    df["_word_count"] = pd.to_numeric(df.get("word_count", pd.Series(0, index=df.index)), errors="coerce").fillna(0)

    # Exclude rows without a bucket or file ID
    df = df[df.get("bucket_number", pd.Series("", index=df.index)).notna()]
    df = df[df.get("drive_file_id", pd.Series("", index=df.index)).astype(str).str.strip() != ""]

    # Sort: quality desc → confidence desc → word_count desc
    df = df.sort_values(
        ["_q_score", "_conf_score", "_word_count"],
        ascending=[False, False, False],
    )

    counts: dict[str, int] = {}

    for (bnum, bname), group in df.groupby(["bucket_number", "bucket_name"], sort=False):
        bucket_folder = _bucket_folder_name(bnum, bname)
        dest_dir = samples_dir / bucket_folder
        dest_dir.mkdir(parents=True, exist_ok=True)
        counts[bucket_folder] = 0

        top = group.head(top_n)
        _log(f"[samples] bucket {bucket_folder}: selecting {len(top)} of {len(group)} files")

        for rank, row in enumerate(top.itertuples(index=False), 1):
            file_id  = str(getattr(row, "drive_file_id", "") or "")
            mime     = str(getattr(row, "mime_type", "") or "")
            filename = str(getattr(row, "filename",  "") or "file")

            if not file_id or _is_binary(mime):
                # Write a stub for binary/missing files
                stub_name = f"top_{rank:02d}_{_safe_name(filename)}.stub.txt"
                (dest_dir / stub_name).write_text(
                    f"[binary or missing — not downloaded]\nfilename: {filename}\nmime: {mime}\n",
                    encoding="utf-8",
                )
                counts[bucket_folder] += 1
                continue

            export_info = _GOOGLE_EXPORT_MIME.get(mime)
            if export_info:
                _, ext = export_info
                base = Path(filename).stem
            else:
                base = Path(filename).stem
                ext  = Path(filename).suffix or ".bin"

            dest = dest_dir / f"top_{rank:02d}_{_safe_name(base)}{ext}"

            err = _download_one(
                service,
                file_id=file_id,
                mime_type=mime,
                display_name=filename,
                dest=dest,
            )
            if err:
                _log(f"[samples] warn: {filename} — {err}")
                stub = dest_dir / f"top_{rank:02d}_{_safe_name(base)}.error.txt"
                stub.write_text(f"download_error: {err}\nfile_id: {file_id}\n", encoding="utf-8")

            counts[bucket_folder] += 1

    total = sum(counts.values())
    _log(f"[samples] done — {total} samples across {len(counts)} buckets → {samples_dir}")
    return counts
