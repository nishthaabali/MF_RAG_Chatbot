"""Step 7: build the final message from parts the model doesn't control (architecture §6.7).

    <answer, <= 3 sentences>

    Source: <source_url of the cited evidence block>

    Last updated from sources: <fetched_at of that block, e.g. 09 Nov 2026>

The URL and the date both come from the evidence block the model named, never from the model.
"""

from app.pipeline.catalog import Catalog
from app.pipeline.retriever import Evidence
from app.pipeline.templates import Reply


def format_answer(answer: str, evidence: Evidence, catalog: Catalog | None = None) -> Reply:
    last_updated = evidence.last_updated
    if last_updated is None and catalog is not None:  # FMT-03: use the source's last fetch
        if evidence.source_id in catalog.source_urls:
            last_updated = catalog.link(evidence.source_id).last_updated
    return Reply("answer", answer.strip(), evidence.source_url, last_updated)
