"""A stand-in for AsyncGroq used by the Phase 6 tests: scripted replies, recorded requests."""

import json
from types import SimpleNamespace

import httpx

from app.config import LLMSettings
from app.pipeline.llm import LLMClient, RateLimiter


def reply(content, finish="stop", prompt=100, completion=40):
    if not isinstance(content, str):
        content = json.dumps(content)
    message = SimpleNamespace(content=content)
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                            total_tokens=prompt + completion,
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=10))
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)],
                           usage=usage)


def answer(text, evidence_id="E1", answerable=True, **kw):
    return reply({"answer": text, "evidence_id": evidence_id, "answerable": answerable}, **kw)


def status_error(status, retry_after=None):
    import groq

    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    response = httpx.Response(status, headers=headers,
                              request=httpx.Request("POST", "https://api.groq.com/x"))
    cls = {429: groq.RateLimitError, 500: groq.InternalServerError,
           503: groq.InternalServerError, 400: groq.BadRequestError}[status]
    return cls(f"HTTP {status}", response=response, body=None)


class FakeGroq:
    """`script`: replies (or exceptions) returned in order; `by_purpose` routes by schema name."""

    def __init__(self, *script, by_purpose=None):
        self.script = list(script)
        self.by_purpose = {k: list(v) for k, v in (by_purpose or {}).items()}
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        name = kwargs.get("response_format", {}).get("json_schema", {}).get("name")
        queue = self.by_purpose.get(name) if name in self.by_purpose else self.script
        if not queue:
            raise AssertionError(f"unexpected LLM call ({name})")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def client(fake: FakeGroq, **settings) -> LLMClient:
    s = LLMSettings(**settings)
    limiter = RateLimiter(s.requests_per_minute, s.tokens_per_minute, s.requests_per_day,
                          s.tokens_per_day)
    return LLMClient(s, client=fake, limiter=limiter, retry_delay=0)
