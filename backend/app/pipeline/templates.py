"""Fixed replies that need no retrieval or answer LLM (architecture §6.8), plus the deterministic
fact and metric templates used when the model is unavailable or misbehaves (§6.6).

Every reply has the same shape as a generated answer: `answer` (at most 3 sentences, no URL),
one `citation_url`, the `last_updated` date of that source, and a `type`. `rendered` adds the
Source line and the footer, so every response type carries exactly one link.

The one exception is the `comparison` reply (published figures for 2-4 schemes side by side):
it also carries a `Comparison` table, and cites each scheme's own page (one link per column).
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from app.pipeline.catalog import Catalog, Link, to_ist_date
from app.pipeline.text import split_sentences

ReplyType = Literal[
    "answer",
    "advisory_refusal",
    "performance_refusal",
    "out_of_scope",
    "not_covered",
    "greeting",
    "pii_blocked",
    "no_evidence",
    "clarify",
    "unavailable",
    "comparison",
]

NOT_PUBLISHED = "—"  # a comparison cell whose value the scheme page does not publish


@dataclass(frozen=True)
class ComparisonColumn:
    scheme_id: str
    name: str
    url: str  # the scheme's own page: each column cites its own source
    last_updated: date | None


@dataclass(frozen=True)
class ComparisonRow:
    label: str
    cells: tuple[str, ...]  # one per column, NOT_PUBLISHED when the page has no value


@dataclass(frozen=True)
class Comparison:
    """Published values for several schemes, side by side, in the order they were named.

    Never sorted, highlighted or ranked: the table restates the pages, it does not judge.
    """

    columns: tuple[ComparisonColumn, ...]
    rows: tuple[ComparisonRow, ...]
    notes: tuple[str, ...] = ()

    def markdown(self) -> str:
        def cell(text: str) -> str:
            return " ".join(str(text).split()).replace("|", "\\|")

        head = ["Field", *(c.name for c in self.columns)]
        lines = ["| " + " | ".join(cell(h) for h in head) + " |",
                 "|" + "---|" * len(head)]
        lines += ["| " + " | ".join(cell(x) for x in (r.label, *r.cells)) + " |"
                  for r in self.rows]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": [{"scheme_id": c.scheme_id, "name": c.name, "url": c.url,
                         "last_updated": c.last_updated.isoformat() if c.last_updated else None}
                        for c in self.columns],
            "rows": [{"label": r.label, "cells": list(r.cells)} for r in self.rows],
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Comparison | None":
        if not data:
            return None
        columns = tuple(
            ComparisonColumn(c["scheme_id"], c["name"], c["url"],
                             date.fromisoformat(c["last_updated"]) if c.get("last_updated")
                             else None)
            for c in data.get("columns", ()))
        rows = tuple(ComparisonRow(r["label"], tuple(r["cells"])) for r in data.get("rows", ()))
        return cls(columns, rows, tuple(data.get("notes", ())))


@dataclass(frozen=True)
class Reply:
    type: ReplyType
    answer: str
    citation_url: str
    last_updated: date | None
    comparison: Comparison | None = None  # only on a `comparison` reply

    @property
    def rendered(self) -> str:
        if self.comparison is not None:
            return self._rendered_comparison()
        parts = [self.answer, f"Source: {self.citation_url}"]
        if self.last_updated is not None:  # never print "None" (FMT-03)
            parts.append(f"Last updated from sources: {format_date(self.last_updated)}")
        return "\n\n".join(parts)

    def _rendered_comparison(self) -> str:
        table = self.comparison
        parts = [self.answer, table.markdown()]
        if table.notes:
            parts.append("\n".join(table.notes))
        parts.append("Sources:\n" + "\n".join(f"- {c.name}: {c.url}" for c in table.columns))
        if self.last_updated is not None:
            parts.append(f"Last updated from sources: {format_date(self.last_updated)}")
        return "\n\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "answer": self.answer,
            "citation_url": self.citation_url,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
            "rendered": self.rendered,
            "comparison": self.comparison.to_dict() if self.comparison else None,
        }


def format_date(d: date) -> str:
    return d.strftime("%d %b %Y")  # 09 Nov 2026


def _reply(kind: ReplyType, answer: str, link: Link) -> Reply:
    return Reply(kind, answer, link.url, link.last_updated)


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " or " + names[-1]


# ------------------------------------------------------------------------------ education topic
_TOPIC_KEYWORDS = (
    ("elss", ("elss", "tax saving", "tax saver", "80c")),
    ("tax", ("tax", "ltcg", "stcg", "capital gain")),
    ("sip_basics", ("sip", "monthly", "systematic")),
    ("risk", ("risk", "safe", "volatile", "riskometer", "crash")),
    ("comparing_funds", ("compare", "comparison", "better", "vs", "versus", "best", "top",
                         "lowest", "highest", "rank")),
    ("choosing_a_fund", ("should i", "invest", "suitable", "recommend", "advice", "advise",
                         "choose", "pick", "portfolio")),
)


def pick_education_topic(text: str) -> str:
    """The investor-education topic that best fits an advisory question."""
    t = text.lower()
    for topic, words in _TOPIC_KEYWORDS:
        if any(w in t for w in words):
            return topic
    return "default"


# ------------------------------------------------------------------------------ refusals etc.
def advisory_refusal(catalog: Catalog, topic: str = "default") -> Reply:
    return _reply(
        "advisory_refusal",
        "I can only share factual information about mutual fund schemes, so I can't advise on "
        "whether to invest or which fund is better. To understand how to evaluate funds for "
        "your own goals, see this investor-education resource from AMFI.",
        catalog.education_link(topic),
    )


def performance_refusal(catalog: Catalog, scheme_id: str | None = None) -> Reply:
    if scheme_id is None:
        return _reply(
            "performance_refusal",
            "I can share published figures such as returns, NAV and category averages, but I "
            "can't judge whether a fund's performance is good or compare funds. Name a scheme "
            "and I can give you its published returns with their dates.",
            catalog.education_link("comparing_funds"),
        )
    name = catalog.schemes[scheme_id].name
    return _reply(
        "performance_refusal",
        "I can share published figures such as returns, NAV and category averages, but I "
        "can't judge whether a fund's performance is good or compare funds. You can see "
        f"{name}'s full performance data on its scheme page.",
        catalog.scheme_link(scheme_id),
    )


def out_of_scope(catalog: Catalog) -> Reply:
    return _reply(
        "out_of_scope",
        f"I can answer factual questions about the {len(catalog.schemes)} mutual fund schemes I "
        "cover, such as their expense ratio, exit load or minimum SIP. For general mutual fund "
        "education, see the AMFI investor page.",
        catalog.education_link("scope"),
    )


def not_covered(catalog: Catalog) -> Reply:
    """A scheme or AMC outside the corpus (CLS-30): say so, never guess a covered one."""
    amcs = sorted(a.name.removesuffix(" Mutual Fund") for a in catalog.amcs.values())
    return _reply(
        "not_covered",
        "That scheme isn't in my sources, so I can't answer questions about it. I cover "
        f"{len(catalog.schemes)} schemes from {_join(amcs).replace(' or ', ' and ')}.",
        catalog.education_link("scope"),
    )


def plan_not_covered(catalog: Catalog, scheme_id: str) -> Reply:
    """Regular plan or IDCW option of a covered scheme (CLS-31)."""
    name = catalog.schemes[scheme_id].name
    return _reply(
        "not_covered",
        f"I only cover the Direct Plan - Growth option of {name}, so I can't answer for its "
        "Regular plan or IDCW option. The scheme page has the Direct - Growth details.",
        catalog.scheme_link(scheme_id),
    )


def greeting(catalog: Catalog) -> Reply:
    return _reply(
        "greeting",
        f"Hello! I answer factual questions about {len(catalog.schemes)} mutual fund schemes, "
        "for example the expense ratio of Kotak Multi Cap Fund or the exit load of Bandhan "
        "Small Cap Fund. I don't give investment advice.",
        catalog.education_link("default"),
    )


def pii_blocked(catalog: Catalog) -> Reply:
    return _reply(
        "pii_blocked",
        "For your security, please don't share personal details such as PAN, Aadhaar, account "
        "numbers, OTPs, phone numbers, or email addresses. I can only answer general factual "
        "questions about mutual fund schemes.",
        catalog.education_link("privacy"),
    )


def no_evidence(catalog: Catalog, scheme_id: str | None = None, link: Link | None = None) -> Reply:
    """`link`: the retriever's fallback (most relevant general page) for a general question."""
    if scheme_id is not None:
        return _reply(
            "no_evidence",
            "I couldn't find that in my sources. You can check the scheme's official page here.",
            catalog.scheme_link(scheme_id),
        )
    return _reply(
        "no_evidence",
        # The link is the most relevant AMFI, SEBI or CAMS page, so the wording names none.
        "I couldn't find that in my sources. You can check the most relevant official "
        "investor-education page here.",
        link or catalog.education_link("default"),
    )


