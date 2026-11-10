"""Personal-data patterns shared by ingestion and the PII guard.

Phase 3 uses them to keep personal data out of the indexes (a chunk the assistant can quote
must never carry a phone number or email). The Phase 4 PII guard (`pii_guard.py`) builds on
the same patterns and adds context-dependent ones (OTPs, account and folio numbers).

Deliberately not matched: toll-free service lines (1800 ...), published by SEBI/AMFI as
public helplines, and short numeric codes such as AMFI scheme codes or PIN codes.
"""

import re

PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"),
    "pan": re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"),
    # 12 digits, first 2-9, optionally grouped 4-4-4
    "aadhaar": re.compile(r"(?<![\d.,])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![\d.,])"),
    # Indian mobile: optional +91/0 prefix, 10 digits starting 6-9, optionally split 5-5
    "phone": re.compile(r"(?<![\d.,])(?:\+91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}(?![\d.,])"),
}


def find_pii_kinds(text: str) -> list[str]:
    """Kinds of personal data present in `text` (never the matched values)."""
    return [kind for kind, pattern in PII_PATTERNS.items() if pattern.search(text)]
