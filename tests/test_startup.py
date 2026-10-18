import asyncio

import pytest
from telegram.error import BadRequest, NetworkError, TimedOut

from telebot_photos.__main__ import initialize_with_retry


class FakeApplication:
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.calls = 0

    async def initialize(self) -> None:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)


async def test_retries_until_the_bot_api_answers():
    app = FakeApplication([NetworkError("connection refused"), TimedOut()])
    assert await initialize_with_retry(app, asyncio.Event(), base_delay=0.01)
    assert app.calls == 3


async def test_gives_up_after_the_last_attempt():
    app = FakeApplication([NetworkError("down")] * 3)
    with pytest.raises(NetworkError):
        await initialize_with_retry(app, asyncio.Event(), attempts=3, base_delay=0.01)
    assert app.calls == 3


async def test_bad_request_is_not_retried():
    app = FakeApplication([BadRequest("nope")])
    with pytest.raises(BadRequest):
        await initialize_with_retry(app, asyncio.Event(), base_delay=0.01)
    assert app.calls == 1


async def test_stop_while_waiting_returns_false():
    stop = asyncio.Event()
    stop.set()
    app = FakeApplication([NetworkError("down")])
    assert not await initialize_with_retry(app, stop, base_delay=10)
    assert app.calls == 1
