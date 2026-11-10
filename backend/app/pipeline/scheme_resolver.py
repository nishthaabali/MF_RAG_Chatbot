"""Step 3: map the user's wording to covered schemes, and detect which field or metric is asked.

Resolution order (architecture §6.3, docs/edge-case.md §6):
1. Reply to a clarifying question: "the ICICI one", "2", "the second" picks from the candidates
   offered last turn.
2. Exact alias match (aliases.yaml + the canonical page name), whole words, case and punctuation
   insensitive. Several schemes in one message are all returned, in order of mention.
3. Fuzzy alias match for typos ("kotak mutli cap"). Token by token, so a different word is never
   "close enough": "HDFC Nifty 50 Index" does not match "HDFC Nifty Next 50 Index", and
   "HDFC Gold ETF FoF" does not match "HDFC Silver ETF FoF".
4. An AMC that is not covered ("SBI Bluechip") -> not_covered; nothing is guessed.
5. A covered AMC: alone -> its only scheme, or a clarifying list; with a qualifier
   ("HDFC index fund", "ABSL short term") -> the scheme whose name contains that phrase, or
   not_covered when none does ("HDFC Mid-Cap Opportunities").
6. A category phrase ("the large cap fund") -> the schemes with it in their name.
7. Thread context: a reference ("its", "this fund", "what about ...") or a bare field question
   ("exit load?") uses the thread's active scheme; without one the pipeline asks which scheme.

A covered fund manager's name ("What is Rajeev Thakkar's experience?") resolves to a scheme they
manage (the active one if they manage it), and narrows the answer to that manager.
"""

import re
from dataclasses import dataclass, field, replace
from typing import Literal

from rapidfuzz import fuzz

from app.pipeline.catalog import Catalog
from app.pipeline.text import normalise_name

Status = Literal["resolved", "context", "ambiguous", "not_covered", "none"]

FUZZY_TOKEN_MIN = 75  # every token of a fuzzy match must reach this ratio
FUZZY_MEAN_MIN = 88  # and the tokens on average this one

# AMCs we do not cover. Their schemes must never be fuzzy-matched to a covered one (RES-10).
UNCOVERED_AMCS = (
    "sbi", "axis", "mirae", "mirae asset", "uti", "dsp", "tata", "quant", "motilal",
    "motilal oswal", "franklin", "franklin templeton", "canara", "canara robeco", "invesco",
    "sundaram", "lic", "lic mf", "baroda", "bank of india", "hsbc", "jm", "jm financial",
    "mahindra manulife", "navi", "groww mutual fund", "whiteoak", "bajaj", "bajaj finserv",
    "360 one", "iti", "union", "quantum", "shriram", "samco", "trust mf", "helios", "nj",
    "pgim", "zerodha", "old bridge", "unifi", "taurus",
)

# Words that appear in fund names generally, beyond the covered schemes' own names. They let
# "HDFC Gold ETF FoF" be recognised as a (different, uncovered) HDFC scheme.
_GENERIC_NAME_WORDS = set(
    """gold silver liquid overnight gilt arbitrage balanced hybrid advantage dynamic equity debt
    savings saver tax elss focused value contra dividend yield infrastructure banking psu
    technology tech consumption opportunities opportunity mid small large cap bluechip
    nifty sensex index etf fof multi flexi sectoral thematic international global nasdaq us
    retirement children ultra short duration medium long term low credit risk corporate bond
    floater floating quality momentum manufacturing innovation mnc esg pharma healthcare auto
    energy defence housing transportation next total market bse 50 100 150 200 250 500
    midcap smallcap largecap multicap flexicap allocation asset fixed maturity plan
    emerging sterling frontline discovery prudence special situations leaders titans elite pure
    magnum vision prime diversified rising aggressive conservative core select""".split()
)
# Words that may sit between an AMC name and "fund" without naming a different scheme
# ("Kotak Mutual Fund", "the HDFC AMC fund").
_AMC_FILLER = {"mutual", "mf", "amc", "s", "the", "a", "an", "india", "asset", "management",
               "company", "house", "own", "main", "only"}
