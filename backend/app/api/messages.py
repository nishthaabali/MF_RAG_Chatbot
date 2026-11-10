"""POST /api/threads/{id}/messages: run one message through the pipeline (architecture §8).

Per thread, messages are processed one at a time in arrival order (ThreadLocks); different
threads run concurrently, sharing one Groq client whose limiter and semaphore keep the whole
process inside the account's rate limits. Only the PII-safe form of the user message is stored:
the placeholder when the PII guard blocked it.
"""

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, status

from app.api.deps import ServicesDep
from app.api.schemas import MessageIn, ReplyOut
from app.api.threads import NOT_FOUND
from app.observability import turn_fields
from app.store.threads import ThreadNotFound

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/threads", tags=["messages"])


@router.post("/{thread_id}/messages", response_model=ReplyOut)
async def post_message(thread_id: str, body: MessageIn,
                       services: ServicesDep) -> ReplyOut:
    limit = services.settings.api.max_input_chars
    if len(body.content) > limit:
        raise HTTPException(422,
                            f"Message is too long: at most {limit} characters.")
    store = services.store
    started = time.monotonic()
    async with services.locks.hold(thread_id):
        try:
            ctx = await asyncio.to_thread(store.context, thread_id)
        except ThreadNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None

        # Read once: a corpus reload swaps services.pipeline, and this message must be answered
        # from one corpus from start to finish.
        pipeline = services.pipeline
        result = await pipeline.answer(ctx, body.content)

        try:
            user, bot = await asyncio.to_thread(
                store.append_turn, thread_id, result.stored_user_text, result.reply,
                result.route, result.next_context(ctx),
            )
        except ThreadNotFound:  # deleted meanwhile (delete waits for this lock, so rare)
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None

    trace = result.trace or {}
    services.stats.record({**trace, "reply_route": result.route})
    # One record per message, whitelisted fields only: never the message or reply text.
    log.info("turn", extra={"thread_id": thread_id, **turn_fields(trace),
                            "reply_route": result.route, "reply_type": result.reply.type,
                            "latency_ms": int((time.monotonic() - started) * 1000)})
    reply = result.reply
    return ReplyOut(message_id=bot.id, user_message_id=user.id, type=reply.type,
                    answer=reply.answer, citation_url=reply.citation_url,
                    last_updated=reply.last_updated, rendered=reply.rendered,
                    comparison=reply.comparison.to_dict() if reply.comparison else None)
