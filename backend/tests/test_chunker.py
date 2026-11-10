import re
from pathlib import Path

import pytest

from app.config import ChunkingSettings, load_settings
from app.pipeline.pii_patterns import find_pii_kinds
from ingest.chunker import (
    _is_menu_like,
    chunk_corpus,
    chunk_document,
    split_sentences,
    token_counter,
)
from ingest.parsers import parse_generic, parse_groww

FIXTURES = Path(__file__).parent / "fixtures"
CFG = ChunkingSettings()
SCHEME = "Kotak Multi Cap Fund Direct Growth"
FETCHED = "2026-11-09T13:39:38+00:00"


def words(text: str) -> int:
    """Deterministic stand-in for the tokenizer in logic tests."""
    return len(text.split())


@pytest.fixture(scope="module")
def count():
    return token_counter("BAAI/bge-small-en-v1.5")


def scheme_doc(sections, title=SCHEME):
    return {
        "source_id": "groww-kotak-multi-cap",
        "url": "https://groww.in/mutual-funds/kotak-multi-cap-fund-direct-growth",
        "type": "scheme_page",
        "scheme_id": "kotak-multi-cap",
        "amc": "kotak",
        "title": title,
        "fetched_at": FETCHED,
        "sections": sections,
    }


def prose_doc(sections, source_id="amfi-x", doc_type="amfi", title="AMFI page"):
    return {
        "source_id": source_id,
        "url": f"https://www.mutualfundssahihai.com/en/{source_id}",
        "type": doc_type,
        "scheme_id": None,
        "amc": None,
        "title": title,
        "fetched_at": FETCHED,
        "sections": sections,
    }


@pytest.fixture(scope="module")
def fixture_docs():
    groww = parse_groww((FIXTURES / "groww_kotak_multi_cap.html").read_text(encoding="utf-8"))
    amfi = parse_generic((FIXTURES / "amfi_exit_load.html").read_text(encoding="utf-8"))
    return [
        scheme_doc(groww.sections, title=groww.facts["scheme_name"]),
        prose_doc(amfi["sections"], "amfi-exit-load", title=amfi["title"]),
    ]


# --------------------------------------------------------------------------- real fixtures


def test_config_loads_chunking_block():
    cfg = load_settings().chunking
    assert cfg.max_tokens == 350 and cfg.min_tokens == 12 and cfg.version
    assert any(re.match(p, "Portfolio holdings 1-25 of 77") for p in cfg.exclude_sections)


def test_fixture_chunks_respect_size_limit_and_metadata(fixture_docs, count):
    result = chunk_corpus(fixture_docs, CFG, count)
    assert result.chunks
    for c in result.chunks:
        assert count(c.text) <= CFG.max_tokens, c.chunk_id
        for name in ("chunk_id", "text", "source_id", "source_url", "doc_type", "fetched_at"):
            assert getattr(c, name), (c.chunk_id, name)
        assert c.section_kind in {
            "facts",
            "returns",
            "risk",
            "manager",
            "fund_house",
            "identifiers",
            "objective",
            "prose",
        }
        assert c.chunker_version == CFG.version
        assert c.chunk_id.startswith(f"{c.source_id}::")


def test_every_scheme_chunk_has_header_and_starts_with_scheme_name(fixture_docs, count):
    chunks = chunk_document(fixture_docs[0], CFG, count).chunks
    name = fixture_docs[0]["title"]
    for c in chunks:
        header, body = c.text.split("\n", 1)
        assert header.startswith(f"[{name}] {c.section}")
        assert body.startswith(f"{name}.")
        assert c.scheme_id == "kotak-multi-cap" and c.amc == "kotak"


def test_holdings_and_similar_funds_are_excluded(fixture_docs, count):
    result = chunk_document(fixture_docs[0], CFG, count)
    sections = {c.section for c in result.chunks}
    assert not any(s.startswith(("Portfolio holdings", "Similar funds")) for s in sections)
    excluded = [d for d in result.dropped if d.reason == "answered from SQL"]
    assert any(d.section.startswith("Portfolio holdings") for d in excluded)


def test_section_kinds_synonyms_and_as_of(fixture_docs, count):
    by_section = {c.section: c for c in chunk_document(fixture_docs[0], CFG, count).chunks}
    costs = by_section["Costs and exit load"]
    assert costs.section_kind == "facts" and "TER" in costs.text.split("\n")[0]
    assert by_section["Investment objective"].section_kind == "objective"
    assert by_section["Annualised returns"].section_kind == "returns"
    assert by_section["Annualised returns"].as_of == "2026-11-06"
    assert by_section["Risk measures"].section_kind == "risk"
    assert by_section["Identifiers"].section_kind == "identifiers"
    assert any(c.section_kind == "manager" for c in by_section.values())


def test_chunk_ids_are_unique_and_stable(fixture_docs, count):
    first = [c.chunk_id for c in chunk_corpus(fixture_docs, CFG, count).chunks]
    second = [c.chunk_id for c in chunk_corpus(fixture_docs, CFG, count).chunks]
    assert first == second
    assert len(set(first)) == len(first)


def test_no_chunk_contains_personal_data(fixture_docs, count):
    for c in chunk_corpus(fixture_docs, CFG, count).chunks:
        assert find_pii_kinds(c.text) == [], c.chunk_id


# --------------------------------------------------------------------------- splitting


