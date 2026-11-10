"""Thread endpoints: create, read with history, delete (architecture §8)."""

import asyncio

from fastapi import APIRouter, HTTPException, Response, status

from app.api.deps import ServicesDep
from app.api.schemas import MessageOut, ThreadCreated, ThreadOut, ThreadSummary, ThreadUpdate
from app.pipeline.pii_guard import check_pii
from app.store.threads import MessageView

router = APIRouter(prefix="/api/threads", tags=["threads"])

NOT_FOUND = "Thread not found."
PII_TITLE = ("Titles can't contain personal information such as PAN, Aadhaar, account numbers, "
             "OTPs, phone numbers or email addresses.")


def message_out(m: MessageView) -> MessageOut:
    return MessageOut(id=m.id, role=m.role, content=m.content, type=m.type,
                      citation_url=m.citation_url, last_updated=m.last_updated,
                      rendered=m.rendered, comparison=m.comparison,
                      created_at=m.created_at)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ThreadCreated)
async def create_thread(services: ServicesDep) -> ThreadCreated:
    thread = await asyncio.to_thread(services.store.create)
    return ThreadCreated(id=thread.id, created_at=thread.created_at)


@router.get("/{thread_id}", response_model=ThreadOut)
async def get_thread(thread_id: str, services: ServicesDep) -> ThreadOut:
    thread = await asyncio.to_thread(services.store.get, thread_id)
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return ThreadOut(id=thread.id, title=thread.title, created_at=thread.created_at,
                     updated_at=thread.updated_at,
                     messages=[message_out(m) for m in thread.messages])


@router.patch("/{thread_id}", response_model=ThreadSummary)
async def rename_thread(thread_id: str, body: ThreadUpdate,
                        services: ServicesDep) -> ThreadSummary:
    # A title is stored and shown like a message, so it gets the same PII guard; the text is
    # never echoed back or logged when it is refused.
    if check_pii(body.title).blocked:
        raise HTTPException(422, PII_TITLE)
    thread = await asyncio.to_thread(services.store.rename, thread_id, body.title)
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return ThreadSummary(id=thread.id, title=thread.title)


@router.delete("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(thread_id: str, services: ServicesDep) -> Response:
    # Wait for a reply in progress on this thread, so it can't be written after the delete.
    async with services.locks.hold(thread_id):
        deleted = await asyncio.to_thread(services.store.delete, thread_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
