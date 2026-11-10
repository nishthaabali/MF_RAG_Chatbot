"""Step 1 of the online pipeline: block messages that carry personal data (architecture §6.1).

Runs before the message is logged, stored, classified or sent anywhere. The result says only
*which kinds* of personal data were found, never the matched text, so nothing downstream can
leak it. The guard errs on the side of blocking: a false positive costs one re-typed question,
a false negative is a privacy breach (docs/edge-case.md §4).

Covered: PAN, Aadhaar, Indian phone numbers, email (also spelt out "x at y dot com"), UPI IDs,
OTPs / PINs (digits near "OTP", "code", "pin"), account / folio / card numbers (digit runs near
those words), masked identifiers ("a/c ****5678") and bare digit runs of 9+ digits.

Not blocked: amounts ("₹1000 SIP", "₹10,00,000"), index names ("NIFTY 100", "BSE 500"), years
and dates, the words "PAN" / "OTP" / "folio" without a value, ISINs, 6-digit AMFI scheme codes.
"""

import re
from dataclasses import dataclass

from app.pipeline.pii_patterns import PII_PATTERNS
from app.pipeline.text import normalise_unicode

WITHHELD_PLACEHOLDER = "[message withheld: contained personal information]"

# A digit run is bounded by anything except another digit or a "," / "." joining more digits
# ("9,876", "16.97"), so "call 9876543210, thanks" still matches.
_DIGITS_BEFORE = r"(?<!\d)(?<!\d[.,])"
_DIGITS_AFTER = r"(?![.,]?\d)"
_AMOUNT_PREFIX = re.compile(r"(?:₹|\brs\.?|\binr)\s*$", re.I)

# Patterns that identify personal data on their own.
_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "pan": [
        # Compact form in any case: "ABCDE1234F", "abcde1234f".
        re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{5}\d{4}[A-Za-z](?![A-Za-z0-9])"),
        # Separated form "ABCDE 1234 F" / "ABCDE-1234-F": upper case only, so ordinary words
        # ("since 2013 a") are not mistaken for a PAN.
        re.compile(r"(?<![A-Za-z0-9])[A-Z]{5}[ -]\d{4}[ -]?[A-Z](?![A-Za-z0-9])"),
        re.compile(r"(?<![A-Za-z0-9])[A-Z]{5}[ -]?\d{4}[ -][A-Z](?![A-Za-z0-9])"),
    ],
    "aadhaar": [
        re.compile(_DIGITS_BEFORE + r"\d{4}[ -]\d{4}[ -]\d{4}" + _DIGITS_AFTER),
        re.compile(_DIGITS_BEFORE + r"\d{12}" + _DIGITS_AFTER),
    ],
    "phone": [
        re.compile(_DIGITS_BEFORE + r"(?:\+91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}" + _DIGITS_AFTER),
        re.compile(_DIGITS_BEFORE + r"(?:\+?91[ -]?)?[6-9]\d{2}[ -]\d{3}[ -]\d{4}" + _DIGITS_AFTER),
        re.compile(_DIGITS_BEFORE + r"\+?91[ -]?[6-9]\d{9}" + _DIGITS_AFTER),
    ],
    "email": [
        PII_PATTERNS["email"],
        # Spelt out: "me at gmail dot com", "me[at]gmail[dot]com", "me (at) gmail (dot) com"
        re.compile(
            r"\b[\w.+-]+(?:\s*[\[({]\s*at\s*[\])}]\s*|\s+at\s+)[\w-]+"
            r"(?:\s*[\[({]\s*dot\s*[\])}]\s*|\s+dot\s+)[a-z]{2,}\b",
            re.I,
        ),
    ],
    # UPI handle "name@okhdfcbank", "98765@ybl": an @ with no dotted domain after it.
    "upi": [re.compile(r"(?<![\w.+-])[\w.-]{2,}@[a-z][a-z0-9]+\b(?!\.[a-z0-9])", re.I)],
}