# Qualifier words that are also common field/metric words; they may continue a qualifier
# ("credit risk") but never start one ("HDFC risk level" is a riskometer question).
_NOT_FIRST_QUALIFIER = {"risk", "top", "value", "yield", "market", "term", "plan", "allocation"}
_QUALIFIER_FILLER = {"the", "a", "an"}

# Category phrases matched against canonical scheme names when no scheme or AMC is named.
CATEGORY_PHRASES = (
    "large cap", "flexi cap", "multi cap", "small cap", "mid cap", "corporate bond",
    "credit risk", "index fund", "pharma", "healthcare", "silver", "total market",
)

_REFERENCE = re.compile(
    r"\b(it|its|it s|this|that|these|those|same|above|previous|the fund|the scheme|"
    r"what about|how about|and the|and its|also)\b"
)
_CONCEPT = re.compile(
    r"^(?:(?:hi|hello|hey)\s+)?(?:what\s+(?:is|are|does)(?:\s+an?)?|what\s+do\s+you\s+mean\s+by|"
    r"define|explain|meaning\s+of|how\s+(?:is|are|does|do|often|frequently|many))\b"
)
_DETERMINER = re.compile(r"\b(the|its|this|that|their|my)\b")
_DEFINITION = re.compile(r"\b(mean|means|meaning|define|definition|explain|concept)\b")
# "of a scheme", "for any fund", "an equity fund", "mutual funds": about funds in general, not
# one scheme.
_GENERIC_SUBJECT = re.compile(
    r"\b(a|an|any|each|every|all)\s+(mutual\s+|elss\s+|equity\s+|debt\s+|index\s+)?"
    r"(fund|scheme|mf)s?\b|\bmutual\s+funds\b"
)
# No covered scheme is an ELSS, so an ELSS / 80C question that names no scheme is general.
_ALWAYS_GENERAL = re.compile(r"\b(elss|tax\s+sav(ing|er)s?(\s+funds?)?|(section\s+)?80\s?c)\b")
# "How many risk levels does the riskometer have?": the structure of a concept.
_HOW_MANY_KINDS = re.compile(
    r"^how\s+many\s+(\w+\s+){0,2}(levels?|types?|categories|kinds?|grades?|labels?)\b"
)
# "How is the riskometer derived?": how a quantity is worked out, in general.
_HOW_DERIVED = re.compile(
    r"^how\s+(is|are|does|do)\b.*\b(derived|calculated|computed|decided|determined|assigned|"
    r"charged|applied|worked\s+out|work|works|measured)\b"
)
_OTHER_PLAN = re.compile(
    r"\b(regular\s+(plan|option|growth|variant)|idcw|dividend\s+(plan|option|payout|reinvest\w*)|"
    r"payout\s+option)\b"
)

ORDINALS = {
    "1": 0, "first": 0, "1st": 0,
    "2": 1, "second": 1, "2nd": 1, "two": 1,
    "3": 2, "third": 2, "3rd": 2, "three": 2,
    "4": 3, "fourth": 3, "4th": 3, "four": 3,
    "5": 4, "fifth": 4, "5th": 4, "five": 4,
    "last": -1,
}


# ---------------------------------------------------------------------------- field intent
@dataclass(frozen=True)
class MetricIntent:
    metric: str  # scheme_metrics.metric, or "holdings" / "peers"
    period: str | None = None  # "3y", "6m", "since_launch", ...
    basis: str | None = None  # "annualised" | "absolute" | "sip_annualised" | "point"


@dataclass(frozen=True)
class Intent:
    fields: tuple[str, ...] = ()  # scheme_facts columns, plus the PROFILE_FIELDS below
    metrics: tuple[MetricIntent, ...] = ()
    managers: tuple[str, ...] = ()  # fund managers named in the message (as published)

    @property
    def empty(self) -> bool:
        return not self.fields and not self.metrics


# Fund-manager profile details: not scheme_facts columns, answered from the fund_managers table
# together with the `fund_managers` field.
PROFILE_FIELDS = ("manager_education", "manager_experience")
_MANAGERS = ("fund_managers",)
_EDUCATION = ("fund_managers", "manager_education")
_EXPERIENCE = ("fund_managers", "manager_experience")
_PROFILE = ("fund_managers", *PROFILE_FIELDS)

