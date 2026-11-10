"""Formatter: the citation and the footer date come from the cited evidence block."""

import re
from dataclasses import replace
from datetime import UTC, date, datetime

from app.pipeline.catalog import Catalog
from app.pipeline.formatter import format_answer
from app.pipeline.retriever import Evidence

URL = "https://www.mutualfundssahihai.com/en/elss-fund-tax-saving-mutual-fund"


def block(fetched_at):
    return Evidence("E2", "chunk", "ELSS has a 3-year lock-in.", "amfi-elss", URL, fetched_at)


def test_footer_date_is_the_cited_blocks_fetch_date_in_ist():
    # 20:00 UTC on 9 Nov is 01:30 on 10 Nov in India
    reply = format_answer("ELSS funds have a lock-in of 3 years. ",
                          block(datetime(2026, 11, 9, 20, 0, tzinfo=UTC)))
    assert reply.last_updated == date(2026, 11, 10)
    assert reply.rendered.endswith("Last updated from sources: 10 Nov 2026")
    assert reply.answer == "ELSS funds have a lock-in of 3 years."


def test_exactly_one_url_and_it_is_the_blocks():
    reply = format_answer("ELSS funds have a lock-in of 3 years.",
                          block(datetime(2026, 11, 9, tzinfo=UTC)))
    assert re.findall(r"https?://\S+", reply.rendered) == [URL]
    assert reply.to_dict()["citation_url"] == URL and reply.type == "answer"


def test_missing_date_falls_back_to_the_sources_table():  # FMT-03
    catalog = replace(Catalog.load(), fetched_at={"amfi-elss": date(2026, 11, 8)})
    reply = format_answer("ELSS funds have a lock-in of 3 years.", block(None), catalog)
    assert reply.last_updated == date(2026, 11, 8)
    assert "None" not in format_answer("x.", block(None)).rendered
