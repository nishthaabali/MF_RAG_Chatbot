"""docs/issues.md #1 and #2: every fund manager listed, consistently, and their education and
experience answered (resolver -> retriever -> validator -> templates), without the LLM."""

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from app.config import RetrievalSettings
from app.pipeline.catalog import Catalog, load_managers
from app.pipeline.classifier import QueryClass
from app.pipeline.generator import GeneratedAnswer
from app.pipeline.orchestrator import deterministic_reply, intent_from_dict, intent_to_dict
from app.pipeline.retriever import FactStore, Retriever
from app.pipeline.router import RetrievalRequest
from app.pipeline.scheme_resolver import Intent, SchemeResolver, detect_intent
from app.pipeline.templates import manager_sentences
from app.pipeline.text import split_sentences
from app.pipeline.validator import validate
from app.store.db import init_db, make_engine, session_factory
from app.store.models import FundManager, SchemeFact, Source

KOTAK = "kotak-multi-cap"
PPFAS = "ppfas-flexi-cap"
FETCHED = datetime(2026, 11, 9, 4, 0, tzinfo=UTC)
BISEN_EDU = "Mr. Bisen has done B.Com and MBA."
BISEN_EXP = ("Prior to joining Kotak AMC, he was working with Securities Trading Corporation Of "
             "India Ltd. His earlier assignments include 2 years of merchant banking.")
SINGHAL_EDU = "Mr. Singhal is an MBA (Finance)."
SINGHAL_EXP = "He has worked with Kotak Securities Ltd and P N Vijay Financial Services Pvt. Ltd."


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    init_db(engine)
    url = "https://groww.in/mutual-funds/kotak-multi-cap-fund-direct-growth"
    common = {"scheme_id": KOTAK, "source_id": "groww-kotak-multi-cap", "fetched_at": FETCHED}
    with session_factory(engine)() as s:
        s.add(Source(id="groww-kotak-multi-cap", url=url, type="scheme_page", scheme_id=KOTAK,
                     amc="kotak", fetched_at=FETCHED))
        s.flush()
        s.add(SchemeFact(scheme_name="Kotak Multi Cap Fund", expense_ratio="0.42%",
                         fund_managers=["Abhishek Bisen", "Devender Singhal"], source_url=url,
                         **common))
        s.add_all([
            FundManager(name="Abhishek Bisen", managing_since=date(2021, 9, 8),
                        education=BISEN_EDU, experience=BISEN_EXP, **common),
            FundManager(name="Devender Singhal", managing_since=date(2021, 9, 8),
                        education=SINGHAL_EDU, experience=SINGHAL_EXP, **common),
            # A manager of a scheme outside the catalog is ignored.
            FundManager(name="Someone Else", scheme_id="not-covered", source_id="x",
                        fetched_at=FETCHED),
        ])
        s.commit()
    return engine


@pytest.fixture
def catalog(engine):
    cat = Catalog.load(engine=engine)
    managers = {**cat.managers, "Rajeev Thakkar": (PPFAS,),
                "Dhruv Muchhal": ("hdfc-flexi-cap", "hdfc-pharma-healthcare")}
    return replace(cat, managers=managers)


class NoChunks:
    def query(self, text, where=None, k=8):
        return []


def retrieve(catalog, engine, fields, managers=()):
    r = Retriever(catalog, FactStore(engine), NoChunks(), NoChunks(), RetrievalSettings())
    request = RetrievalRequest(QueryClass.FACTUAL_SCHEME, KOTAK, (),
                               Intent(tuple(fields), (), tuple(managers)), "q")
    return request, r.retrieve_sync(request)


# ------------------------------------------------------------------------------ catalog
def test_catalog_maps_managers_to_covered_schemes(engine):
    managers = load_managers(engine, [KOTAK, PPFAS])
    assert managers == {"Abhishek Bisen": (KOTAK,), "Devender Singhal": (KOTAK,)}


