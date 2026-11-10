"""Step 4: hybrid retrieval, structured facts first (architecture §6.4).

Given a routed question (`RetrievalRequest` from the router) it returns numbered evidence
blocks (`E1`, `E2`, ...) or decides there is no evidence:

1. Facts lookup. A resolved scheme + field reads `scheme_facts`; a metric question reads
   `scheme_metrics` (or holdings, similar funds). These blocks come first: they are the
   strongest evidence and carry the label, basis, period and as-of date of every figure.
2. Chunk search. Chroma (top 8) and BM25 (top 8), both filtered by `scheme_id` for scheme
   questions or to AMFI/SEBI/RTA documents for general ones, merged with Reciprocal Rank Fusion;
   the top 5 chunks follow the facts.
3. No-evidence rule. Without a fact, the question needs (a) a best dense cosine of at least
   `min_relevance` and (b) its content words in the retrieved chunks. (b) exists because inside
   one scheme every chunk scores ~0.75-0.85 just for sharing the scheme name, so the cosine
   alone cannot reject "What is the colour of HDFC Flexi Cap?" (measured 2026-11-10: nonsense
   0.735-0.779 vs answerable 0.720-0.886 scoped; general questions do separate, 0.42-0.47 vs
   0.63-0.75).

Citations follow `citation_policy: groww`: scheme evidence carries the scheme's Groww page URL,
general evidence its AMFI/SEBI/RTA page URL. Every block keeps its own `fetched_at`.
"""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from sqlalchemy import Engine, select

from app.config import RetrievalSettings
from app.pipeline.catalog import Catalog, Link, to_ist_date
from app.pipeline.classifier import QueryClass
from app.pipeline.router import RetrievalRequest
from app.pipeline.scheme_resolver import PROFILE_FIELDS, Intent, MetricIntent
from app.pipeline.templates import FIELD_LABELS, format_date, format_metric_value
from app.pipeline.text import normalise_name

GENERAL_DOC_TYPES = ("amfi", "sebi", "rta_help")
RRF_K = 60
MAX_METRIC_ROWS = 8
MAX_HOLDINGS = 10
MIN_CONTENT_COVERAGE = 0.5

EvidenceKind = Literal["fact", "metric", "holdings", "peers", "chunk"]


# ------------------------------------------------------------------------------ data shapes
@dataclass(frozen=True)
class Evidence:
    id: str  # "E1", "E2", ...
    kind: EvidenceKind
    text: str  # what the answer model reads
    source_id: str
    source_url: str  # the citation if the model picks this block
    fetched_at: datetime | None
    scheme_id: str | None = None
    score: float | None = None  # dense cosine for chunks
    chunk_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)  # structured values for the validator

    @property
    def last_updated(self) -> date | None:
        return to_ist_date(self.fetched_at)

    def render(self) -> str:
        return f"[{self.id}] {self.text}"


@dataclass(frozen=True)
class RetrievalResult:
    status: Literal["ok", "no_evidence"]
    evidence: tuple[Evidence, ...]
    scheme_id: str | None
    fallback: Link  # the scheme's Groww page or the most relevant AMFI/SEBI page
    top_score: float | None = None  # best dense cosine (for logs and threshold tuning)
    reason: str = ""

    def get(self, evidence_id: str) -> Evidence | None:
        return next((e for e in self.evidence if e.id == evidence_id), None)


@dataclass(frozen=True)
class Hit:
    """One search result; the shape returned by ingest.indexer's indexes."""

    chunk_id: str
    score: float
    text: str
    metadata: dict[str, Any]


class SearchIndex(Protocol):
    def query(self, text: str, where: dict | None = None, k: int = 8) -> Sequence[Hit]: ...


