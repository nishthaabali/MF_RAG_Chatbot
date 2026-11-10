"""Retriever (architecture §6.4).

Fast tests use fake indexes and a temporary SQLite database. Tests marked `slow` run against the
real Phase 3 indexes in data/ (Chroma + BM25 + app.db) and are skipped when those are missing.
"""

import re
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from app.config import DATA_DIR, RetrievalSettings
from app.pipeline.catalog import Catalog
from app.pipeline.classifier import QueryClass
from app.pipeline.retriever import FactStore, Hit, Retriever, rrf
from app.pipeline.router import RetrievalRequest, Router
from app.pipeline.scheme_resolver import Intent, MetricIntent
from app.store.db import init_db, make_engine, session_factory
from app.store.models import (
    FundManager,
    SchemeFact,
    SchemeHolding,
    SchemeMetric,
    SchemePeer,
    Source,
)

KOTAK = "kotak-multi-cap"
FETCHED = datetime(2026, 11, 9, 13, 39, tzinfo=UTC)
CONFIRMED = date(2026, 11, 10)  # last confirmed fetch in the sources table


# ------------------------------------------------------------------------------ fakes
class FakeIndex:
    """Returns its hits in a fixed order; applies the filter only if `obey_filter`."""

    def __init__(self, hits, obey_filter=True):
        self.hits, self.obey, self.calls = hits, obey_filter, []

    def query(self, text, where=None, k=8):
        self.calls.append((text, where, k))
        out = []
        for h in self.hits:
            ok = all(
                h.metadata.get(key) in (v if isinstance(v, list | tuple) else [v])
                for key, v in (where or {}).items()
            )
            if ok or not self.obey:
                out.append(h)
        return out[:k]


def chunk(cid, score, text, scheme_id=KOTAK, doc_type="scheme_page", source_id=None):
    meta = {
        "source_id": source_id or (f"groww-{scheme_id}" if scheme_id else "amfi-elss"),
        "source_url": "https://example.org/" + cid,
        "doc_type": doc_type,
        "section": cid.split("::")[1] if "::" in cid else cid,
        "fetched_at": "2026-11-09T13:39:00+00:00",
    }
    if scheme_id:
        meta["scheme_id"] = scheme_id
    return Hit(cid, score, text, meta)


KOTAK_CHUNKS = [
    chunk("groww-kotak-multi-cap::costs-and-exit-load::0", 0.86,
          "[Kotak Multi Cap Fund] Costs and exit load (expense ratio, TER)\nExpense ratio: 0.42%."),
    chunk("groww-kotak-multi-cap::identifiers::0", 0.80,
          "[Kotak Multi Cap Fund] Identifiers\nISIN: INF174KA1HS9. Registrar: CAMS."),
    chunk("groww-kotak-multi-cap::nav-and-fund-size::0", 0.78,
          "[Kotak Multi Cap Fund] NAV and fund size\nNAV: ₹21.56. Face value ₹10."),
]
OTHER_SCHEME = chunk("groww-hdfc-flexi-cap::costs-and-exit-load::0", 0.99,
                     "[HDFC Flexi Cap Fund] Costs\nExpense ratio: 0.70%.",
                     scheme_id="hdfc-flexi-cap")
GENERAL = [
    chunk("amfi-elss::elss::0", 0.66, "[ELSS] ELSS funds have a lock-in of 3 years.",
          scheme_id=None, doc_type="amfi", source_id="amfi-elss"),
    chunk("sebi-cas::cas::0", 0.55, "[CAS] Consolidated account statement.",
          scheme_id=None, doc_type="sebi", source_id="sebi-cas"),
]


@pytest.fixture(scope="module")
def catalog():
    cat = Catalog.load()
    return replace(cat, fetched_at={sid: CONFIRMED for sid in cat.source_urls})