# ------------------------------------------------------------------------------ intent
@pytest.mark.parametrize("text, fields", [
    ("Who runs Kotak Multi Cap?", ("fund_managers",)),
    ("Who is managing Kotak Multi Cap?", ("fund_managers",)),
    ("Who handles Kotak Multi Cap?", ("fund_managers",)),
    ("Name the people managing it", ("fund_managers",)),
    ("Who is the portfolio manager of Kotak Multi Cap?", ("fund_managers",)),
    ("What is the education of the fund manager?", ("fund_managers", "manager_education")),
    ("What are the qualifications of its managers?", ("fund_managers", "manager_education")),
    ("How experienced is the manager?", ("fund_managers", "manager_experience")),
    ("Where has the fund manager worked before?", ("fund_managers", "manager_experience")),
    ("Tell me about the fund managers' background",
     ("fund_managers", "manager_education", "manager_experience")),
])
def test_manager_phrasings(text, fields):
    intent = detect_intent(text)
    assert intent.fields == fields
    assert not intent.metrics  # "portfolio manager" is not a holdings question


def test_holdings_still_detected():
    assert [m.metric for m in detect_intent("Top holdings of its portfolio").metrics] == \
        ["holdings"]


def test_manager_name_resolves_to_their_scheme(catalog):
    r = SchemeResolver(catalog)
    res, intent = r.analyse("What is the experience of Rajeev Thakkar?")
    assert (res.status, res.via, res.scheme_id) == ("resolved", "manager", PPFAS)
    assert intent.managers == ("Rajeev Thakkar",)
    assert intent.fields == ("fund_managers", "manager_experience")


def test_bare_manager_name_asks_for_the_whole_profile(catalog):
    _, intent = SchemeResolver(catalog).analyse("Who is Abhishek Bisen?")
    assert intent.fields == ("fund_managers", "manager_education", "manager_experience")


def test_manager_of_several_schemes_prefers_the_active_one(catalog):
    r = SchemeResolver(catalog)
    res, _ = r.analyse("Who is Dhruv Muchhal?", active_scheme_id="hdfc-pharma-healthcare")
    assert res.scheme_id == "hdfc-pharma-healthcare"
    res, _ = r.analyse("Who is Dhruv Muchhal?", active_scheme_id=KOTAK)
    assert res.scheme_id == "hdfc-flexi-cap"


def test_unique_surname_counts_only_in_a_manager_question(catalog):
    r = SchemeResolver(catalog)
    _, intent = r.analyse("What did fund manager Thakkar study?")
    assert intent.managers == ("Rajeev Thakkar",)
    _, intent = r.analyse("Thakkar")
    assert intent.managers == ()


def test_scheme_name_wins_over_manager_scheme(catalog):
    res, intent = SchemeResolver(catalog).analyse(
        "Experience of Rajeev Thakkar at Kotak Multi Cap?")
    assert res.scheme_id == KOTAK and intent.managers == ("Rajeev Thakkar",)


def test_intent_round_trips_with_managers():
    intent = Intent(("fund_managers", "manager_experience"), (), ("Rajeev Thakkar",))
    assert intent_from_dict(intent_to_dict(intent)) == intent
    assert intent_from_dict({"fields": ["exit_load"], "metrics": []}).managers == ()  # old rows


# ------------------------------------------------------------------------------ evidence
def test_names_only_block_lists_every_manager_with_dates(catalog, engine):
    _, result = retrieve(catalog, engine, ["fund_managers"])
    e1 = result.evidence[0]
    assert ("Fund managers (all 2, in order): Abhishek Bisen (managing since 08 Sep 2021), "
            "Devender Singhal (managing since 08 Sep 2021)") in e1.text
    assert "education" not in e1.text
    assert e1.data["fields"]["fund_managers"] == ["Abhishek Bisen", "Devender Singhal"]


def test_profile_block_has_education_and_experience_in_one_block(catalog, engine):
    _, result = retrieve(catalog, engine,
                         ["fund_managers", "manager_education", "manager_experience"])
    assert len(result.evidence) == 1
    text = result.evidence[0].text
    for part in (BISEN_EDU, BISEN_EXP, SINGHAL_EDU, SINGHAL_EXP):
        assert part in text


