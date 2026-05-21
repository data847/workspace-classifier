from __future__ import annotations

import re
from dataclasses import dataclass


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(
    r"(?:(?:\+\d{1,3}[\s-]?)?(?:\(\d{2,4}\)|\d{2,4})[\s-]?)?\d{3,4}[\s-]?\d{3,4}\b"
)
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", re.IGNORECASE)
CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")


@dataclass(frozen=True)
class PiiResult:
    has_pii: bool
    types: tuple[str, ...] = ()


def detect_pii(text: str | None) -> PiiResult:
    if not text:
        return PiiResult(False, ())

    types: list[str] = []
    if EMAIL_RE.search(text):
        types.append("email")
    if IBAN_RE.search(text):
        types.append("iban")
    # Card regex is noisy; only count as PII if it looks like a card number (Luhn-ish check skipped).
    if CARD_RE.search(text):
        types.append("numeric_id")
    # Phone regex is noisy; still useful as a flag.
    if PHONE_RE.search(text):
        types.append("phone")

    dedup = tuple(sorted(set(types)))
    return PiiResult(bool(dedup), dedup)