def test_long_scheme_list_repeats_lead_and_intro_in_every_piece():
    intro = "Absolute returns as of 06 Nov 2026, as published by Groww:"
    items = [f"{n}-month +{n}.25%" for n in range(1, 41)]
    body = f"{SCHEME}. {intro} {'; '.join(items)}."
    cfg = ChunkingSettings(max_tokens=60)
    chunks = chunk_document(scheme_doc([{"heading": "Absolute returns", "text": body}]), cfg, words)
    assert len(chunks.chunks) > 1
    seen = []
    for c in chunks.chunks:
        assert words(c.text) <= cfg.max_tokens
        piece = c.text.split("\n", 1)[1]
        assert piece.startswith(f"{SCHEME}. {intro} ")
        seen += piece.removeprefix(f"{SCHEME}. {intro} ").rstrip(".").split("; ")
        assert c.as_of == "2026-11-06"
    assert seen == items  # every item exactly once, in order, no overlap
    assert [c.chunk_id for c in chunks.chunks] == [
        f"groww-kotak-multi-cap::absolute-returns::{n}" for n in range(len(chunks.chunks))
    ]


def test_prose_split_has_one_sentence_overlap():
    sentences = [f"Sentence number {n} explains one point about mutual funds." for n in range(30)]
    cfg = ChunkingSettings(max_tokens=50)
    doc = prose_doc([{"heading": "Basics", "text": " ".join(sentences)}])
    chunks = chunk_document(doc, cfg, words).chunks
    assert len(chunks) > 1
    pieces = [split_sentences(c.text.split("\n", 1)[1]) for c in chunks]
    for prev, nxt in zip(pieces, pieces[1:], strict=False):
        assert nxt[0] == prev[-1]
    assert set(s for p in pieces for s in p) == set(sentences)
    assert all(words(c.text) <= cfg.max_tokens for c in chunks)


def test_single_oversized_sentence_is_split_on_words():
    text = " ".join(f"word{n}" for n in range(200)) + "."
    cfg = ChunkingSettings(max_tokens=40)
    chunks = chunk_document(prose_doc([{"heading": "Long", "text": text}]), cfg, words).chunks
    assert len(chunks) > 1 and all(words(c.text) <= cfg.max_tokens for c in chunks)


def test_sentence_splitter_keeps_abbreviations_and_decimals():
    text = "Mr. Joshi is a PGDM. Expense ratio: 0.70%. Deduction up to Rs. 1.5 lacs is allowed."
    assert split_sentences(text) == [
        "Mr. Joshi is a PGDM.",
        "Expense ratio: 0.70%.",
        "Deduction up to Rs. 1.5 lacs is allowed.",
    ]


# --------------------------------------------------------------------------- noise rules

GOOD = "An exit load is a fee charged when units are redeemed before a set period ends."


@pytest.mark.parametrize(
    ("section", "reason"),
    [
        ({"heading": "Quick Links", "text": GOOD}, "navigation heading"),
        ({"heading": "Dark", "text": GOOD}, "navigation heading"),
        (
            {"heading": None, "text": "Home\nAbout us\nInvestors\nDistributors\nContact\nSearch"},
            "menu-like block",
        ),
        ({"heading": "Banner", "text": "Know More"}, "under 12 tokens"),
        ({"heading": None, "text": "Navigated to Mutual Fund Glossary"}, "under 12 tokens"),
        (
            {"heading": "Schedule", "text": f"{GOOD} Contact Ravi on 9876543210 for details."},
            "personal data (phone)",
        ),
        (
            {"heading": "Help", "text": f"{GOOD} Write to help.desk@example.com today."},
            "personal data (email)",
        ),
        (
            {"heading": "Help", "text": f"{GOOD} Quote PAN ABCDE1234F in the form."},
            "personal data (pan)",
        ),
    ],
)
def test_noise_rules_drop_their_examples(section, reason):
    result = chunk_document(prose_doc([section]), CFG, words)
    assert result.chunks == []
    assert [d.reason for d in result.dropped] == [reason]


def test_navigated_to_line_is_stripped_but_content_kept():
    section = {"heading": "Glossary", "text": f"Navigated to Mutual Fund Glossary\n{GOOD}"}
    (chunk,) = chunk_document(prose_doc([section]), CFG, words).chunks
    assert "Navigated to" not in chunk.text and GOOD in chunk.text


def test_flattened_table_inside_prose_is_not_menu_like():
    """Regression: the glossary's Expense Ratio entry (prose + a TER table) was dropped."""
    prose = "\n".join(
        "The total expense ratio of a scheme is charged as a percentage of daily net assets."
        for _ in range(4)
    )
    table = "\n".join(["AUM (Crore)", "0 - 500", "2.25", "2.00", "500 - 750", "1.75", "1.50"] * 2)
    assert not _is_menu_like(f"{prose}\n{table}")
    assert _is_menu_like(table)


def test_public_helplines_and_codes_are_not_treated_as_personal_data():
    text = (
        "SEBI toll free helpline: 1800 266 7575. ISIN: INF879O01027; AMFI scheme code: 122639. "
        "Fund size: ₹30,335.28 Cr. Minimum SIP ₹1000. PIN 400069. NIFTY 100."
    )
    assert find_pii_kinds(text) == []
    assert find_pii_kinds("call +91 98765 43210") == ["phone"]
    assert find_pii_kinds("Aadhaar 2345 6789 0123") == ["aadhaar"]
