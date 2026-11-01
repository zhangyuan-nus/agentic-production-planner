import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from production_planner.order_agent import (
    OrderIntent,
    intent_to_goal,
    parse_deadline,
    parse_order,
)

NOW = datetime(2026, 4, 17, 9, 30, tzinfo=timezone(timedelta(hours=8)))


class FakeClient:
    """Stands in for ``genai.Client``: records each request and returns a canned response."""

    def __init__(self, response=None, error: Exception | None = None):
        self.models = self
        self.requests: list[dict] = []
        self._response = response
        self._error = error

    def generate_content(self, **kwargs):
        self.requests.append(kwargs)
        if self._error:
            raise self._error
        return self._response


def test_intent_normalises_the_part_name():
    assert OrderIntent(target_part=" P 12 ", quantity=5).target_part == "p12"


@pytest.mark.parametrize(
    "fields",
    [{"target_part": "p9", "quantity": 5}, {"target_part": "p1", "quantity": 0}],
)
def test_intent_rejects_invalid_orders(fields):
    with pytest.raises(ValidationError):
        OrderIntent(**fields)


@pytest.mark.parametrize(("raw", "clamped"), [(1.7, 1.0), (-0.2, 0.0), (0.4, 0.4)])
def test_confidence_is_clamped(raw, clamped):
    assert OrderIntent(target_part="p1", quantity=1, confidence=raw).confidence == clamped


def test_parse_deadline_accepts_a_z_suffix():
    assert parse_deadline("2026-04-17T18:00:00Z") == datetime(2026, 4, 17, 18, tzinfo=timezone.utc)


def test_parse_deadline_keeps_an_explicit_offset():
    assert parse_deadline("2026-04-17T18:00+08:00").utcoffset() == timedelta(hours=8)


def test_parse_deadline_reads_naive_times_as_local():
    parsed = parse_deadline("2026-04-17T18:00")
    assert parsed.tzinfo is not None
    assert parsed.replace(tzinfo=None) == datetime(2026, 4, 17, 18, 0)


def test_parse_deadline_rejects_free_text():
    with pytest.raises(ValueError):
        parse_deadline("tomorrow")


def test_intent_to_goal_carries_every_field():
    intent = OrderIntent(
        target_part="p123",
        quantity=500,
        deadline="2026-04-17T18:00:00+08:00",
        use_existing_stock=True,
    )
    goal = intent_to_goal(intent)
    assert (goal.target_part, goal.quantity, goal.use_existing_stock) == ("p123", 500, True)
    assert goal.deadline == datetime(2026, 4, 17, 10, tzinfo=timezone.utc)


def test_intent_to_goal_reports_a_bad_deadline():
    with pytest.raises(ValueError, match="bad ISO-8601 deadline"):
        intent_to_goal(OrderIntent(target_part="p1", quantity=1, deadline="end of shift"))


def test_parse_order_sends_local_time_and_requests_structured_output():
    expected = OrderIntent(target_part="p12", quantity=200)
    client = FakeClient(SimpleNamespace(parsed=expected, text=None))

    assert parse_order("make 200 p12", client=client, now=NOW) is expected

    request = client.requests[0]
    assert json.loads(request["contents"]) == {
        "current_local_time": "2026-04-17T09:30:00+08:00",
        "operator_request": "make 200 p12",
    }
    assert request["config"].response_schema is OrderIntent


def test_parse_order_falls_back_to_the_response_text():
    text = '{"target_part": "P4", "quantity": 30, "use_existing_stock": true}'
    client = FakeClient(SimpleNamespace(parsed=None, text=text))
    intent = parse_order("30 of p4 from stock", client=client, now=NOW)
    assert (intent.target_part, intent.quantity, intent.use_existing_stock) == ("p4", 30, True)


@pytest.mark.parametrize(
    "client",
    [
        FakeClient(SimpleNamespace(parsed=None, text="Sure! Here is your order.")),
        FakeClient(error=ConnectionError("network unreachable")),
    ],
)
def test_parse_order_wraps_llm_failures(client):
    with pytest.raises(RuntimeError):
        parse_order("make 5 p1", client=client, now=NOW)


def test_parse_order_rejects_an_empty_order():
    with pytest.raises(ValueError):
        parse_order("   ", client=FakeClient())
