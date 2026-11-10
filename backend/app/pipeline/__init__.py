"""Answer pipeline (Phases 4-6).

`answer_message(thread_ctx, text)` runs one message through the whole pipeline: PII guard ->
resolver -> classifier -> retriever -> generator -> validator -> formatter. The heavy modules
are imported on first use, so importing a small helper (e.g. `pii_patterns` from ingestion)
stays cheap.
"""

_default = None


async def answer_message(thread_ctx, text: str):
    """Answer one message with the pipeline built from config/settings.yaml (built once)."""
    global _default
    if _default is None:
        from app.config import get_settings
        from app.pipeline.orchestrator import AnswerPipeline

        _default = AnswerPipeline.from_settings(get_settings())
    return await _default.answer(thread_ctx, text)


def __getattr__(name: str):
    if name in ("AnswerPipeline", "ThreadContext", "PipelineResult"):
        from app.pipeline import orchestrator

        return getattr(orchestrator, name)
    raise AttributeError(name)