def unavailable(link: Link) -> Reply:
    """The answer model is busy or down and no deterministic template fits the question.

    Honest about the cause (unlike the no-evidence reply), and still links the most relevant
    source the retriever found.
    """
    return _reply(
        "unavailable",
        "I'm handling a lot of questions right now and can't write this answer at the moment. "
        "Please try again in a minute, or see the most relevant source page here.",
        link,
    )


def corpus_unavailable(catalog: Catalog, scheme_id: str | None = None) -> Reply:
    """No corpus is loaded yet (a new server waiting for its first data bundle).

    Honest about the cause and links the scheme's page (or the default education page), so the
    user still gets one official source.
    """
    link = (catalog.scheme_link(scheme_id) if scheme_id in catalog.schemes
            else catalog.education_link("default"))
    return _reply(
        "unavailable",
        "My sources are still being loaded, so I can't answer this right now. "
        "Please try again in a few minutes, or see the official page here.",
        link,
    )


MAX_LISTED_CANDIDATES = 5


def clarify(catalog: Catalog, candidates: list[str] | tuple[str, ...] = (),
            amc: str | None = None) -> Reply:
    """Ask which scheme the user means; lists at most MAX_LISTED_CANDIDATES names (RES-06)."""
    names = [catalog.schemes[c].name for c in candidates]
    if not names:
        answer = (
            "Which scheme do you mean? Please name the fund, for example \"exit load of Kotak "
            "Multi Cap Fund\"."
        )
    elif len(names) <= MAX_LISTED_CANDIDATES:
        answer = f"Which scheme do you mean: {_join(names)}?"
    else:
        amc_name = catalog.amcs[amc].name.removesuffix(" Mutual Fund") if amc else ""
        prefix = f"{amc_name} " if amc_name else ""
        answer = (
            f"Which {prefix}scheme do you mean? I cover {len(names)}, including "
            f"{', '.join(names[:3])} and {names[3]}."
        )
    return _reply("clarify", answer, catalog.education_link("default"))


