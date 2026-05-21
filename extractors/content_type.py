"""Infer training-oriented content-type labels from artifact evidence."""

from __future__ import annotations

from typing import Iterable

# Display labels (stable sort order).
CONTENT_TYPE_LABELS = (
    "Conversational",
    "Decision-log",
    "Factual",
    "Instructional",
    "Narrative",
    "Transactional",
)

# Substrings matched case-insensitively on word-ish boundaries where helpful.
_PATTERNS: dict[str, tuple[str, ...]] = {
    "Narrative": (
        "narrative",
        "essay",
        "memoir",
        "chronicle",
        "editorial",
        "long-form",
        "storytelling",
        "blog post",
        "whitepaper",
        "case study",
    ),
    "Conversational": (
        "conversational",
        "dialogue",
        "chat log",
        "slack",
        "transcript",
        "thread",
        "meeting",
        "zoom ",
        "teams meeting",
        "dm ",
        "direct message",
        "conversation",
    ),
    "Instructional": (
        "instructional",
        "tutorial",
        "how-to",
        "how to",
        "walkthrough",
        "onboarding",
        "training",
        "lesson",
        "workshop",
        "courseware",
        "syllabus",
        "exercise",
        "readme",
        "getting started",
        "step-by-step",
    ),
    "Factual": (
        "factual",
        "reference",
        "specification",
        "metrics",
        "statistics",
        "dataset",
        "schema",
        "dictionary",
        "api reference",
        "lookup",
        "tabular",
        "spreadsheet",
    ),
    "Decision-log": (
        "decision log",
        "decision record",
        "architecture decision",
        " adr",
        "adr ",
        "postmortem",
        "post-mortem",
        "incident",
        "rca",
        "root cause",
        "retrospective",
        "retro ",
        "approval",
        "signed off",
        "rfc",
        "design review",
    ),
    "Transactional": (
        "transactional",
        "invoice",
        "receipt",
        "purchase order",
        "payment",
        "billing",
        "ledger",
        "accounting",
        "order id",
        "transaction id",
        "payroll",
    ),
}


def infer_content_types(
    *,
    rationale: str = "",
    content_summary: str = "",
    snippet: str = "",
    filename: str = "",
    path: str = "",
) -> list[str]:
    """Return sorted content-type labels inferred from text cues.

    Rationale is included twice so classifier explanations weigh heavily.
    """
    r = (rationale or "").strip()
    pieces: list[str] = []
    if r:
        pieces.extend([r, r])
    for s in (content_summary, snippet, filename, path):
        if s:
            pieces.append(str(s))
    blob = "\n".join(pieces).lower()

    found: set[str] = set()
    for label, needles in _PATTERNS.items():
        for n in needles:
            nlow = n.lower().strip()
            if nlow and nlow in blob:
                found.add(label)
                break

    if not found:
        return ["Factual"]

    return sorted(found, key=lambda x: CONTENT_TYPE_LABELS.index(x))


def format_content_types_cell(labels: Iterable[str]) -> str:
    return "; ".join(labels)


def infer_content_types_cell(**kwargs: str) -> str:
    """Convenience: infer + format for one evidence row."""
    return format_content_types_cell(infer_content_types(**kwargs))
