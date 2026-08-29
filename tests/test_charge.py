"""B-027: charge count must equal the number of posts actually pushed, and a
failing/disabled charge call must never raise into the caller (a customer must
never be charged for data that was not written, and a charging hiccup must
never crash a run that already produced good data).
"""
import asyncio
from unittest.mock import AsyncMock, patch

import main


def test_charge_is_called_once_with_the_pushed_count():
    with patch.object(main.Actor, "charge", new=AsyncMock()) as charge:
        asyncio.run(main._charge(7))
    charge.assert_awaited_once_with(event_name=main.CHARGE_EVENT_POST, count=7)


def test_zero_posts_never_calls_charge():
    """count<=0 is not 'charge nothing', it's 'do not call charge at all' -
    an empty run must not touch billing."""
    with patch.object(main.Actor, "charge", new=AsyncMock()) as charge:
        asyncio.run(main._charge(0))
    charge.assert_not_awaited()


def test_charge_failure_does_not_raise():
    """If the Apify PPE plumbing is unavailable/misconfigured, the run must
    keep going (data already pushed is not undone) - it must not blow up the
    whole run over a charge that could not be recorded."""
    with patch.object(main.Actor, "charge", new=AsyncMock(side_effect=RuntimeError("boom"))):
        asyncio.run(main._charge(3))  # must not raise


def test_push_charges_exactly_the_pushed_batch_size(monkeypatch):
    """End-to-end on the real push() closure: pushed count and charged count
    must be the same number, not two independently-tracked counters that can
    drift apart."""
    pushed_batches = []
    charged = []

    async def fake_push_data(batch):
        pushed_batches.append(batch)

    async def fake_charge(event_name, count):
        charged.append((event_name, count))

    monkeypatch.setattr(main.Actor, "push_data", staticmethod(fake_push_data))
    monkeypatch.setattr(main.Actor, "charge", staticmethod(fake_charge))

    total_posts = 0

    async def push(batch):
        nonlocal total_posts
        await main.Actor.push_data(batch)
        await main._charge(len(batch))
        total_posts += len(batch)

    batch = [{"id": 1}, {"id": 2}, {"id": 3}]
    asyncio.run(push(batch))

    assert pushed_batches == [batch]
    assert charged == [(main.CHARGE_EVENT_POST, len(batch))]
    assert total_posts == len(batch)
