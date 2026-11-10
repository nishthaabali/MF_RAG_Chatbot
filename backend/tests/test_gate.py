"""Phase 11 quality gate: each rule fails on a crafted bad run and passes on a good one."""

import json

import pytest

from ingest.gate import evaluate, markdown, run
from tests.corpus_fixture import corpus_settings, make_corpus

GOOD_REPORT = {"ok": True, "ingest": {"processed": 38, "failures": {}}}
PREVIOUS = {"id": "2026-11-09-101500", "chunk_count": 387, "schemes": 2,
            "core_nulls": {"kotak-multi-cap": [], "hdfc-flexi-cap": ["expense_ratio"]},
            "sources": {"groww-kotak-multi-cap": {"sha256": "old"}}}
CURRENT = {"chunk_count": 390, "schemes": 2,
           "core_nulls": {"kotak-multi-cap": [], "hdfc-flexi-cap": ["expense_ratio"]},
           "sources": {"groww-kotak-multi-cap": {"sha256": "new"}}}


def failed(checks) -> set[str]:
    return {c.name for c in checks if not c.ok}


def test_a_good_run_passes():
    checks = evaluate(GOOD_REPORT, CURRENT, PREVIOUS)
    assert failed(checks) == set()


def test_unfinished_ingest_fails():
    assert "ingest_finished" in failed(evaluate({**GOOD_REPORT, "ok": False}, CURRENT, PREVIOUS))


def test_too_many_fetch_failures_fail():
    many = {f"s{i}": "timeout" for i in range(12)}  # 12/38 = 32%
    report = {"ok": True, "ingest": {"processed": 38, "failures": many}}
    assert "fetch_failures" in failed(evaluate(report, CURRENT, PREVIOUS))


def test_a_few_fetch_failures_pass_with_a_warning():
    report = {"ok": True, "ingest": {"processed": 38, "failures": {"amfi-sip": "timeout"}}}
    checks = evaluate(report, CURRENT, PREVIOUS)
    assert failed(checks) == set()
    assert any(c.warning and "amfi-sip" in c.detail for c in checks)


def test_a_core_fact_that_goes_missing_fails():
    current = {**CURRENT, "core_nulls": {"kotak-multi-cap": ["exit_load", "min_sip"],
                                         "hdfc-flexi-cap": ["expense_ratio"]}}
    checks = evaluate(GOOD_REPORT, current, PREVIOUS)
    assert failed(checks) == {"core_facts"}
    detail = next(c.detail for c in checks if c.name == "core_facts")
    assert "kotak-multi-cap: exit_load, min_sip" in detail


def test_a_scheme_that_disappears_fails():
    current = {**CURRENT, "core_nulls": {"kotak-multi-cap": []}}
    assert "schemes_present" in failed(evaluate(GOOD_REPORT, current, PREVIOUS))


def test_a_fact_already_missing_yesterday_is_accepted():
    # hdfc-flexi-cap's expense ratio was NULL before too (documented in Phase 2).
    assert failed(evaluate(GOOD_REPORT, CURRENT, PREVIOUS)) == set()


@pytest.mark.parametrize("count, ok", [(194, False), (300, False), (310, True), (464, True),
                                       (465, False)])
def test_chunk_count_change_limit(count, ok):
    checks = evaluate(GOOD_REPORT, {**CURRENT, "chunk_count": count}, PREVIOUS)
    assert ("chunk_count" not in failed(checks)) is ok


def test_first_run_has_nothing_to_compare_with():
    current = {**CURRENT, "core_nulls": {"hdfc-flexi-cap": ["expense_ratio"]}}
    checks = evaluate(GOOD_REPORT, current, None)
    assert failed(checks) == set()
    assert any(c.name == "core_facts" and c.warning for c in checks)


def test_markdown_lists_checks_and_changed_sources():
    checks = evaluate(GOOD_REPORT, CURRENT, PREVIOUS)
    md = markdown(checks, True, CURRENT, PREVIOUS)
    assert md.startswith("## Daily data refresh: PASSED")
    assert "| chunk_count | ok |" in md and "groww-kotak-multi-cap" in md
    md = markdown(checks, False, CURRENT, PREVIOUS)
    assert "previous bundle stays published" in md


def test_run_reads_the_corpus_and_reports(tmp_path):
    data = tmp_path / "data"
    settings = corpus_settings(tmp_path, data)
    make_corpus(data, settings)
    report = tmp_path / "ingest_report.json"
    report.write_text(json.dumps({"ok": True, "ingest": {"processed": 1, "failures": {}}}),
                      encoding="utf-8")
    previous = tmp_path / "bundle.json"
    previous.write_text(json.dumps({**PREVIOUS, "chunk_count": 4}), encoding="utf-8")
    passed, out, md = run(report, previous, settings=settings, data_dir=data, with_index=False)
    assert passed and out["previous_bundle"] == "2026-11-09-101500"
    assert {c["name"] for c in out["checks"]} >= {"ingest_finished", "core_facts", "chunk_count"}