# Keyword -> scheme_facts field(s). Longer phrases win over the words inside them.
FIELD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "expense ratio": ("expense_ratio",),
    "total expense ratio": ("expense_ratio",),
    "ter": ("expense_ratio",),
    "expense": ("expense_ratio",),
    "expenses": ("expense_ratio",),
    "fees": ("expense_ratio",),
    "fee": ("expense_ratio",),
    "annual charges": ("expense_ratio",),
    "management fee": ("expense_ratio",),
    "cost": ("expense_ratio",),
    "costs": ("expense_ratio",),
    "charges": ("expense_ratio", "exit_load"),  # FLD-02: same page, answer both
    "exit load": ("exit_load",),
    "exit": ("exit_load",),
    "exit charge": ("exit_load",),
    "exit fee": ("exit_load",),
    "redemption charge": ("exit_load",),
    "redemption fee": ("exit_load",),
    "redeem early": ("exit_load",),
    "early redemption": ("exit_load",),
    "minimum sip": ("min_sip",),
    "min sip": ("min_sip",),
    "sip amount": ("min_sip",),
    "minimum sip amount": ("min_sip",),
    "smallest sip": ("min_sip",),
    "minimum lumpsum": ("min_lumpsum",),
    "minimum lump sum": ("min_lumpsum",),
    "min lumpsum": ("min_lumpsum",),
    "lumpsum": ("min_lumpsum",),
    "lump sum": ("min_lumpsum",),
    "one time investment": ("min_lumpsum",),
    "minimum investment": ("min_sip", "min_lumpsum"),  # FLD-03
    "minimum amount": ("min_sip", "min_lumpsum"),
    "min investment": ("min_sip", "min_lumpsum"),
    "lock in": ("lock_in",),
    "lockin": ("lock_in",),
    "lock in period": ("lock_in",),
    "riskometer": ("riskometer",),
    "risk o meter": ("riskometer",),
    "risk level": ("riskometer",),
    "risk rating": ("riskometer",),
    "risk category": ("riskometer",),
    "risk grade": ("riskometer",),
    "risk profile": ("riskometer",),
    "how risky": ("riskometer",),
    "risk": ("riskometer",),
    "benchmark": ("benchmark",),
    "benchmark index": ("benchmark",),
    "which index": ("benchmark",),
    "tracks": ("benchmark",),
    "fund manager": _MANAGERS,
    "fund managers": _MANAGERS,
    "manager": _MANAGERS,
    "managers": _MANAGERS,
    "managed by": _MANAGERS,
    "managed": _MANAGERS,
    "manages": _MANAGERS,
    "managing": _MANAGERS,
    "who manages": _MANAGERS,
    "who runs": _MANAGERS,
    "runs the": _MANAGERS,
    "who handles": _MANAGERS,
    "handled by": _MANAGERS,
    "portfolio manager": _MANAGERS,
    "portfolio managers": _MANAGERS,
    "fund management team": _MANAGERS,
    "education": _EDUCATION,
    "educational": _EDUCATION,
    "qualification": _EDUCATION,
    "qualifications": _EDUCATION,
    "qualified": _EDUCATION,
    "degree": _EDUCATION,
    "degrees": _EDUCATION,
    "studied": _EDUCATION,
    "academic": _EDUCATION,
    "experience": _EXPERIENCE,
    "experienced": _EXPERIENCE,
    "work experience": _EXPERIENCE,
    "career": _EXPERIENCE,
    "worked": _EXPERIENCE,
    "previously worked": _EXPERIENCE,
    "work history": _EXPERIENCE,
    "background": _PROFILE,
    "profile": _PROFILE,
    "bio": _PROFILE,
    "biography": _PROFILE,
    "manager details": _PROFILE,
    "fund manager details": _PROFILE,
    "about the fund manager": _PROFILE,
    "about the fund managers": _PROFILE,
    "launch date": ("launch_date",),
    "launched": ("launch_date",),
    "inception date": ("launch_date",),
    "start date": ("launch_date",),
    "category": ("category",),
    "type of fund": ("category",),
    "fund type": ("category",),
    "kind of fund": ("category",),
}