def test_named_manager_narrows_the_block(catalog, engine):
    _, result = retrieve(catalog, engine, ["fund_managers", "manager_education"],
                         managers=["Devender Singhal"])
    e1 = result.evidence[0]
    assert "one of the 2 managers" in e1.text and SINGHAL_EDU in e1.text
    assert "Abhishek Bisen" not in e1.text
    assert e1.data["narrowed"] is True


# ------------------------------------------------------------------------------ validator
def test_validator_requires_every_manager(catalog, engine):
    request, result = retrieve(catalog, engine, ["fund_managers"])
    partial = GeneratedAnswer(answer="The fund manager of Kotak Multi Cap Fund is Abhishek Bisen.",
                              evidence_id="E1", answerable=True)
    assert validate(partial, result.evidence, request.intent).failures == ("fact_consistency",)
    full = GeneratedAnswer(answer="The fund managers of Kotak Multi Cap Fund are Abhishek Bisen "
                                  "and Devender Singhal (both managing since 08 Sep 2021).",
                           evidence_id="E1", answerable=True)
    assert validate(full, result.evidence, request.intent).ok


def test_pvt_ltd_is_not_a_sentence_end():
    assert len(split_sentences("He worked with Invesco Asset Management (India) Pvt. Ltd. "
                               "and Fidelity.")) == 1


# ------------------------------------------------------------------------------ templates
def test_template_lists_all_managers_with_dates(catalog, engine):
    request, result = retrieve(catalog, engine, ["fund_managers"])
    reply = deterministic_reply(catalog, request, result.evidence)
    assert reply.answer == ("The fund managers of Kotak Multi Cap Fund are Abhishek Bisen "
                            "(managing since 08 Sep 2021) and Devender Singhal (managing since "
                            "08 Sep 2021).")


def test_profile_template_quotes_both_managers_within_three_sentences(catalog, engine):
    request, result = retrieve(catalog, engine,
                               ["fund_managers", "manager_education", "manager_experience"])
    reply = deterministic_reply(catalog, request, result.evidence)
    sentences = split_sentences(reply.answer)
    assert len(sentences) == 3
    assert sentences[1].startswith("Education, as published: Abhishek Bisen - Mr. Bisen has done")
    assert "Devender Singhal - Mr. Singhal is an MBA (Finance)" in sentences[1]
    # The profile's own sentence break becomes a semicolon, wording otherwise unchanged.
    assert ("Securities Trading Corporation Of India Ltd; his earlier assignments include 2 years"
            in sentences[2])
    assert "P N Vijay Financial Services Pvt. Ltd." in sentences[2]


def test_narrowed_template_does_not_claim_a_sole_manager(catalog, engine):
    request, result = retrieve(catalog, engine, ["fund_managers", "manager_experience"],
                               managers=["Devender Singhal"])
    reply = deterministic_reply(catalog, request, result.evidence)
    assert reply.answer.startswith("Devender Singhal (managing since 08 Sep 2021) is a fund "
                                   "manager of Kotak Multi Cap Fund.")
    assert "Experience, as published: He has worked with Kotak Securities" in reply.answer


def test_missing_profile_says_not_published():
    rows = [{"name": "A B", "managing_since": None, "education": None, "experience": None}]
    assert manager_sentences("X Fund", rows, ("manager_education",)) == [
        "The fund manager of X Fund is A B.", "Education, as published: not published."]


@pytest.mark.parametrize("n", [1, 4, 7])
def test_manager_template_is_at_most_three_sentences(n):
    rows = [{"name": f"Person {i}", "managing_since": date(2020, 1, i + 1),
             "education": "Mr. P has done B.Com. He is also a CFA.",
             "experience": "He worked with X Pvt. Ltd. and Y."} for i in range(n)]
    sentences = manager_sentences("X Fund", rows, ("manager_education", "manager_experience"))
    assert len(split_sentences(" ".join(sentences))) == 3