# Values that are personal only when a keyword is nearby: (keyword pattern, value pattern).
_OTP_KEYWORD = re.compile(
    r"\b(?:otp|one[ -]?time[ -]?pass(?:word|code)?|passcode|m?pin|tpin|cvv|code)\b", re.I
)
_OTP_VALUE = re.compile(_DIGITS_BEFORE + r"\d{4,8}" + _DIGITS_AFTER)
_NOT_PERSONAL_CODE = re.compile(r"\b(?:scheme|amfi|fund|isin|product|plan|promo)\s*$", re.I)

_ACCOUNT_KEYWORD = re.compile(
    r"\b(?:account|acct|a/c|ac|folio|demat|dp\s*id|client\s*id|customer\s*id|card|"
    r"bank|ifsc|crn|ckyc|kyc\s*no)\b",
    re.I,
)
_ACCOUNT_VALUE = re.compile(_DIGITS_BEFORE + r"\d[\d/ -]{4,}\d" + _DIGITS_AFTER)

_MASK_KEYWORD = re.compile(
    r"\b(?:pan|aadhaa?r|account|acct|a/c|card|folio|phone|mobile|number|no)\b", re.I
)
_MASKED_VALUE = re.compile(r"(?:[xX*•#]{2,}[ -]?)+\d{2,}[A-Za-z]?")

_LONG_NUMBER = re.compile(_DIGITS_BEFORE + r"\d{9,}" + _DIGITS_AFTER)

_KIND_ORDER = (
    "pan", "aadhaar", "phone", "email", "upi", "otp", "account", "masked_id", "long_number",
)


@dataclass(frozen=True)
class PIIResult:
    blocked: bool
    kinds: tuple[str, ...] = ()  # kinds only; the matched text is never kept


def _near(keyword: re.Pattern[str], value: re.Pattern[str], text: str, after: int, before: int,
          skip=None) -> bool:
    for kw in keyword.finditer(text):
        if skip is not None and skip(text, kw):
            continue
        window_after = text[kw.end() : kw.end() + after]
        if value.search(window_after):
            return True
        window_before = text[max(0, kw.start() - before) : kw.start()]
        if before and value.search(window_before):
            return True
    return False


def _is_scheme_code(text: str, kw: re.Match[str]) -> bool:
    return kw.group().lower() == "code" and bool(_NOT_PERSONAL_CODE.search(text[: kw.start()]))


def _has_account_number(text: str) -> bool:
    for kw in _ACCOUNT_KEYWORD.finditer(text):
        window = text[kw.end() : kw.end() + 30]
        for m in _ACCOUNT_VALUE.finditer(window):
            digits = re.sub(r"\D", "", m.group())
            if len(digits) >= 6 and not _AMOUNT_PREFIX.search(window[: m.start()]):
                return True
    return False


def _has_long_number(text: str) -> bool:
    return any(
        not _AMOUNT_PREFIX.search(text[max(0, m.start() - 4) : m.start()])
        for m in _LONG_NUMBER.finditer(text)
    )


_JOINED_DIGITS = re.compile(r"(?<=\d)[ -](?=\d)")


def check_pii(text: str) -> PIIResult:
    """Scan a user message. Returns only whether it is blocked and the kinds found."""
    t = normalise_unicode(text)
    found = {kind for kind, patterns in _PATTERNS.items() if any(p.search(t) for p in patterns)}
    # A phone number grouped any other way ("9876 543210"): match it with separators removed.
    if any(p.search(_JOINED_DIGITS.sub("", t)) for p in _PATTERNS["phone"]):
        found.add("phone")
    if _near(_OTP_KEYWORD, _OTP_VALUE, t, after=25, before=20, skip=_is_scheme_code):
        found.add("otp")
    if _has_account_number(t):
        found.add("account")
    if _near(_MASK_KEYWORD, _MASKED_VALUE, t, after=25, before=0):
        found.add("masked_id")
    if _has_long_number(t):
        found.add("long_number")
    kinds = tuple(k for k in _KIND_ORDER if k in found)
    return PIIResult(blocked=bool(kinds), kinds=kinds)