# Keyword -> metric. Period and basis are filled in from the rest of the message.
METRIC_KEYWORDS: dict[str, str] = {
    "nav": "nav",
    "net asset value": "nav",
    "unit price": "nav",
    "aum": "aum",
    "fund size": "aum",
    "size of the fund": "aum",
    "assets under management": "aum",
    "corpus": "aum",
    "return": "return",
    "returns": "return",
    "cagr": "return",
    "absolute return": "return",
    "annualised return": "return",
    "annualized return": "return",
    "sip return": "sip_return",
    "sip returns": "sip_return",
    "sip xirr": "sip_return",
    "category average": "category_average_return",
    "category average return": "category_average_return",
    "category return": "category_average_return",
    "rank": "category_rank",
    "ranking": "category_rank",
    "ranked": "category_rank",
    "category rank": "category_rank",
    "rating": "groww_rating",
    "groww rating": "groww_rating",
    "sharpe": "sharpe_ratio",
    "sharpe ratio": "sharpe_ratio",
    "sortino": "sortino_ratio",
    "sortino ratio": "sortino_ratio",
    "beta": "beta",
    "alpha": "alpha",
    "standard deviation": "standard_deviation",
    "std dev": "standard_deviation",
    "volatility": "standard_deviation",
    "information ratio": "information_ratio",
    "turnover": "portfolio_turnover",
    "portfolio turnover": "portfolio_turnover",
    "base expense ratio": "base_expense_ratio",
    "minimum withdrawal": "min_withdrawal",
    "face value": "face_value",
    "minimum additional investment": "mini_additional_investment",
    "additional investment": "mini_additional_investment",
    "maximum sip": "max_sip_investment",
    "holdings": "holdings",
    "holding": "holdings",
    "top holdings": "holdings",
    "portfolio": "holdings",
    "stocks": "holdings",
    "shares": "holdings",
    "sector allocation": "holdings",
    "sectors": "holdings",
    "invests in": "holdings",
    "similar funds": "peers",
    "similar schemes": "peers",
    "peers": "peers",
    "peer funds": "peers",
}

_PERIOD_UNITS = {
    "y": "y", "yr": "y", "yrs": "y", "year": "y", "years": "y",
    "m": "m", "mo": "m", "month": "m", "months": "m",
    "w": "w", "week": "w", "weeks": "w",
    "d": "d", "day": "d", "days": "d",
}
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "nine": 9,
    "ten": 10,
}
_PERIOD = re.compile(
    r"\b(\d{1,2}|one|two|three|four|five|six|seven|nine|ten)\s*"
    r"(y|yr|yrs|year|years|m|mo|month|months|w|week|weeks|d|day|days)\b"
)
_SINCE_LAUNCH = re.compile(r"\b(since\s+(launch|inception|start)|inception|all\s+time|max)\b")
_ANNUALISED = re.compile(r"\b(annuali[sz]ed|cagr|annual|per\s+annum|p\s+a|yearly)\b")
_ABSOLUTE = re.compile(r"\b(absolute|point\s+to\s+point|total\s+return|cumulative)\b")
_TAX_RETURN = re.compile(r"\b(income\s+)?tax\s+returns?\b|\bitr\b")


def _period(text: str) -> str | None:
    if _SINCE_LAUNCH.search(text):
        return "since_launch"
    m = _PERIOD.search(text)
    if not m:
        return None
    number = _NUMBER_WORDS.get(m.group(1)) or int(m.group(1))
    unit = _PERIOD_UNITS[m.group(2)]
    if unit == "m" and number == 12:
        return "1y"
    return f"{number}{unit}"


def _default_basis(metric: str, period: str | None, text: str) -> str | None:
    if metric == "sip_return":
        return "sip_annualised"
    if metric not in ("return", "category_average_return"):
        return "point"
    if _ABSOLUTE.search(text):
        return "absolute"
    if _ANNUALISED.search(text):
        return "annualised"
    if period is None:
        return None
    # As published on the page: periods up to a year are absolute, longer ones annualised.
    if period == "since_launch":
        return "annualised"
    n, unit = int(period[:-1]), period[-1]
    return "annualised" if unit == "y" and n >= 2 else "absolute"


