"""Shared Groq client: request shape, retries, error handling and the rate limiter."""

import pytest

from app.pipeline.llm import LLMUnavailable, RateLimited, RateLimiter, estimate_tokens
from tests.fake_groq import FakeGroq, client, reply, status_error

MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
SCHEMA = {"title": "answer", "type": "object", "properties": {}, "required": [],
          "additionalProperties": False}


async def test_request_shape_for_gpt_oss():
    fake = FakeGroq(reply('{"x": 1}'))
    result = await client(fake).chat(MESSAGES, purpose="answer", json_schema=SCHEMA)
    req = fake.requests[0]
    assert req["model"] == "openai/gpt-oss-120b"
    assert req["temperature"] == 0
    assert req["max_completion_tokens"] == 1024
    assert req["reasoning_effort"] == "low" and req["include_reasoning"] is False
    assert req["response_format"] == {
        "type": "json_schema", "json_schema": {"name": "answer", "strict": True,
                                               "schema": SCHEMA}}
    assert result.content == '{"x": 1}' and result.usage.total_tokens == 140
    assert result.usage.reasoning_tokens == 10


async def test_no_reasoning_params_for_other_models():
    fake = FakeGroq(reply("{}"))
    await client(fake, model="llama-3.3-70b-versatile", reasoning_effort=None).chat(
        MESSAGES, purpose="answer")
    assert "reasoning_effort" not in fake.requests[0]
    assert "include_reasoning" not in fake.requests[0]


@pytest.mark.parametrize("error", [status_error(429, retry_after=0), status_error(500),
                                   status_error(503), TimeoutError()])
async def test_retries_once_on_transient_errors(error):
    fake = FakeGroq(error, reply("{}"))
    result = await client(fake).chat(MESSAGES, purpose="answer")
    assert len(fake.requests) == 2 and result.content == "{}"


async def test_gives_up_after_the_retry():
    fake = FakeGroq(status_error(500), status_error(500))
    with pytest.raises(LLMUnavailable) as err:
        await client(fake).chat(MESSAGES, purpose="answer")
    assert err.value.reason == "server" and len(fake.requests) == 2


async def test_client_errors_are_not_retried():
    fake = FakeGroq(status_error(400))
    with pytest.raises(LLMUnavailable) as err:
        await client(fake).chat(MESSAGES, purpose="answer")
    assert err.value.reason == "http_400" and len(fake.requests) == 1


async def test_long_retry_after_is_not_waited_for():
    fake = FakeGroq(status_error(429, retry_after=120))
    with pytest.raises(LLMUnavailable) as err:
        await client(fake).chat(MESSAGES, purpose="answer")
    assert err.value.reason == "rate_limit" and len(fake.requests) == 1


async def test_local_budget_exhausted_means_unavailable():
    fake = FakeGroq(reply("{}"), reply("{}"))
    llm = client(fake, requests_per_minute=1, max_wait_seconds=0)
    await llm.chat(MESSAGES, purpose="answer")
    with pytest.raises(LLMUnavailable) as err:
        await llm.chat(MESSAGES, purpose="answer")
    assert err.value.reason == "rate_budget" and len(fake.requests) == 1


def test_estimate_tokens():
    assert estimate_tokens([{"role": "user", "content": "x" * 400}]) == 108


# ------------------------------------------------------------------------------ limiter
class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def limiter(clock, rpm=30, tpm=8000, rpd=1000, tpd=200000):
    return RateLimiter(rpm, tpm, rpd, tpd, clock=clock, sleep=clock.sleep)


async def test_requests_per_minute():
    clock = Clock()
    lim = limiter(clock, rpm=2)
    await lim.acquire(10, max_wait=100)
    await lim.acquire(10, max_wait=100)
    await lim.acquire(10, max_wait=100)  # waits until the first call leaves the window
    assert clock.slept == [60.0]


async def test_tokens_per_minute_waits_for_enough_budget():
    clock = Clock()
    lim = limiter(clock, tpm=8000)
    await lim.acquire(5000, max_wait=100)
    clock.now += 10
    await lim.acquire(2000, max_wait=100)
    clock.now += 10
    await lim.acquire(3000, max_wait=100)  # 7000 used: wait until the 5000 call expires
    assert clock.slept == [40.0]


async def test_settle_replaces_the_estimate():
    clock = Clock()
    lim = limiter(clock, tpm=8000)
    event = await lim.acquire(7000, max_wait=0)
    lim.settle(event, 500)
    await lim.acquire(7000, max_wait=0)  # fits now: no wait
    assert clock.slept == []


async def test_waiting_longer_than_allowed_raises():
    clock = Clock()
    lim = limiter(clock, rpm=1)
    await lim.acquire(10, max_wait=20)
    with pytest.raises(RateLimited):
        await lim.acquire(10, max_wait=20)


async def test_daily_budget():
    clock = Clock()
    lim = limiter(clock, rpd=2)
    await lim.acquire(10, max_wait=0)
    clock.now += 61
    await lim.acquire(10, max_wait=0)
    clock.now += 61
    with pytest.raises(RateLimited):
        await lim.acquire(10, max_wait=1000)
    clock.now += 86400
    await lim.acquire(10, max_wait=0)  # a day later the budget is back


async def test_a_429_blocks_new_calls():
    clock = Clock()
    lim = limiter(clock)
    lim.block_for(5)
    await lim.acquire(10, max_wait=10)
    assert clock.slept == [5.0]
