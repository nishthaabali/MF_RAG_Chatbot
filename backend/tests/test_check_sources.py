import copy

import yaml

from app.config import ROOT_DIR, load_settings
from ingest.check_sources import (
    check_aliases,
    check_education_links,
    check_sources,
    normalise,
    run_checks,
)

CONFIG = ROOT_DIR / "config"


def _load(name):
    return yaml.safe_load((CONFIG / name).read_text(encoding="utf-8"))


def test_real_config_passes():
    assert run_checks() == []


def test_citation_policy_is_groww():
    assert load_settings().citation_policy == "groww"


def test_corpus_size():
    sources = _load("sources.yaml")["sources"]
    schemes = [s for s in sources if s["type"] == "scheme_page"]
    general = [s for s in sources if s["type"] != "scheme_page"]
    assert len(schemes) == 19
    assert len(general) >= 10
    assert len({s["amc"] for s in schemes}) == 8


def test_duplicate_id_and_url_detected():
    sources = copy.deepcopy(_load("sources.yaml")["sources"])
    sources.append(copy.deepcopy(sources[0]))
    errors = check_sources(sources)
    assert any("duplicate source id" in e for e in errors)
    assert any("duplicate url" in e for e in errors)


def test_second_groww_page_for_a_scheme_detected():
    sources = copy.deepcopy(_load("sources.yaml")["sources"])
    extra = copy.deepcopy(sources[0])
    extra["id"] = "groww-extra"
    extra["url"] = "https://groww.in/mutual-funds/other"
    sources.append(extra)
    assert any("expected exactly 1" in e for e in check_sources(sources))


def test_invalid_urls_detected():
    sources = copy.deepcopy(_load("sources.yaml")["sources"])
    sources[0]["url"] = "http://groww.in/x"
    sources[-1]["url"] = "https://example.com/blog"
    errors = check_sources(sources)
    assert any("absolute https URL" in e for e in errors)
    assert any("not an AMFI/SEBI/RTA domain" in e for e in errors)


def test_alias_to_unknown_scheme_detected():
    sources = _load("sources.yaml")["sources"]
    aliases = copy.deepcopy(_load("aliases.yaml"))
    aliases["schemes"]["no-such-scheme"] = ["Imaginary Fund"]
    assert any("does not exist" in e for e in check_aliases(aliases, sources))


def test_duplicate_alias_across_schemes_detected():
    sources = _load("sources.yaml")["sources"]
    aliases = copy.deepcopy(_load("aliases.yaml"))
    aliases["schemes"]["kotak-multi-cap"].append("ABSL Large Cap")
    assert any("used by both" in e for e in check_aliases(aliases, sources))


def test_scheme_without_aliases_detected():
    sources = _load("sources.yaml")["sources"]
    aliases = copy.deepcopy(_load("aliases.yaml"))
    del aliases["schemes"]["kotak-multi-cap"]
    assert any("has no aliases" in e for e in check_aliases(aliases, sources))


def test_education_link_must_reference_allowlisted_non_scheme_source():
    sources = _load("sources.yaml")["sources"]
    education = copy.deepcopy(_load("education_links.yaml"))
    education["topics"]["risk"] = "missing-source"
    education["topics"]["tax"] = "groww-kotak-multi-cap"
    del education["topics"]["sip_basics"]
    errors = check_education_links(education, sources)
    assert any("not in sources.yaml" in e for e in errors)
    assert any("not a scheme page" in e for e in errors)
    assert any("missing required topic" in e for e in errors)


def test_normalise_ignores_case_and_punctuation():
    assert normalise("ABSL Small & Midcap") == normalise("absl small and midcap")
    assert normalise("Kotak Multi-Cap") == "kotak multi cap"


def test_invalid_paginate_block_detected():
    sources = copy.deepcopy(_load("sources.yaml")["sources"])
    glossary = next(s for s in sources if s["id"] == "amfi-glossary")
    assert glossary["paginate"]["items"]  # the real config uses pagination
    glossary["paginate"] = {"pages": "", "max_pages": 500, "next": "a"}
    errors = " | ".join(check_sources(sources))
    assert "paginate.pages" in errors
    assert "paginate.items" in errors
    assert "max_pages" in errors
    assert "unknown paginate key(s): next" in errors