# ------------------------------------------------------------------------------ facts / metrics
FIELD_LABELS = {
    "expense_ratio": "expense ratio",
    "exit_load": "exit load",
    "min_sip": "minimum SIP amount",
    "min_lumpsum": "minimum lumpsum investment",
    "lock_in": "lock-in period",
    "riskometer": "riskometer level",
    "benchmark": "benchmark",
    "fund_managers": "fund manager",
    "manager_education": "fund manager education",
    "manager_experience": "fund manager experience",
    "launch_date": "launch date",
    "category": "category",
}


def fact_sentence(field: str, name: str, value) -> str:
    if field == "exit_load":
        text = str(value).strip().rstrip(".")
        if text.lower() in ("nil", "none", "0", "0%", "no exit load"):
            return f"There is no exit load for {name}."
        lowered = text[0].lower() + text[1:]
        for prefix in ("exit load of ", "exit load is "):
            if lowered.startswith(prefix):
                return f"The exit load of {name} is {text[len(prefix):]}."
        return f"The exit load of {name}: {text}."
    if field == "lock_in":
        text = str(value).strip().rstrip(".")
        if text.lower() in ("no lock-in", "no lock in", "nil", "none"):
            return f"{name} has no lock-in period."
        return f"The lock-in period of {name} is {text}."
    if field == "fund_managers":
        managers = list(value) if isinstance(value, list | tuple) else [str(value)]
        if len(managers) == 1:
            return f"The fund manager of {name} is {managers[0]}."
        return f"The fund managers of {name} are {_join(managers).replace(' or ', ' and ')}."
    if field == "launch_date" and isinstance(value, date):
        value = format_date(value)
    return f"The {FIELD_LABELS.get(field, field.replace('_', ' '))} of {name} is {value}."


_PRONOUN = re.compile(r"^(He|She|His|Her|They|Their|It|Its)\b")


def _one_sentence(text: str) -> str:
    """A published profile as one clause: its own sentence breaks become semicolons (split the
    same way the validator counts sentences), so a template with several managers stays within
    three sentences without rewording anything."""
    pieces = [p.rstrip(".!?;, ") for p in split_sentences(" ".join(str(text).split()))]
    pieces = [pieces[0], *(_PRONOUN.sub(lambda m: m.group(1).lower(), p) for p in pieces[1:])]
    return "; ".join(p for p in pieces if p)


def _since(row: dict) -> str:
    since = row.get("managing_since")
    return f" (managing since {format_date(since)})" if isinstance(since, date) else ""


def manager_sentences(name: str, rows: list[dict], profile=(), narrowed: bool = False
                      ) -> list[str]:
    """Every listed manager with their start date, then the asked profile details as published.
    At most three sentences, however many managers the scheme has. `narrowed`: the rows are only
    the managers named in the question, so they are not presented as the full list."""
    names = [r["name"] + _since(r) for r in rows]
    if narrowed:
        joined = _join(names).replace(" or ", " and ")
        role = "is a fund manager" if len(rows) == 1 else "are fund managers"
        first = f"{joined} {role} of {name}."
    elif len(rows) == 1:
        first = f"The fund manager of {name} is {names[0]}."
    else:
        first = f"The fund managers of {name} are {_join(names).replace(' or ', ' and ')}."
    sentences = [first]
    for key, label in (("education", "Education"), ("experience", "Experience")):
        if f"manager_{key}" not in profile:
            continue
        parts = [(r["name"], _one_sentence(r[key]) if r.get(key) else "not published")
                 for r in rows]
        if len(parts) == 1:
            sentences.append(f"{label}, as published: {parts[0][1]}.")
        else:
            sentences.append(f"{label}, as published: "
                             + "; ".join(f"{n} - {t}" for n, t in parts) + ".")
    return sentences