# ------------------------------------------------------------------------------ SQL facts
class FactStore:
    """Read-only access to the structured tables written by ingestion."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def _rows(self, stmt) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(stmt)]

    def facts(self, scheme_id: str) -> dict[str, Any] | None:
        from app.store.models import SchemeFact

        rows = self._rows(select(SchemeFact.__table__).where(SchemeFact.scheme_id == scheme_id))
        return rows[0] if rows else None

    def metrics(self, scheme_id: str, metric: str) -> list[dict[str, Any]]:
        from app.store.models import SchemeMetric

        t = SchemeMetric.__table__
        return self._rows(
            select(t).where(t.c.scheme_id == scheme_id, t.c.metric == metric).order_by(t.c.id)
        )

    def holdings(self, scheme_id: str) -> list[dict[str, Any]]:
        from app.store.models import SchemeHolding

        t = SchemeHolding.__table__
        return self._rows(select(t).where(t.c.scheme_id == scheme_id).order_by(t.c.position))

    def peers(self, scheme_id: str) -> list[dict[str, Any]]:
        from app.store.models import SchemePeer

        t = SchemePeer.__table__
        return self._rows(select(t).where(t.c.scheme_id == scheme_id).order_by(t.c.position))

    def managers(self, scheme_id: str) -> list[dict[str, Any]]:
        from app.store.models import FundManager

        t = FundManager.__table__
        return self._rows(select(t).where(t.c.scheme_id == scheme_id).order_by(t.c.id))


# ------------------------------------------------------------------------------ helpers
_PERIOD_ORDER = ["1d", "1w", "1m", "3m", "6m", "9m", "1y", "2y", "3y", "4y", "5y", "7y", "10y",
                 "since_launch"]
_STOPWORDS = set(
    """a an the of for in on to at by from with about as and or is are was were be been being
    has have had do does did can could will would shall should may might must what whats which
    who whom whose when where why how much many tell me please give show know get i my we our
    you your it its this that these those there their them they any some all current latest
    today now present fund funds scheme schemes mutual mf direct growth plan option regular
    details info information kindly also just like is there s according per amfi sebi""".split()
)  # source names (amfi, sebi) are like scheme names: they say where to look, not what about
_UNDOUBLE = re.compile(r"(.)\1")


def _as_datetime(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _as_date(value) -> date | None:
    if value is None or isinstance(value, date):
        return value.date() if isinstance(value, datetime) else value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _words(text: str) -> set[str]:
    return set(normalise_name(text).split())


def _close(term: str, word: str) -> bool:
    """Same word, or the same stem ("risky"/"risk", "joined"/"join", "charges"/"charge").

    Doubled letters count as single ones, so British and American spellings match
    ("instalment"/"installment", "enrolment"/"enrollment").
    """
    if term == word:
        return True
    if len(term) < 4 or len(word) < 4:
        return False
    term, word = _UNDOUBLE.sub(r"\1", term), _UNDOUBLE.sub(r"\1", word)
    if term == word:
        return True
    stem = term[: max(4, len(term) - 2)]
    return word.startswith(stem) or term.startswith(word[: max(4, len(word) - 2)])


def rrf(*rankings: Sequence[str], k: int = RRF_K) -> list[str]:
    """Reciprocal Rank Fusion; ties broken by chunk id so results are reproducible (RET-12)."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for pos, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + pos + 1)
    return sorted(scores, key=lambda cid: (-scores[cid], cid))


