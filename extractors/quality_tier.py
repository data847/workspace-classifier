"""Heuristic quality-tier assignment (High/Medium/Low) for LLM training usefulness."""

from __future__ import annotations

HIGH_TOKEN_MIN = 8_000
MEDIUM_TOKEN_MIN = 1_500

# Near-empty extract (often OCR-less images or broken reads).
TINY_TOKEN_MAX = 80

# Modalities that imply little usable text for training without extra processing.
IMAGE_ONLY_MODALITIES = frozenset({"image"})
AUDIO_VIDEO_MODALITIES = frozenset({"audio_video"})

# Content-type labels that usually carry richer training signal than raw grids/logs.
STRONG_CONTENT_TYPES = frozenset(
    {
        "Narrative",
        "Instructional",
        "Decision-log",
        "Conversational",
    }
)

# Extensions often dominated by layout/minified noise unless token count is high.
WEAK_SOLO_EXTENSIONS = frozenset({"html", "htm", "log"})

_STRONG_CT_ADJUST = 1  # score bump toward High
_WEAK_EXT_ADJUST = -1  # nudge toward Low for noisy formats when not huge

_TABULAR_EXTENSIONS = frozenset({"csv", "tsv", "xlsx", "xlsm", "xls", "json"})


def _parse_tags(cell: str) -> set[str]:
    if not cell or not str(cell).strip():
        return set()
    parts = [x.strip() for x in str(cell).replace(",", ";").split(";")]
    return {p for p in parts if p}


def _score_from_tokens(token_count: int) -> int:
    """Map tokens to 0=Low, 1=Medium, 2=High base tier."""
    if token_count >= HIGH_TOKEN_MIN:
        return 2
    if token_count >= MEDIUM_TOKEN_MIN:
        return 1
    return 0


def infer_quality_tier(
    *,
    token_count: int = 0,
    word_count: int = 0,
    pii_flag: str = "",
    modality: str = "",
    content_type: str = "",
    extension: str = "",
    confidence: str = "",
) -> str:
    """Return ``\"High\"``, ``\"Medium\"``, or ``\"Low\"``.

    The ``pii_flag`` argument is ignored; see module docstring.
    """

    tc = int(token_count) if token_count is not None else 0
    wc = int(word_count) if word_count is not None else 0
    mods = _parse_tags(modality)
    cts = _parse_tags(content_type)
    ext = (extension or "").lower().lstrip(".")
    conf = (confidence or "").strip().lower()

    # Image / AV assets without meaningful text extract.
    if mods and mods <= IMAGE_ONLY_MODALITIES and tc <= TINY_TOKEN_MAX:
        return "Low"
    if mods and mods <= AUDIO_VIDEO_MODALITIES and tc <= TINY_TOKEN_MAX:
        return "Low"

    if tc <= TINY_TOKEN_MAX and wc <= max(20, TINY_TOKEN_MAX // 2):
        return "Low"

    score = _score_from_tokens(tc)

    # Content-type nudges (training signal).
    if cts & STRONG_CONTENT_TYPES:
        score = min(2, score + _STRONG_CT_ADJUST)

    tabular_like = "tabular" in mods or ext in _TABULAR_EXTENSIONS
    if tabular_like and not (cts & STRONG_CONTENT_TYPES):
        score = max(0, score - 1)

    if ext in WEAK_SOLO_EXTENSIONS and tc < HIGH_TOKEN_MIN:
        score = max(0, score + _WEAK_EXT_ADJUST)

    labels = ("Low", "Medium", "High")
    tier = labels[score]

    if conf == "low" and tier == "High":
        tier = "Medium"

    return tier