@pytest.fixture
def store(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    init_db(engine)
    url = "https://groww.in/mutual-funds/kotak-multi-cap-fund-direct-growth"
    common = {"scheme_id": KOTAK, "source_id": "groww-kotak-multi-cap", "fetched_at": FETCHED}
    with session_factory(engine)() as s:
        s.add(Source(id="groww-kotak-multi-cap", url=url, type="scheme_page", scheme_id=KOTAK,
                     amc="kotak", fetched_at=FETCHED))
        s.flush()
        s.add(SchemeFact(scheme_name="Kotak Multi Cap Fund", expense_ratio="0.42%",
                         exit_load="Exit load of 1% if redeemed within 1 year.",
                         min_sip="₹100", lock_in="No lock-in", riskometer=None,
                         fund_managers=["Abhishek Bisen", "Devender Singhal"],
                         launch_date=date(2021, 9, 29), source_url=url, **common))
        metric = {"source_url": url, "as_of": date(2026, 11, 6), **common}
        s.add_all([
            SchemeMetric(metric="nav", period=None, basis="point", value=21.558, unit="₹",
                         label="NAV", displayed=True, **metric),
            SchemeMetric(metric="return", period="3y", basis="annualised", value=17.06,
                         unit="%", label="3-year annualised return", displayed=True, **metric),
            SchemeMetric(metric="return", period="3y", basis="absolute", value=60.46, unit="%",
                         label="3-year absolute return", displayed=False, **metric),
            SchemeMetric(metric="return", period="1y", basis="absolute", value=2.7, unit="%",
                         label="1-year absolute return", displayed=True, **metric),
            SchemeMetric(metric="sharpe_ratio", period=None, basis="point", value=0.6,
                         unit="ratio", label="Sharpe ratio", displayed=False, **metric),
            SchemeMetric(metric="category_rank", period="3y", basis="point", value=8,
                         unit="rank", rank_of=23, label="Groww rank within Multi Cap for "
                         "3-year return", displayed=True, **metric),
        ])
        s.add_all([
            SchemeHolding(position=i, company_name=f"Company {i}", sector="Financial",
                          instrument="Equity", percent_of_assets=6.0 - i * 0.1,
                          portfolio_date=date(2026, 9, 30), scheme_id=KOTAK,
                          source_id="groww-kotak-multi-cap", fetched_at=FETCHED)
            for i in range(1, 13)
        ])
        s.add_all([
            SchemePeer(position=1, peer_name="Axis Multicap Fund Direct Growth",
                       fund_house="Axis Mutual Fund", data={}, **common),
            SchemePeer(position=2, peer_name="Kotak Multi Cap Fund Direct Growth",
                       fund_house="Kotak Mahindra Mutual Fund", data={}, **common),
        ])
        s.add(FundManager(name="Abhishek Bisen", managing_since=date(2021, 9, 8), **common))
        s.commit()
    return FactStore(engine)


def make(catalog, store, hits=KOTAK_CHUNKS + [OTHER_SCHEME] + GENERAL, keyword=None,
         obey_filter=True, min_relevance=0.60):
    vector = FakeIndex(hits, obey_filter)
    bm25 = FakeIndex(keyword if keyword is not None else hits, obey_filter)
    settings = RetrievalSettings(min_relevance=min_relevance)
    return Retriever(catalog, store, vector, bm25, settings), vector, bm25


def req(question, scheme_id=KOTAK, fields=(), metrics=(), cls=QueryClass.FACTUAL_SCHEME):
    return RetrievalRequest(cls, scheme_id, (), Intent(tuple(fields), tuple(metrics)), question)


# ------------------------------------------------------------------------------ facts first
def test_known_field_returns_the_fact_as_e1(catalog, store):
    r, _, _ = make(catalog, store)
    result = r.retrieve_sync(req("What is the expense ratio of Kotak Multi Cap?",
                                 fields=["expense_ratio"]))
    assert result.status == "ok"
    e1 = result.evidence[0]
    assert e1.id == "E1" and e1.kind == "fact"
    assert "Expense ratio: 0.42%" in e1.text
    assert e1.data["fields"] == {"expense_ratio": "0.42%"}
    assert e1.source_url == catalog.schemes[KOTAK].url  # citation policy: Groww page
    assert [e.id for e in result.evidence] == [f"E{i}" for i in range(1, len(result.evidence) + 1)]
    assert all(e.kind == "chunk" for e in result.evidence[1:])


def test_fact_date_is_the_last_confirmed_fetch(catalog, store):
    r, _, _ = make(catalog, store)
    e1 = r.retrieve_sync(req("TER?", fields=["expense_ratio"])).evidence[0]
    assert e1.last_updated == CONFIRMED


def test_several_fields_share_one_block(catalog, store):  # FLD-02
    r, _, _ = make(catalog, store)
    result = r.retrieve_sync(req("charges of Kotak Multi Cap",
                                 fields=["expense_ratio", "exit_load"]))
    facts = [e for e in result.evidence if e.kind == "fact"]
    assert len(facts) == 1
    assert set(facts[0].data["fields"]) == {"expense_ratio", "exit_load"}


def test_null_fact_falls_back_to_chunks(catalog, store):  # RET-02
    r, _, _ = make(catalog, store)
    result = r.retrieve_sync(req("What is the riskometer of Kotak Multi Cap Fund?",
                                 fields=["riskometer"]))
    assert all(e.kind == "chunk" for e in result.evidence)


def test_fund_managers_include_managing_since(catalog, store):
    r, _, _ = make(catalog, store)
    e1 = r.retrieve_sync(req("Who manages Kotak?", fields=["fund_managers"])).evidence[0]
    assert "Abhishek Bisen (managing since 08 Sep 2021), Devender Singhal" in e1.text


# ------------------------------------------------------------------------------ metrics
def test_metric_with_period_and_basis(catalog, store):
    r, _, _ = make(catalog, store)
    result = r.retrieve_sync(req("3-year return of Kotak Multi Cap", cls=QueryClass.FACTUAL_METRIC,
                                 metrics=[MetricIntent("return", "3y", "annualised")]))
    e1 = result.evidence[0]
    assert e1.kind == "metric"
    assert "3-year annualised return: +17.06% as of 06 Nov 2026" in e1.text
    assert "60.46" not in e1.text  # the absolute row was not asked for
    assert e1.data["rows"][0]["basis"] == "annualised"


def test_metric_without_period_lists_displayed_figures(catalog, store):
    r, _, _ = make(catalog, store)
    e1 = r.retrieve_sync(req("returns of Kotak Multi Cap", cls=QueryClass.FACTUAL_METRIC,
                             metrics=[MetricIntent("return")])).evidence[0]
    assert "1-year absolute return: +2.70%" in e1.text
    assert "3-year annualised return: +17.06%" in e1.text
    assert "60.46" not in e1.text  # not displayed on the page
    assert e1.text.index("1-year") < e1.text.index("3-year")


def test_hidden_metric_is_marked_and_rank_has_its_total(catalog, store):  # CLS-21
    r, _, _ = make(catalog, store)
    result = r.retrieve_sync(req("sharpe and rank", cls=QueryClass.FACTUAL_METRIC, metrics=[
        MetricIntent("sharpe_ratio", None, "point"),
        MetricIntent("category_rank", "3y", "point")]))
    sharpe, rank = result.evidence[0], result.evidence[1]
    assert "Sharpe ratio: 0.6 as of 06 Nov 2026 (from Groww's page data)" in sharpe.text
    assert "Groww rank within Multi Cap for 3-year return: 8 of 23" in rank.text


def test_holdings_and_peers(catalog, store):
    r, _, _ = make(catalog, store)
    result = r.retrieve_sync(req("top holdings and similar funds", cls=QueryClass.FACTUAL_METRIC,
                                 metrics=[MetricIntent("holdings"), MetricIntent("peers")]))
    holdings, peers = result.evidence[0], result.evidence[1]
    assert holdings.kind == "holdings" and "top 10 of 12 portfolio holdings as of 30 Sep 2026" \
        in holdings.text
    assert "Company 1 (Financial, Equity): 5.90% of assets" in holdings.text
    assert peers.kind == "peers" and peers.data["peers"] == ["Axis Multicap Fund Direct Growth"]


# ------------------------------------------------------------------------------ chunk search
def test_scheme_question_is_filtered_to_the_scheme(catalog, store):
    r, vector, bm25 = make(catalog, store, obey_filter=False)  # indexes ignore the filter
    result = r.retrieve_sync(req("ISIN of Kotak Multi Cap"))
    assert vector.calls[0][1] == {"scheme_id": KOTAK}
    assert bm25.calls[0][1] == {"scheme_id": KOTAK}
    assert {e.scheme_id for e in result.evidence} == {KOTAK}  # RET-03 defence in depth


def test_general_question_searches_only_general_documents(catalog, store):
    r, vector, _ = make(catalog, store)
    result = r.retrieve_sync(req("What is ELSS lock-in?", scheme_id=None,
                                 cls=QueryClass.FACTUAL_GENERAL))
    assert vector.calls[0][1] == {"doc_type": ["amfi", "sebi", "rta_help"]}
    assert result.evidence[0].chunk_id == "amfi-elss::elss::0"
    assert result.evidence[0].source_url == "https://example.org/amfi-elss::elss::0"


def test_general_class_ignores_a_named_scheme(catalog, store):  # RET-05
    r, vector, _ = make(catalog, store)
    r.retrieve_sync(req("download capital gains statement for Kotak",
                        cls=QueryClass.FACTUAL_GENERAL))
    assert vector.calls[0][1] == {"doc_type": ["amfi", "sebi", "rta_help"]}


def test_scheme_chunks_cite_the_groww_page(catalog, store):
    r, _, _ = make(catalog, store)
    result = r.retrieve_sync(req("ISIN of Kotak Multi Cap"))
    assert {e.source_url for e in result.evidence} == {catalog.schemes[KOTAK].url}
    assert result.evidence[0].last_updated == date(2026, 11, 9)


def test_keeps_the_top_five_chunks(catalog, store):
    many = [chunk(f"groww-kotak-multi-cap::s{i}::0", 0.9 - i / 100, "Kotak isin") for i in range(9)]
    r, _, _ = make(catalog, store, hits=many)
    assert len(r.retrieve_sync(req("isin of kotak")).evidence) == 5


def test_rrf_is_deterministic():
    assert rrf(["a", "b"], ["b", "a"]) == ["a", "b"]  # tie broken by id (RET-12)
    # c: 1/63 + 1/61 = 0.03227 just beats b: 2/62 = 0.03226
    assert rrf(["a", "b", "c"], ["c", "b"]) == ["c", "b", "a"]


def test_query_rewrite(catalog, store):
    r, vector, _ = make(catalog, store)
    r.retrieve_sync(req("and its benchmark?", fields=["benchmark"]))
    assert vector.calls[0][0] == "Kotak Multi Cap Fund: and its benchmark? benchmark"
    r.retrieve_sync(req("What is the ISIN of Kotak Multi Cap Fund?"))
    assert vector.calls[1][0] == "What is the ISIN of Kotak Multi Cap Fund?"


# ------------------------------------------------------------------------------ no evidence
def test_low_score_without_facts_is_no_evidence(catalog, store):
    r, _, _ = make(catalog, store, min_relevance=0.95)
    result = r.retrieve_sync(req("ISIN of Kotak Multi Cap"))
    assert result.status == "no_evidence" and result.evidence == ()
    assert result.fallback.url == catalog.schemes[KOTAK].url
    assert result.reason == "below min_relevance"


def test_question_words_missing_from_evidence_is_no_evidence(catalog, store):
    r, _, _ = make(catalog, store)
    result = r.retrieve_sync(req("What is the colour of Kotak Multi Cap Fund?"))
    assert result.status == "no_evidence"
    assert result.reason == "question words not in the evidence"


def test_a_fact_overrides_a_low_score(catalog, store):
    r, _, _ = make(catalog, store, min_relevance=0.99)
    assert r.retrieve_sync(req("TER?", fields=["expense_ratio"])).status == "ok"


def test_general_no_evidence_links_the_most_relevant_page(catalog, store):
    r, _, _ = make(catalog, store, min_relevance=0.9)
    result = r.retrieve_sync(req("What is an NFO?", scheme_id=None,
                                 cls=QueryClass.FACTUAL_GENERAL))
    assert result.status == "no_evidence"
    assert result.fallback.url == catalog.source_urls["amfi-elss"]


def test_content_terms(catalog, store):
    r, _, _ = make(catalog, store)
    assert r.content_terms("What is the colour of Kotak Multi Cap Fund?", KOTAK) == ["colour"]
    assert r.content_terms("Tell me about Kotak Multi Cap", KOTAK) == []
    # source names and "according to" say where to look, not what about (Phase 7 eval)
    assert r.content_terms("SIP instalment according to AMFI", None) == ["sip", "instalment"]


def test_spelling_variants_count_as_the_same_word():
    from app.pipeline.retriever import _close

    assert _close("instalment", "installment")
    assert _close("enrolment", "enrollment")
    assert not _close("instalment", "investment")


async def test_async_retrieve_runs_in_a_thread(catalog, store):
    r, _, _ = make(catalog, store)
    result = await r.retrieve(req("TER?", fields=["expense_ratio"]))
    assert result.evidence[0].kind == "fact"


def test_rejects_other_citation_policies(catalog, store):
    with pytest.raises(ValueError):
        Retriever(catalog, store, FakeIndex([]), FakeIndex([]), RetrievalSettings(), "amc")


# ------------------------------------------------------------------------------ real indexes
REAL_READY = (DATA_DIR / "chroma").exists() and (DATA_DIR / "bm25.pkl").exists() and (
    DATA_DIR / "app.db"
).exists()
real = pytest.mark.skipif(not REAL_READY, reason="needs the Phase 3 indexes in data/")


@pytest.fixture(scope="module")
def live():
    from app.config import get_settings

    retriever = Retriever.from_settings(get_settings())
    return retriever, Router(retriever.catalog)


async def ask(live, question, active=None):
    retriever, router = live
    route = await router.route_message(question, active_scheme_id=active)
    assert route.retrieval is not None, (question, route.route)
    return await retriever.retrieve(route.retrieval)


@real
@pytest.mark.slow
async def test_real_facts_first(live):
    result = await ask(
        live, "Whats the expense ratio of HDFC NIFTY Next 50 Index Fund Direct Growth fund?"
    )
    e1 = result.evidence[0]
    assert e1.kind == "fact" and e1.data["fields"]["expense_ratio"] == "0.38%"
    assert e1.source_url == "https://groww.in/mutual-funds/hdfc-nifty-next-50-index-fund-direct-growth"


@real
@pytest.mark.slow
@pytest.mark.parametrize("question", [
    "What is the exit load of Kotak Multi Cap?",
    "Who is the registrar of Kotak Multi Cap?",
    "Kotak Multi Cap investment objective",
    "Kotak Multi Cap expense ratio compared to HDFC",
])
async def test_real_kotak_question_never_returns_other_schemes(live, question):
    retriever, router = live
    route = await router.route_message(question)
    if route.retrieval is None:  # e.g. the comparison is refused before retrieval
        return
    result = await retriever.retrieve(route.retrieval)
    assert {e.scheme_id for e in result.evidence} == {KOTAK}


@real
@pytest.mark.slow
@pytest.mark.parametrize("question", [
    "What is the colour of HDFC Flexi Cap?",
    "Who is the CEO of HDFC Flexi Cap?",
    "Does Kotak Multi Cap Fund have a mobile app?",
])
async def test_real_nonsense_is_no_evidence(live, question):
    result = await ask(live, question)
    assert result.status == "no_evidence"
    assert result.fallback.url.startswith("https://groww.in/mutual-funds/")


# 20 hand-written factual questions -> what must be among the returned evidence blocks.
GOLDEN = [
    ("What is the expense ratio of HDFC NIFTY Next 50 Index Fund?", "fact:expense_ratio"),
    ("What is the exit load of Bandhan Small Cap Fund?", "fact:exit_load"),
    ("Minimum SIP amount for Parag Parikh Flexi Cap?", "fact:min_sip"),
    ("What is the riskometer level of ICICI Prudential Corporate Bond Fund?", "fact:riskometer"),
    ("What is the benchmark of ABSL Large Cap?", "fact:benchmark"),
    ("Who manages Edelweiss Mid Cap Fund?", "fact:fund_managers"),
    ("Is there a lock-in period for Nippon India Large Cap?", "fact:lock_in"),
    ("What is the 3-year return of Kotak Multi Cap Fund?", "metric:return"),
    ("What is the NAV of HDFC Flexi Cap?", "metric:nav"),
    ("What is the fund size of ABSL Small Cap?", "metric:aum"),
    ("What is the Sharpe ratio of PPFAS Flexi Cap?", "metric:sharpe_ratio"),
    ("Top holdings of HDFC Pharma and Healthcare Fund?", "holdings"),
    ("What is the ISIN of Kotak Multi Cap?", "chunk:groww-kotak-multi-cap::identifiers"),
    ("What is the investment objective of HDFC Silver ETF FoF?",
     "chunk:groww-hdfc-silver-etf-fof::investment-objective"),
    ("What is ELSS lock-in?", "chunk:amfi-elss::"),
    ("How do I download my CAS?", "chunk:(sebi-cas|rta-cams-cas)::"),
    ("What is an exit load?", "chunk:(amfi-glossary::exit-load|amfi-exit-load::)"),
    ("How is the riskometer of a scheme derived?", "chunk:amfi-riskometer::"),
    ("How do I get my capital gains statement from CAMS?", "chunk:rta-cams-capital-gains::"),
    ("What is a SIP?", "chunk:(amfi-sip::|amfi-glossary::sip)"),
]


def _matches(evidence, expected: str) -> bool:
    kind, _, key = expected.partition(":")
    if kind == "fact":
        return evidence.kind == "fact" and key in evidence.data["fields"]
    if kind == "metric":
        return evidence.kind == "metric" and evidence.data["metric"] == key
    if kind == "holdings":
        return evidence.kind == "holdings"
    return evidence.kind == "chunk" and bool(re.match(key, evidence.chunk_id or ""))


@real
@pytest.mark.slow
async def test_real_golden_questions_find_the_right_evidence(live):
    misses = []
    for question, expected in GOLDEN:
        result = await ask(live, question)
        if not any(_matches(e, expected) for e in result.evidence):
            misses.append((question, expected, [e.chunk_id or e.kind for e in result.evidence]))
    assert len(GOLDEN) == 20
    assert len(misses) <= 1, misses  # exit criterion: at least 19 of 20
