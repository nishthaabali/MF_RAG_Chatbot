"""Value normalisation for scraped facts. Every helper returns None rather than guessing."""

import html
import re
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# "Nil" is deliberately NOT a placeholder: an exit load of "Nil" is a real published value.
PLACEHOLDERS = {"", "-", "--", "—", "–", "na", "n/a", "null"}
RISK_LEVELS = ["Low", "Low to Moderate", "Moderate", "Moderately High", "High", "Very High"]
MAX_EXPENSE_RATIO = 3.5  # percent; anything above is treated as a parse error

_ZERO_WIDTH = re.compile(r"[​-‏⁠﻿]")


def clean_text(value: object) -> str | None:
    """Collapse whitespace, strip zero-width characters, map placeholders to None."""
    if value is None:
        return None
    text = _ZERO_WIDTH.sub("", str(value)).replace("\xa0", " ")
    text = " ".join(text.split())
    if text.casefold() in PLACEHOLDERS:
        return None
    return text


def parse_percent(value: object) -> float | None:
    text = clean_text(value)
    if text is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(m.group()) if m else None


def expense_ratio(value: object) -> tuple[str | None, float | None]:
    """Return (display text, numeric) rounded to 2 decimals, as Groww displays it."""
    number = value if isinstance(value, (int, float)) else parse_percent(value)
    if number is None or not 0 < number <= MAX_EXPENSE_RATIO:
        return None, None
    rounded = round(float(number) + 1e-9, 2)
    return f"{rounded:.2f}%", rounded


def parse_amount(value: object) -> float | None:
    """Parse ₹/Rs/INR amounts including Indian digit grouping (₹1,00,000)."""
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value)
    if text is None:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    if not m:
        return None
    return float(m.group().replace(",", ""))


def format_inr(amount: float) -> str:
    """₹ with Indian grouping: 100000 -> ₹1,00,000."""
    whole = int(round(amount))
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"₹{digits}"


def amount(value: object) -> tuple[str | None, float | None]:
    number = parse_amount(value)
    if number is None or number <= 0:
        return None, None
    return format_inr(number), number


def riskometer(value: object) -> str | None:
    """Map variants ("Very high risk", "MODERATELY HIGH") to the SEBI six-level scale."""
    text = clean_text(value)
    if text is None:
        return None
    key = re.sub(r"\s*risk\s*$", "", text, flags=re.I).strip().casefold()
    for level in RISK_LEVELS:
        if key == level.casefold():
            return level
    return None


def lock_in(value: object) -> str | None:
    """Groww's lock_in object: {years, months, days}. All empty means the scheme has no lock-in."""
    if not isinstance(value, dict):
        return None
    parts = []
    for unit in ("years", "months", "days"):
        n = value.get(unit)
        if n:
            n = int(n)
            parts.append(f"{n} {unit[:-1] if n == 1 else unit}")
    return " ".join(parts) if parts else "No lock-in"


def parse_date(value: object) -> date | None:
    """Parse a date. ISO timestamps in UTC ("2026-09-29T18:30:00.000Z") are converted to the IST
    calendar date (30 Sep 2026), since Groww stores Indian dates as UTC midnight-minus-5:30."""
    text = clean_text(value)
    if text is None:
        return None
    if "T" in text and (text.endswith("Z") or "+" in text[10:]):
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return stamp.astimezone(IST).date()
        except ValueError:
            pass
    text = text.split("T")[0]
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d %b %Y", "%b %d, %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def strip_html(value: object) -> str | None:
    """Plain text from an HTML fragment (e.g. Groww's AMC description)."""
    if value is None:
        return None
    text = re.sub(r"<[^>]+>", " ", str(value))
    return clean_text(html.unescape(text))


def to_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def signed_pct(value: float) -> str:
    """+2.70% / -1.67%, as Groww shows returns."""
    return f"{value:+.2f}%"


def format_inr_decimal(amount: float) -> str:
    """Indian grouping with 2 decimals, as Groww shows AUM: 108546.98 -> 1,08,546.98."""
    whole, frac = f"{amount:.2f}".split(".")
    sign = "-" if whole.startswith("-") else ""
    return f"{sign}{format_inr(abs(int(whole)))[1:]}.{frac}"
