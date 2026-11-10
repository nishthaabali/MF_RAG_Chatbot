"""CLI against the real indexes, templates only (--no-llm), so tests never spend API quota."""

import json
import re

import pytest

from app import cli
from app.config import DATA_DIR

READY = (DATA_DIR / "chroma").exists() and (DATA_DIR / "app.db").exists()
pytestmark = [pytest.mark.slow, pytest.mark.skipif(not READY, reason="needs data/ indexes")]


@pytest.fixture
def threads(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "THREADS_DIR", tmp_path)
    return tmp_path


def run(capsys, *args):
    assert cli.main(list(args)) == 0
    return capsys.readouterr().out


@pytest.mark.parametrize(
    "question",
    [
        "What is the expense ratio of Kotak Multi Cap Fund?",
        "Should I invest in Kotak Multi Cap?",
        "My PAN is ABCDE1234F",
        "What is the colour of HDFC Flexi Cap?",
        "What is the exit load of the large cap fund?",
        "Hi",
    ],
)
def test_every_cli_reply_has_one_link_and_the_footer(capsys, threads, question):
    out = run(capsys, "--no-llm", question)
    reply = out.split("\n\n[route:")[0]
    assert len(re.findall(r"https?://\S+", reply)) == 1
    assert re.search(r"Last updated from sources: \d{2} \w{3} \d{4}$", reply)


def test_thread_keeps_context(capsys, threads):
    run(capsys, "--no-llm", "--thread", "t", "What is the exit load of Kotak Multi Cap?")
    out = run(capsys, "--no-llm", "--thread", "t", "and its benchmark?")
    assert "Kotak Multi Cap Fund" in out and "active scheme: kotak-multi-cap" in out
    saved = json.loads((threads / "t.json").read_text(encoding="utf-8"))
    assert saved["active_scheme_id"] == "kotak-multi-cap" and len(saved["history"]) == 4


def test_json_output(capsys, threads):
    data = json.loads(run(capsys, "--no-llm", "--json", "minimum SIP of Bandhan Small Cap"))
    assert data["type"] == "answer" and data["answer"].endswith("is ₹100.")
    assert data["citation_url"].startswith("https://groww.in/")