def _phrase_index(phrases) -> list[tuple[str, tuple[str, ...]]]:
    return sorted(((p, tuple(p.split())) for p in phrases), key=lambda x: -len(x[1]))


_FIELD_PHRASES = _phrase_index(FIELD_KEYWORDS)
_METRIC_PHRASES = _phrase_index(METRIC_KEYWORDS)


def _token_close(a: str, b: str) -> float:
    """Similarity of two words; numbers and short words must match exactly."""
    if a == b:
        return 100.0
    if a.isdigit() or b.isdigit() or len(a) <= 3 or len(b) <= 3:
        return 0.0
    return fuzz.ratio(a, b)


def _window_score(window: list[str], phrase: tuple[str, ...]) -> float:
    scores = [_token_close(w, p) for w, p in zip(window, phrase, strict=True)]
    if min(scores) < FUZZY_TOKEN_MIN:
        return 0.0
    return sum(scores) / len(scores)


def _find_phrases(words: list[str], phrases, min_single: float = 92.0):
    """Non-overlapping phrase matches (longest first), allowing small typos."""
    taken = [False] * len(words)
    found: list[tuple[int, str]] = []
    for phrase, ptoks in phrases:
        n = len(ptoks)
        for i in range(len(words) - n + 1):
            if any(taken[i : i + n]):
                continue
            score = _window_score(words[i : i + n], ptoks)
            needed = min_single if n == 1 else FUZZY_MEAN_MIN - 3
            if score >= needed:
                found.append((i, phrase))
                for j in range(i, i + n):
                    taken[j] = True
    return sorted(found)


def detect_intent(text: str) -> Intent:
    """Which scheme_facts fields and metrics the message asks about ("TER" -> expense_ratio)."""
    norm = _TAX_RETURN.sub(" ", normalise_name(text))
    words = norm.split()
    fields: list[str] = []
    for _, phrase in _find_phrases(words, _FIELD_PHRASES):
        for f in FIELD_KEYWORDS[phrase]:
            if f not in fields:
                fields.append(f)
    metrics: list[MetricIntent] = []
    period = _period(norm)
    for _, phrase in _find_phrases(words, _METRIC_PHRASES):
        metric = METRIC_KEYWORDS[phrase]
        if metric == "groww_rating" and "risk" in words:
            continue  # "risk rating" is the riskometer
        p = period if metric in ("return", "sip_return", "category_average_return",
                                 "category_rank") else None
        intent = MetricIntent(metric, p, _default_basis(metric, p, norm))
        if intent not in metrics:
            metrics.append(intent)
    if "category" in fields and any(m.metric.startswith("category_") for m in metrics):
        fields.remove("category")  # "category average/rank" is a metric, not the category
    # "portfolio manager" is a person, not the portfolio holdings.
    if re.search(r"\bportfolio managers?\b", norm):
        metrics = [m for m in metrics if m.metric != "holdings"]
    # "base expense ratio" is a metric; don't also report the expense_ratio field for it.
    if any(m.metric == "base_expense_ratio" for m in metrics) and "expense_ratio" in fields:
        if "expense ratio" not in norm.replace("base expense ratio", ""):
            fields.remove("expense_ratio")
    return Intent(tuple(fields), tuple(metrics))


def with_managers(intent: Intent, managers: list[str] | tuple[str, ...]) -> Intent:
    """Narrow the intent to named managers. A bare name ("Who is Rajeev Thakkar?") asks for
    the whole profile; "since when does X manage it" keeps just the managers field."""
    fields = list(intent.fields)
    if not fields and not intent.metrics:
        fields = list(_PROFILE)
    elif "fund_managers" not in fields:
        fields.insert(0, "fund_managers")
    return replace(intent, fields=tuple(dict.fromkeys(fields)), managers=tuple(managers))


def has_reference(text: str) -> bool:
    """'its', 'this fund', 'what about', 'and the': the message leans on earlier turns."""
    return bool(_REFERENCE.search(normalise_name(text)))


def is_concept_question(text: str) -> bool:
    """'What is exit load?' (a concept) as opposed to 'What is the exit load?' (of a scheme)."""
    norm = normalise_name(text)
    if _DEFINITION.search(norm) or _HOW_DERIVED.search(norm) or _HOW_MANY_KINDS.search(norm):
        return True
    if _ALWAYS_GENERAL.search(norm):
        return True
    if _CONCEPT.search(norm) and _GENERIC_SUBJECT.search(norm):
        return True
    return bool(_CONCEPT.search(norm)) and not _DETERMINER.search(norm)