def fact(catalog: Catalog, scheme_id: str, field: str, value,
         fetched_at: datetime | date | None = None) -> Reply:
    """'The <field> of <scheme> is <value>.' straight from the facts table."""
    link = catalog.scheme_link(scheme_id)
    when = to_ist_date(fetched_at) if fetched_at is not None else link.last_updated
    name = catalog.schemes[scheme_id].name
    return Reply("answer", fact_sentence(field, name, value), link.url, when)


def facts(catalog: Catalog, scheme_id: str, values: dict, fetched_at=None,
          managers: list[dict] | None = None, profile=(), narrowed: bool = False) -> Reply:
    """Up to three fact sentences from one scheme page (e.g. expense ratio and exit load).
    `managers` (fund_managers evidence rows) adds start dates and any asked profile details."""
    link = catalog.scheme_link(scheme_id)
    when = to_ist_date(fetched_at) if fetched_at is not None else link.last_updated
    name = catalog.schemes[scheme_id].name
    sentences: list[str] = []
    for f, v in values.items():
        if f == "fund_managers" and managers:
            sentences += manager_sentences(name, managers, profile, narrowed)
        else:
            sentences.append(fact_sentence(f, name, v))
    return Reply("answer", " ".join(sentences[:3]), link.url, when)


def format_metric_value(value: float, unit: str, metric: str = "") -> str:
    if unit == "%":
        signed = metric in ("return", "sip_return", "category_average_return")
        return f"{value:+.2f}%" if signed else f"{value:.2f}%"
    if unit == "₹ Cr":
        return f"₹{value:,.2f} Cr"
    if unit == "₹":
        return f"₹{value:,.2f}" if value % 1 else f"₹{value:,.0f}"
    if unit == "rating":
        return f"{value:g} out of 5"
    return f"{value:g}"


def metric(catalog: Catalog, scheme_id: str, *, label: str, value: float, unit: str,
           as_of: date | None, metric_name: str = "", rank_of: int | None = None,
           fetched_at: datetime | date | None = None) -> Reply:
    """'The <label> of <scheme> was <value> as of <date>, as published by Groww.'"""
    link = catalog.scheme_link(scheme_id)
    when = to_ist_date(fetched_at) if fetched_at is not None else link.last_updated
    name = catalog.schemes[scheme_id].name
    on = f" as of {format_date(as_of)}" if as_of else ""
    if unit == "rank" and rank_of:
        what = label.removeprefix("Groww rank ").strip()
        answer = f"Groww ranks {name} {value:g} of {rank_of} {what}{on}."
    else:
        shown = format_metric_value(value, unit, metric_name)
        what = _label_in_sentence(label, unit)
        answer = f"The {what} of {name} was {shown}{on}, as published by Groww."
    return Reply("answer", answer, link.url, when)


def holdings(catalog: Catalog, scheme_id: str, rows: list[dict], portfolio_date: date | None,
             fetched_at: datetime | date | None = None, top: int = 5) -> Reply:
    """'The top 5 holdings of <scheme> as of <date> were A (7.75%), B (5.60%), ...'"""
    link = catalog.scheme_link(scheme_id)
    when = to_ist_date(fetched_at) if fetched_at is not None else link.last_updated
    name = catalog.schemes[scheme_id].name
    items = [f"{r['company_name']} ({r['percent_of_assets']:.2f}%)" if r.get("percent_of_assets")
             else r["company_name"] for r in rows[:top]]
    on = f" as of {format_date(portfolio_date)}" if portfolio_date else ""
    answer = (f"The top {len(items)} holdings of {name}{on} were "
              f"{_join(items).replace(' or ', ' and ')}, as published by Groww.")
    return Reply("answer", answer, link.url, when)


_COMMON_FIRST_WORDS = {
    "fund", "base", "portfolio", "minimum", "maximum", "face", "mean", "information",
    "standard", "absolute", "annualised",
}


def _label_in_sentence(label: str, unit: str) -> str:
    """'Fund size (AUM)' -> 'fund size (AUM)'; names and acronyms ('NAV', 'Groww') stay."""
    if unit == "rating":
        label = label.replace(" (out of 5)", "")
    first = label.split()[0]
    if first.lower() in _COMMON_FIRST_WORDS or first[0].isdigit():
        return label[0].lower() + label[1:]
    return label