# ------------------------------------------------------------------------------ retriever
class Retriever:
    def __init__(
        self,
        catalog: Catalog,
        store: FactStore,
        vector: SearchIndex,
        bm25: SearchIndex,
        settings: RetrievalSettings,
        citation_policy: str = "groww",
    ):
        if citation_policy != "groww":
            raise ValueError(f"unsupported citation_policy {citation_policy!r}")
        self.catalog = catalog
        self.store = store
        self.vector = vector
        self.bm25 = bm25
        self.settings = settings
        self.data_dir: Path | None = None  # the corpus folder, when loaded from disk

    @classmethod
    def from_settings(cls, settings, catalog: Catalog | None = None,
                      engine: Engine | None = None, *, embedder=None,
                      data_dir: Path | None = None, verify: bool = False) -> "Retriever":
        """Load the real indexes from data/ (Chroma + BM25) and the facts database.

        For a hot reload (`verify=True`), pass the running embedder so the model isn't loaded
        again: Chroma is opened fresh from disk and the corpus is checked (`verify_corpus`)
        before anything is returned, so a half-built or mismatched index is never used.
        """
        from app.corpus import active_corpus_dir, corpus_url, ensure_corpus_db, is_bundle_dir
        from app.store.db import make_engine
        from ingest.indexer import (
            BM25_NAME,
            CHROMA_DIRNAME,
            BM25Index,
            ChromaVectorIndex,
            make_embedder,
            verify_corpus,
        )

        data_dir = data_dir or active_corpus_dir()
        if engine is None:
            if not is_bundle_dir(data_dir):
                ensure_corpus_db(settings)  # one-time copy out of a pre-Phase 11 app.db
            engine = make_engine(corpus_url(settings, data_dir))
        catalog = catalog or Catalog.load(engine=engine)
        embedder = embedder or make_embedder(settings)
        vector = ChromaVectorIndex(data_dir / CHROMA_DIRNAME, embedder, fresh=verify)
        bm25 = BM25Index.load(data_dir / BM25_NAME)
        if verify:
            verify_corpus(settings, vector, bm25, embedder, data_dir)
        retriever = cls(catalog, FactStore(engine), vector, bm25, settings.retrieval,
                        settings.citation_policy)
        retriever.data_dir = data_dir
        return retriever

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """Async entry point: embedding, BM25 and SQL run in a worker thread."""
        return await asyncio.to_thread(self.retrieve_sync, request)

    def retrieve_sync(self, request: RetrievalRequest) -> RetrievalResult:
        scheme_id = request.scheme_id
        general = scheme_id is None or request.query_class == QueryClass.FACTUAL_GENERAL
        if general:
            scheme_id = None

        structured = [] if general else self._structured(scheme_id, request.intent)
        hits, top_score = self._search(request, scheme_id)
        chunks = [self._chunk_evidence(h) for h in hits]

        fallback = self._fallback(scheme_id, hits)
        if not structured:
            if not hits:
                return RetrievalResult("no_evidence", (), scheme_id, fallback, None, "no chunks")
            if top_score is None or top_score < self.settings.min_relevance:
                return RetrievalResult("no_evidence", (), scheme_id, fallback, top_score,
                                       "below min_relevance")
            if not self._supported(request.question, scheme_id, hits):
                return RetrievalResult("no_evidence", (), scheme_id, fallback, top_score,
                                       "question words not in the evidence")

        blocks = [*structured, *chunks]
        numbered = tuple(replace(b, id=f"E{i}") for i, b in enumerate(blocks, start=1))
        return RetrievalResult("ok", numbered, scheme_id, fallback, top_score)

    async def structured_for(self, scheme_ids, intent: Intent) -> dict[str, list[Evidence]]:
        """Fact and metric evidence for each scheme, without chunk search (comparison table)."""
        def run():
            return {sid: self._structured(sid, intent) for sid in scheme_ids}
        return await asyncio.to_thread(run)

    # ------------------------------------------------------------------ structured facts
    def _scheme_header(self, scheme_id: str) -> str:
        return f"{self.catalog.schemes[scheme_id].name} (Direct Plan - Growth)"

    def _source(self, scheme_id: str, row_fetched_at=None) -> tuple[str, str, datetime | None]:
        """Citation for a scheme fact: the Groww page and its last confirmed fetch."""
        scheme = self.catalog.schemes[scheme_id]
        confirmed = self.catalog.fetched_at.get(scheme.source_id)
        row = _as_datetime(row_fetched_at)
        when = row
        if confirmed is not None and (row is None or confirmed > to_ist_date(row)):
            when = datetime(confirmed.year, confirmed.month, confirmed.day)
        return scheme.source_id, scheme.url, when

    def _structured(self, scheme_id: str, intent: Intent) -> list[Evidence]:
        blocks: list[Evidence] = []
        if intent.fields:
            fact = self._fact_block(scheme_id, intent.fields, intent.managers)
            if fact:
                blocks.append(fact)
        for metric in intent.metrics:
            if metric.metric == "holdings":
                block = self._holdings_block(scheme_id)
            elif metric.metric == "peers":
                block = self._peers_block(scheme_id)
            else:
                block = self._metric_block(scheme_id, metric)
            if block:
                blocks.append(block)
        return blocks

    def _fact_block(self, scheme_id: str, fields: Sequence[str],
                    managers: Sequence[str] = ()) -> Evidence | None:
        row = self.store.facts(scheme_id)
        if not row:
            return None
        lines, values, data = [], {}, {}
        for f in fields:
            if f in PROFILE_FIELDS:
                continue  # answered with fund_managers, from the fund_managers table
            value = row.get(f)
            if value in (None, "", []):
                continue  # NULL: never infer a value (RET-02); chunks may still answer
            if f == "fund_managers":
                profile = [p for p in PROFILE_FIELDS if p in fields]
                names, manager_lines, rows = self._managers_evidence(
                    scheme_id, value, profile, managers)
                values[f] = names
                data.update(managers=rows, profile=profile,
                            narrowed=len(names) < len(value or ()))
                lines += manager_lines
                continue
            if f == "launch_date":
                value = format_date(_as_date(value)) if _as_date(value) else value
            values[f] = row.get(f)
            lines.append(f"- {FIELD_LABELS.get(f, f).capitalize()}: {value}")
        if not lines:
            return None
        source_id, url, when = self._source(scheme_id, row.get("fetched_at"))
        text = f"{self._scheme_header(scheme_id)}, from its scheme page:\n" + "\n".join(lines)
        return Evidence("", "fact", text, source_id, url, when, scheme_id,
                        data={"fields": values, **data})

    def _managers_evidence(self, scheme_id: str, names, profile: Sequence[str],
                           wanted: Sequence[str] = ()) -> tuple[list[str], list[str], list[dict]]:
        """Every manager the page lists, in its order, with their managing-since date; with
        education / experience when asked. `wanted` narrows it to managers named in the question
        (ignored when none of them manages this scheme)."""
        names = list(names) if isinstance(names, list | tuple) else [str(names)]
        total = len(names)
        if wanted and any(n in names for n in wanted):
            names = [n for n in names if n in wanted]
        by_name = {m["name"]: m for m in self.store.managers(scheme_id)}
        rows, parts = [], []
        for n in names:
            m = by_name.get(n, {})
            since = _as_date(m.get("managing_since"))
            rows.append({"name": n, "managing_since": since,
                         "education": m.get("education"), "experience": m.get("experience")})
            parts.append(f"{n} (managing since {format_date(since)})" if since else n)
        if len(names) < total:
            label = f"Fund manager(s) named in the question (one of the {total} managers)"
        elif len(names) == 1:
            label = "Fund manager"
        else:
            label = f"Fund managers (all {total}, in order)"
        lines = [f"- {label}: {', '.join(parts)}"]
        for r in rows if profile else ():
            details = []
            if "manager_education" in profile:
                details.append(f"education: {r['education'] or 'not published'}")
            if "manager_experience" in profile:
                details.append(f"experience: {r['experience'] or 'not published'}")
            lines.append(f"- {r['name']}, {'; '.join(details)}")
        return names, lines, rows

    def _metric_block(self, scheme_id: str, intent: MetricIntent) -> Evidence | None:
        rows = self.store.metrics(scheme_id, intent.metric)
        if not rows:
            return None
        chosen = rows
        if intent.period:
            chosen = [r for r in rows if r["period"] == intent.period]
            if intent.basis and intent.basis != "point":
                exact = [r for r in chosen if r["basis"] == intent.basis]
                chosen = exact or chosen
        elif len(rows) > 1:  # no period asked: the figures the page displays
            chosen = [r for r in rows if r["displayed"]] or rows
        if not chosen:
            return None
        chosen = sorted(
            chosen,
            key=lambda r: (_PERIOD_ORDER.index(r["period"]) if r["period"] in _PERIOD_ORDER
                           else -1, r["basis"]),
        )[:MAX_METRIC_ROWS]

        lines = []
        for r in chosen:
            as_of = _as_date(r["as_of"])
            on = f" as of {format_date(as_of)}" if as_of else ""
            if r["unit"] == "rank" and r["rank_of"]:
                shown = f"{r['value']:g} of {r['rank_of']}"
            else:
                shown = format_metric_value(r["value"], r["unit"], r["metric"])
            note = "" if r["displayed"] else " (from Groww's page data)"
            lines.append(f"- {r['label']}: {shown}{on}{note}")
        source_id, url, when = self._source(scheme_id, chosen[0]["fetched_at"])
        text = f"{self._scheme_header(scheme_id)}, figures published by Groww:\n" + "\n".join(lines)
        data = {
            "metric": intent.metric,
            "rows": [
                {k: r[k] for k in ("label", "value", "unit", "period", "basis", "as_of",
                                   "rank_of", "displayed")}
                for r in chosen
            ],
        }
        return Evidence("", "metric", text, source_id, url, when, scheme_id, data=data)

    def _holdings_block(self, scheme_id: str) -> Evidence | None:
        rows = self.store.holdings(scheme_id)
        if not rows:
            return None
        top = rows[:MAX_HOLDINGS]
        when_portfolio = _as_date(top[0]["portfolio_date"])
        on = f" as of {format_date(when_portfolio)}" if when_portfolio else ""
        lines = []
        for r in top:
            detail = ", ".join(x for x in (r["sector"], r["instrument"]) if x)
            pct = f": {r['percent_of_assets']:.2f}% of assets" if r["percent_of_assets"] else ""
            lines.append(f"- {r['position']}. {r['company_name']}"
                         f"{f' ({detail})' if detail else ''}{pct}")
        source_id, url, when = self._source(scheme_id, top[0]["fetched_at"])
        text = (f"{self._scheme_header(scheme_id)}, top {len(top)} of {len(rows)} portfolio "
                f"holdings{on}, as published by Groww:\n" + "\n".join(lines))
        data = {"holdings": [
            {"position": r["position"], "company_name": r["company_name"],
             "percent_of_assets": r["percent_of_assets"]} for r in top
        ], "portfolio_date": when_portfolio, "total": len(rows)}
        return Evidence("", "holdings", text, source_id, url, when, scheme_id, data=data)

    def _peers_block(self, scheme_id: str) -> Evidence | None:
        own = normalise_name(self.catalog.schemes[scheme_id].name)
        # Groww lists the scheme itself among its similar funds; leave it out.
        rows = [r for r in self.store.peers(scheme_id)
                if not normalise_name(r["peer_name"]).startswith(own)]
        if not rows:
            return None
        lines = [f"- {r['peer_name']}" + (f" ({r['fund_house']})" if r["fund_house"] else "")
                 for r in rows]
        source_id, url, when = self._source(scheme_id, rows[0]["fetched_at"])
        text = (f"{self._scheme_header(scheme_id)}, similar funds listed on its Groww page "
                "(listed by Groww, not ranked by this assistant):\n" + "\n".join(lines))
        data = {"peers": [r["peer_name"] for r in rows]}
        return Evidence("", "peers", text, source_id, url, when, scheme_id, data=data)

    # ------------------------------------------------------------------ chunk search
    def rewrite_query(self, request: RetrievalRequest, scheme_id: str | None) -> str:
        """Put the canonical scheme name (and, for short follow-ups, the field) in the query."""
        question = request.question.strip()
        parts = [question]
        if scheme_id:
            name = self.catalog.schemes[scheme_id].name
            if normalise_name(name) not in normalise_name(question):
                parts.insert(0, f"{name}:")
            if len(question.split()) < 6:  # "ER?", "and its benchmark?" (RET-10)
                labels = [FIELD_LABELS.get(f, f) for f in request.intent.fields]
                labels += [m.metric.replace("_", " ") for m in request.intent.metrics]
                parts.extend(labels)
        return " ".join(parts)

    def _search(self, request: RetrievalRequest, scheme_id: str | None):
        s = self.settings
        where = {"scheme_id": scheme_id} if scheme_id else {"doc_type": list(GENERAL_DOC_TYPES)}
        query = self.rewrite_query(request, scheme_id)
        dense = list(self.vector.query(query, where, s.vector_top_k))
        keyword = list(self.bm25.query(query, where, s.bm25_top_k))
        # Defence in depth: keep only hits that satisfy the filter (RET-03).
        dense = [h for h in dense if self._allowed(h, where)]
        keyword = [h for h in keyword if self._allowed(h, where)]
        by_id = {h.chunk_id: h for h in keyword}
        by_id.update({h.chunk_id: h for h in dense})  # dense hits carry the cosine score
        fused = rrf([h.chunk_id for h in dense], [h.chunk_id for h in keyword])
        cosine = {h.chunk_id: h.score for h in dense}
        hits = [
            Hit(cid, cosine.get(cid, float("nan")), by_id[cid].text, by_id[cid].metadata)
            for cid in fused[: s.final_top_k]
        ]
        top = max((h.score for h in dense), default=None)
        return hits, top

    @staticmethod
    def _allowed(hit, where: dict) -> bool:
        for key, value in where.items():
            allowed = value if isinstance(value, list | tuple) else [value]
            if hit.metadata.get(key) not in allowed:
                return False
        return True

    def _chunk_evidence(self, hit: Hit) -> Evidence:
        meta = hit.metadata
        scheme_id = meta.get("scheme_id")
        source_id = meta["source_id"]
        url = meta["source_url"]
        if scheme_id in self.catalog.schemes:  # citation policy: the scheme's Groww page
            url = self.catalog.schemes[scheme_id].url
        when = _as_datetime(meta.get("fetched_at"))
        score = None if hit.score != hit.score else hit.score  # NaN: BM25-only hit
        return Evidence("", "chunk", hit.text, source_id, url, when, scheme_id, score,
                        hit.chunk_id, data={"section": meta.get("section"),
                                            "as_of": meta.get("as_of")})

    # ------------------------------------------------------------------ no-evidence rule
    def content_terms(self, question: str, scheme_id: str | None) -> list[str]:
        """Question words that name the topic: no stopwords, numbers or scheme-name words."""
        name_words: set[str] = set()
        if scheme_id:
            scheme = self.catalog.schemes[scheme_id]
            for n in (scheme.name, *scheme.aliases):
                name_words |= _words(n)
            amc = self.catalog.amcs.get(scheme.amc)
            for a in amc.aliases if amc else ():
                name_words |= _words(a)
        return [
            w for w in dict.fromkeys(normalise_name(question).split())
            if w not in _STOPWORDS and w not in name_words and not w.isdigit() and len(w) > 1
        ]

    def _supported(self, question: str, scheme_id: str | None, hits: Sequence[Hit]) -> bool:
        terms = self.content_terms(question, scheme_id)
        if not terms:
            return True  # "Tell me about Kotak Multi Cap": nothing to check
        evidence_words = set().union(*(_words(h.text) for h in hits))
        found = sum(any(_close(t, w) for w in evidence_words) for t in terms)
        return found / len(terms) >= MIN_CONTENT_COVERAGE

    def _fallback(self, scheme_id: str | None, hits: Sequence[Hit]) -> Link:
        if scheme_id:
            return self.catalog.scheme_link(scheme_id)
        if hits:  # the most relevant general page
            return self.catalog.link(hits[0].metadata["source_id"])
        return self.catalog.education_link("default")