def mentions_other_plan(text: str) -> bool:
    """Regular plan / IDCW: only the Direct - Growth plan is covered (CLS-31)."""
    return bool(_OTHER_PLAN.search(normalise_name(text)))


# ---------------------------------------------------------------------------- resolution
@dataclass(frozen=True)
class Resolution:
    status: Status
    scheme_ids: tuple[str, ...] = ()  # resolved / context: in order of mention
    candidates: tuple[str, ...] = ()  # ambiguous: the schemes to choose from
    via: str = ""  # alias | fuzzy | amc | category | context | pending
    amc: str | None = None  # AMC named in the message, if any
    needs_clarification: bool = False  # status "none" but the message is about a scheme
    score: float = 100.0

    @property
    def scheme_id(self) -> str | None:
        return self.scheme_ids[0] if self.scheme_ids else None


@dataclass(frozen=True)
class _Hit:
    start: int
    length: int
    owner: str  # scheme id, or "amc:<id>"


@dataclass
class _Alias:
    tokens: tuple[str, ...]
    owner: str  # scheme id, or "amc:<id>"


@dataclass
class SchemeResolver:
    catalog: Catalog
    _aliases: list[_Alias] = field(init=False)
    _scheme_texts: dict[str, list[str]] = field(init=False)
    _scheme_words: dict[str, set[str]] = field(init=False)
    _name_vocab: set[str] = field(init=False)
    _uncovered: list[tuple[str, ...]] = field(init=False)

    def __post_init__(self) -> None:
        aliases: dict[tuple[str, ...], str] = {}
        self._scheme_texts = {}
        for s in self.catalog.schemes.values():
            names = {normalise_name(n) for n in (s.name, *s.aliases)}
            names |= {n.removesuffix(" fund") for n in names if n.endswith(" fund")}
            self._scheme_texts[s.id] = sorted(names)
            for n in names:
                aliases[tuple(n.split())] = s.id
        for amc in self.catalog.amcs.values():
            for a in amc.aliases:
                aliases.setdefault(tuple(normalise_name(a).split()), f"amc:{amc.id}")
        self._aliases = sorted(
            (_Alias(t, o) for t, o in aliases.items() if t), key=lambda a: -len(a.tokens)
        )
        self._scheme_words = {
            sid: {w for n in texts for w in n.split()} for sid, texts in self._scheme_texts.items()
        }
        self._name_vocab = {w for texts in self._scheme_texts.values() for n in texts
                            for w in n.split()} | _GENERIC_NAME_WORDS
        self._uncovered = sorted(
            (tuple(normalise_name(a).split()) for a in UNCOVERED_AMCS), key=len, reverse=True
        )
        self._managers = sorted(
            ((tuple(normalise_name(n).split()), n) for n in self.catalog.managers),
            key=lambda m: -len(m[0]),
        )
        surnames: dict[str, list[str]] = {}
        for tokens, name in self._managers:
            if len(tokens) >= 2 and len(tokens[-1]) >= 5:
                surnames.setdefault(tokens[-1], []).append(name)
        self._surnames = {w: names[0] for w, names in surnames.items()
                          if len(names) == 1 and w not in self._name_vocab}

    # ------------------------------------------------------------------ public
    def analyse(
        self,
        text: str,
        active_scheme_id: str | None = None,
        pending_candidates: tuple[str, ...] | list[str] | None = None,
    ) -> tuple[Resolution, Intent]:
        """Resolve the schemes and detect the field intent of one message.

        Intent is detected with the matched scheme and AMC names removed, so a name such as
        "ABSL Credit Risk Fund" is not read as a riskometer question.
        """
        words = normalise_name(text).split()
        hits = self._exact_hits(words)
        covered = {j for h in hits for j in range(h.start, h.start + h.length)}
        managers, spans = self._manager_mentions(words, covered)
        covered |= spans
        rest = " ".join(w for j, w in enumerate(words) if j not in covered)
        intent = detect_intent(rest)
        if not managers and "fund_managers" in intent.fields:
            # A surname alone names a manager only in a question about managers.
            managers, _ = self._manager_mentions(words, covered, surnames=True)
        if managers:
            intent = with_managers(intent, managers)
        return self.resolve(text, active_scheme_id, pending_candidates, intent), intent

    def resolve(
        self,
        text: str,
        active_scheme_id: str | None = None,
        pending_candidates: tuple[str, ...] | list[str] | None = None,
        intent: Intent | None = None,
    ) -> Resolution:
        words = normalise_name(text).split()
        intent = intent if intent is not None else detect_intent(text)

        if pending_candidates:
            picked = self._pick_pending(words, list(pending_candidates))
            if picked:
                return Resolution("resolved", (picked,), via="pending")

        hits = self._exact_hits(words)
        schemes = list(dict.fromkeys(h.owner for h in hits if not h.owner.startswith("amc:")))
        amc_hits = [h for h in hits if h.owner.startswith("amc:")]
        amcs = list(dict.fromkeys(h.owner[4:] for h in amc_hits))
        if schemes:
            return Resolution("resolved", tuple(schemes), via="alias",
                              amc=amcs[0] if amcs else None)

        if intent.managers:
            managed = [sid for m in intent.managers for sid in self.catalog.managers.get(m, ())]
            if managed:
                pick = active_scheme_id if active_scheme_id in managed else managed[0]
                return Resolution("resolved", (pick,), via="manager")

        fuzzy = self._fuzzy(words)
        if fuzzy:
            ids, score = fuzzy
            return Resolution("resolved", ids, via="fuzzy", score=score)

        if self._mentions_uncovered_amc(words) and not amcs:
            return Resolution("not_covered", via="amc")

        if amc_hits:
            return self._by_amc(words, amc_hits[0])

        category = self._by_category(words)
        if category is not None:
            return category

        about_scheme = bool(_REFERENCE.search(" ".join(words))) or (
            not intent.empty and not is_concept_question(text)
        )
        if about_scheme and active_scheme_id in self.catalog.schemes:
            return Resolution("context", (active_scheme_id,), via="context")
        return Resolution("none", needs_clarification=about_scheme)

    # ------------------------------------------------------------------ steps
    def _manager_mentions(self, words: list[str], skip: set[int],
                          surnames: bool = False) -> tuple[list[str], set[int]]:
        """Covered fund managers named in the message: full names, or (when asked about
        managers) a surname that only one covered manager has."""
        found: list[str] = []
        taken = set(skip)
        if surnames:
            for j, w in enumerate(words):
                name = self._surnames.get(w)
                if j not in taken and name and name not in found:
                    found.append(name)
                    taken.add(j)
            return found, taken - skip
        for tokens, name in self._managers:
            n = len(tokens)
            for i in range(len(words) - n + 1):
                span = set(range(i, i + n))
                if tuple(words[i:i + n]) == tokens and not span & taken:
                    if name not in found:
                        found.append(name)
                    taken |= span
        return found, taken - skip

    def _exact_hits(self, words: list[str]) -> list[_Hit]:
        """Whole-word alias matches, longest first, non-overlapping, in order of mention."""
        taken = [False] * len(words)
        hits: list[_Hit] = []
        for alias in self._aliases:
            n = len(alias.tokens)
            for i in range(len(words) - n + 1):
                if tuple(words[i : i + n]) == alias.tokens and not any(taken[i : i + n]):
                    hits.append(_Hit(i, n, alias.owner))
                    for j in range(i, i + n):
                        taken[j] = True
        return sorted(hits, key=lambda h: h.start)

    def _fuzzy(self, words: list[str]) -> tuple[tuple[str, ...], float] | None:
        best: dict[str, tuple[int, float]] = {}
        for alias in self._aliases:
            if alias.owner.startswith("amc:") or len(alias.tokens) < 2:
                continue
            n = len(alias.tokens)
            for i in range(len(words) - n + 1):
                score = _window_score(words[i : i + n], alias.tokens)
                if score >= FUZZY_MEAN_MIN and score > best.get(alias.owner, (0, 0.0))[1]:
                    best[alias.owner] = (i, score)
        if not best:
            return None
        ordered = sorted(best.items(), key=lambda kv: kv[1][0])
        return tuple(sid for sid, _ in ordered), min(s for _, (_, s) in ordered)

    def _mentions_uncovered_amc(self, words: list[str]) -> bool:
        for toks in self._uncovered:
            n = len(toks)
            if any(tuple(words[i : i + n]) == toks for i in range(len(words) - n + 1)):
                return True
        return False

    def _qualifier(self, words: list[str], start: int) -> list[str]:
        out: list[str] = []
        for w in words[start:]:
            if w in _QUALIFIER_FILLER and not out:
                continue
            if w not in self._name_vocab or w in ("fund", "funds", "scheme", "schemes"):
                break
            if not out and w in _NOT_FIRST_QUALIFIER:
                break
            out.append(w)
        return out

    @staticmethod
    def _names_unknown_scheme(words: list[str], start: int) -> bool:
        """AMC + 1-4 unrecognised words + "fund"/"scheme": the name of some other scheme."""
        between: list[str] = []
        for w in words[start : start + 5]:
            if w in ("fund", "scheme"):
                return any(b not in _AMC_FILLER for b in between)
            between.append(w)
        return False

    def _by_amc(self, words: list[str], hit: _Hit) -> Resolution:
        amc_id = hit.owner[4:]
        schemes = [s.id for s in self.catalog.schemes_of(amc_id)]
        qualifier = self._qualifier(words, hit.start + hit.length)
        if qualifier:
            phrase = " ".join(qualifier)
            matches = [
                sid for sid in schemes
                if any(re.search(rf"\b{re.escape(phrase)}\b", t) for t in self._scheme_texts[sid])
            ]
            if len(matches) == 1:
                return Resolution("resolved", (matches[0],), via="amc", amc=amc_id)
            if matches:
                return Resolution("ambiguous", candidates=tuple(matches), via="amc", amc=amc_id)
            return Resolution("not_covered", via="amc", amc=amc_id)
        if self._names_unknown_scheme(words, hit.start + hit.length):
            # "Kotak Emerging Equity Fund": a specific scheme we don't cover; never guess the
            # AMC's covered one (RES-10).
            return Resolution("not_covered", via="amc", amc=amc_id)
        if len(schemes) == 1:
            return Resolution("resolved", (schemes[0],), via="amc", amc=amc_id)
        return Resolution("ambiguous", candidates=tuple(schemes), via="amc", amc=amc_id)

    def _by_category(self, words: list[str]) -> Resolution | None:
        text = " ".join(words)
        for phrase in CATEGORY_PHRASES:
            if not re.search(rf"\b{phrase}\b", text):
                continue
            matches = tuple(
                s.id for s in self.catalog.schemes.values()
                if re.search(rf"\b{phrase}\b", normalise_name(s.name))
            )
            if len(matches) == 1:
                return Resolution("resolved", matches, via="category")
            if matches:
                return Resolution("ambiguous", candidates=matches, via="category")
        return None

    def _pick_pending(self, words: list[str], candidates: list[str]) -> str | None:
        """Resolve a reply to a clarifying question against the candidates offered."""
        hits = self._exact_hits(words)
        within = list(dict.fromkeys(h.owner for h in hits if h.owner in candidates))
        if len(within) == 1:
            return within[0]
        for amc in dict.fromkeys(h.owner[4:] for h in hits if h.owner.startswith("amc:")):
            of_amc = [c for c in candidates if self.catalog.schemes[c].amc == amc]
            if len(of_amc) == 1:
                return of_amc[0]
        # A word that names exactly one candidate: "the nippon one", "bluechip".
        picked = {
            c
            for w in words
            if w not in ("fund", "the", "one")
            for c in candidates
            if w in self._scheme_words[c]
            and sum(w in self._scheme_words[o] for o in candidates) == 1
        }
        if len(picked) == 1:
            return picked.pop()
        # Position in the list: "2", "the second", "the first one", "last".
        if len(words) <= 4:
            for w in words:
                idx = ORDINALS.get(w)
                if idx is not None and -len(candidates) <= idx < len(candidates):
                    return candidates[idx]
        return None
