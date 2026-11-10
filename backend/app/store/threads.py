"""Thread and message storage (architecture §7.2), behind a small repository.

The API never touches SQLAlchemy directly, so SQLite can be swapped for Postgres here (§7.3).
Methods are synchronous; the async API calls them in a worker thread.

What a thread remembers between messages is exactly the pipeline's `ThreadContext`:
`active_scheme_id` ("its"), `pending_candidates` (after a clarifying question), `last_intent`
("and for Bandhan Small Cap?") and the recent history. A turn (user message, assistant reply,
new thread state) is written in one transaction.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.pipeline.orchestrator import ThreadContext, intent_from_dict, intent_to_dict
from app.pipeline.templates import Comparison, Reply
from app.store.db import init_db, session_factory
from app.store.models import THREAD_TABLES, Message, Thread

TITLE_CHARS = 60
HISTORY_MESSAGES = 10  # loaded per turn; the generator sends the last llm.history_messages


class ThreadNotFound(Exception):
    pass


def utcnow() -> datetime:
    """Naive UTC (SQLite DateTime keeps no zone); the API adds the zone when serialising."""
    return datetime.now(UTC).replace(tzinfo=None)


def make_title(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1].rstrip() + "…"


@dataclass(frozen=True)
class MessageView:
    id: str
    seq: int
    role: str
    content: str
    type: str | None
    route: str | None
    citation_url: str | None
    last_updated: date | None
    created_at: datetime
    comparison: dict | None = None  # Comparison.to_dict() of a comparison reply

    @property
    def rendered(self) -> str | None:
        """The answer with its Source line and footer, as first shown (assistant only)."""
        if self.role != "assistant" or not self.citation_url:
            return None
        return Reply(self.type or "answer", self.content, self.citation_url,
                     self.last_updated, Comparison.from_dict(self.comparison)).rendered


@dataclass(frozen=True)
class ThreadView:
    id: str
    title: str | None
    active_scheme_id: str | None
    created_at: datetime
    updated_at: datetime
    messages: tuple[MessageView, ...] = ()


def _message_view(m: Message) -> MessageView:
    return MessageView(m.id, m.seq, m.role, m.content, m.response_type, m.route,
                       m.citation_url, m.last_updated, m.created_at, m.comparison)


def _thread_view(t: Thread, messages=()) -> ThreadView:
    return ThreadView(t.id, t.title, t.active_scheme_id, t.created_at, t.updated_at,
                      tuple(_message_view(m) for m in messages))


class ThreadStore:
    def __init__(self, engine: Engine, *, create_tables: bool = True):
        if create_tables:
            init_db(engine, THREAD_TABLES)
        self.sessions: sessionmaker[Session] = session_factory(engine)

    # ------------------------------------------------------------------ threads
    def create(self) -> ThreadView:
        now = utcnow()
        thread = Thread(id=str(uuid.uuid4()), title=None, active_scheme_id=None,
                        pending_candidates=[], last_intent=None, message_count=0,
                        created_at=now, updated_at=now)
        with self.sessions.begin() as s:
            s.add(thread)
        return _thread_view(thread)

    def get(self, thread_id: str) -> ThreadView | None:
        """Thread metadata and its full history, oldest first."""
        with self.sessions() as s:
            thread = s.get(Thread, thread_id)
            if thread is None:
                return None
            messages = s.scalars(select(Message).where(Message.thread_id == thread_id)
                                 .order_by(Message.seq)).all()
            return _thread_view(thread, messages)

    def rename(self, thread_id: str, title: str) -> ThreadView | None:
        """Set a user-chosen title (already PII-checked by the API). Activity time is kept, so
        renaming doesn't reorder threads or postpone retention. The first message's automatic
        title only fills an empty one, so a rename made before it sticks."""
        with self.sessions.begin() as s:
            thread = s.get(Thread, thread_id)
            if thread is None:
                return None
            thread.title = make_title(title)
            return _thread_view(thread)

    def exists(self, thread_id: str) -> bool:
        with self.sessions() as s:
            return s.get(Thread, thread_id) is not None

    def delete(self, thread_id: str) -> bool:
        with self.sessions.begin() as s:
            s.execute(delete(Message).where(Message.thread_id == thread_id))
            return s.execute(delete(Thread).where(Thread.id == thread_id)).rowcount > 0

    def delete_idle(self, before: datetime) -> int:
        """Retention (§7.5): delete threads not updated since `before`; returns the count."""
        with self.sessions.begin() as s:
            ids = s.scalars(select(Thread.id).where(Thread.updated_at < before)).all()
            if ids:
                s.execute(delete(Message).where(Message.thread_id.in_(ids)))
                s.execute(delete(Thread).where(Thread.id.in_(ids)))
            return len(ids)

    def count(self) -> int:
        with self.sessions() as s:
            return s.scalar(select(func.count()).select_from(Thread)) or 0

    # ------------------------------------------------------------------ conversation state
    def context(self, thread_id: str, history: int = HISTORY_MESSAGES) -> ThreadContext:
        """The pipeline's view of the thread. Raises ThreadNotFound."""
        with self.sessions() as s:
            thread = s.get(Thread, thread_id)
            if thread is None:
                raise ThreadNotFound(thread_id)
            recent = s.scalars(select(Message).where(Message.thread_id == thread_id)
                               .order_by(Message.seq.desc()).limit(history)).all()
            return ThreadContext(
                active_scheme_id=thread.active_scheme_id,
                pending_candidates=tuple(thread.pending_candidates or ()),
                history=[{"role": m.role, "content": m.content} for m in reversed(recent)],
                last_intent=intent_from_dict(thread.last_intent),
            )

    def append_turn(self, thread_id: str, stored_user_text: str, reply: Reply, route: str,
                    new_ctx: ThreadContext) -> tuple[MessageView, MessageView]:
        """Store the user message (already PII-safe), the reply and the new thread state."""
        now = utcnow()
        with self.sessions.begin() as s:
            thread = s.get(Thread, thread_id, with_for_update=True)
            if thread is None:  # deleted while the reply was being produced
                raise ThreadNotFound(thread_id)
            seq = thread.message_count
            user = Message(id=str(uuid.uuid4()), thread_id=thread_id, seq=seq + 1, role="user",
                           content=stored_user_text, created_at=now)
            bot = Message(id=str(uuid.uuid4()), thread_id=thread_id, seq=seq + 2,
                          role="assistant", content=reply.answer, response_type=reply.type,
                          route=route, citation_url=reply.citation_url,
                          last_updated=reply.last_updated,
                          comparison=reply.comparison.to_dict() if reply.comparison else None,
                          created_at=now)
            s.add_all([user, bot])
            thread.message_count = seq + 2
            if thread.title is None:
                thread.title = make_title(stored_user_text)
            thread.active_scheme_id = new_ctx.active_scheme_id
            thread.pending_candidates = list(new_ctx.pending_candidates)
            thread.last_intent = intent_to_dict(new_ctx.last_intent)
            thread.updated_at = now
            return _message_view(user), _message_view(bot)
