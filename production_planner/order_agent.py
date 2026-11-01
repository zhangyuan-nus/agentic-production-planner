"""
Order Agent.

Takes a free-text production request from an operator (e.g. "make 500 p12 by
tomorrow 8am using existing stock") and uses Gemini structured output to
translate it into a strict, validated ``ProductionGoal`` that the optimiser
can consume. The LLM only captures intent: it never sees stock levels or does
any arithmetic.

Standalone usage (for testing):

    python -m production_planner.order_agent "produce 300 p123 by 2026-05-01, reuse stock"
    python -m production_planner.order_agent --interactive
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, field_validator

from .config import MODEL, PART_TO_MACHINE
from .models import ProductionGoal

_VALID_PARTS = sorted(PART_TO_MACHINE.keys())


class OrderIntent(BaseModel):
    """LLM-facing schema. Deliberately minimal so Gemini's structured-output
    mode has fewer ways to drift."""

    target_part: str = Field(
        description="Canonical part id. Must be one of: " + ", ".join(_VALID_PARTS),
    )
    quantity: int = Field(
        description="Number of units of target_part to produce. Must be >= 1.",
    )
    deadline: str | None = Field(
        default=None,
        description=(
            "Optional ISO-8601 deadline including the UTC offset "
            "(e.g. '2026-05-01T08:00:00+08:00'). Resolve relative phrases like "
            "'tomorrow morning' against the 'current_local_time' provided. "
            "Leave null if no deadline was given."
        ),
    )
    use_existing_stock: bool = Field(
        default=False,
        description=(
            "True if the user wants existing container/lorry stock to be "
            "counted against the order. False otherwise."
        ),
    )
    confidence: float = Field(
        default=1.0,
        description=(
            "How confident the model is that the parsed fields match the "
            "operator's intent (0.0 = guessing, 1.0 = certain)."
        ),
    )
    notes: str | None = Field(
        default=None,
        description=(
            "Short free-text clarification or warning (e.g. 'assumed p12 "
            "from \"p 12\"'). Leave null if no notes."
        ),
    )

    @field_validator("target_part")
    @classmethod
    def _normalise_part(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("target_part must be a string")
        v = v.strip().lower().replace(" ", "")
        if v not in PART_TO_MACHINE:
            raise ValueError(
                f"target_part must be one of {_VALID_PARTS}, got {v!r}"
            )
        return v

    @field_validator("quantity")
    @classmethod
    def _quantity_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("quantity must be >= 1")
        return v

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, v: float) -> float:
        return min(max(v, 0.0), 1.0)


_SYSTEM_PROMPT = """\
You translate natural-language factory orders into a strict JSON object.

Valid parts in this factory network:
  {parts}

Rules:
- `target_part` must be one of the valid parts above, lowercase, no spaces
  ('p12' not 'P 12' or 'part12').
- `quantity` must be a positive integer (the number of units of `target_part`).
- Times are in the operator's time zone. `current_local_time` is "now" as an
  ISO-8601 timestamp with UTC offset. Convert relative deadlines like
  "by tomorrow 8am", "in 2 hours" or "next Monday" to an absolute ISO-8601
  timestamp that includes the same UTC offset. If the user gives no deadline,
  set `deadline` to null. Do NOT invent deadlines.
- Set `use_existing_stock` to true ONLY if the operator explicitly asks to
  use/reuse/count existing stock, inventory, or parts already on hand.
  Otherwise false.
- `confidence` is your self-rating of this parse. If the request is
  ambiguous (e.g. unknown part, multiple parts, missing quantity), set
  confidence low and explain in `notes`.
- `notes` is optional: one short sentence only if you had to interpret
  something non-obvious.
- If the order is impossible to parse at all (no quantity, no recognisable
  part), return your best guess with confidence <= 0.3 and explain in
  `notes`. Do not refuse.

Output: exactly one JSON object matching the schema. No prose, no markdown.
"""


def _build_request(order_text: str, now: datetime) -> str:
    return json.dumps(
        {
            "current_local_time": now.isoformat(timespec="seconds"),
            "operator_request": order_text,
        },
        indent=2,
    )


def parse_order(
    order_text: str,
    client: genai.Client | None = None,
    now: datetime | None = None,
) -> OrderIntent:
    """Call Gemini and return a validated ``OrderIntent``.

    Raises ``RuntimeError`` if the LLM call fails or returns something
    that does not match the schema.
    """
    if not order_text or not order_text.strip():
        raise ValueError("order_text is empty")

    now = now or datetime.now().astimezone()
    try:
        client = client or genai.Client()
        response = client.models.generate_content(
            model=MODEL,
            contents=_build_request(order_text, now),
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT.format(parts=", ".join(_VALID_PARTS)),
                response_mime_type="application/json",
                response_schema=OrderIntent,
            ),
        )
    except Exception as exc:
        raise RuntimeError(f"order agent LLM call failed: {exc}") from exc

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, OrderIntent):
        return parsed

    raw = (getattr(response, "text", None) or "").strip()
    if not raw:
        raise RuntimeError("order agent: empty response from LLM")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"order agent: LLM returned non-JSON output: {raw!r}"
        ) from exc
    return OrderIntent.model_validate(data)


def parse_deadline(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware datetime.

    A trailing 'Z' is accepted (``datetime.fromisoformat`` only supports it from
    Python 3.11) and timestamps without an offset are taken as local time.
    """
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def intent_to_goal(intent: OrderIntent) -> ProductionGoal:
    """Convert an ``OrderIntent`` to the optimiser's ``ProductionGoal``."""
    deadline_dt = None
    if intent.deadline:
        try:
            deadline_dt = parse_deadline(intent.deadline)
        except ValueError as exc:
            raise ValueError(
                f"order agent: bad ISO-8601 deadline from LLM: {intent.deadline!r}"
            ) from exc

    return ProductionGoal(
        target_part=intent.target_part,
        quantity=intent.quantity,
        deadline=deadline_dt,
        use_existing_stock=intent.use_existing_stock,
    )


def print_intent(intent: OrderIntent) -> None:
    print("===== PARSED ORDER =====")
    print(f"  target_part         : {intent.target_part}")
    print(f"  quantity            : {intent.quantity}")
    print(f"  deadline            : {intent.deadline or '(none)'}")
    print(f"  use_existing_stock  : {intent.use_existing_stock}")
    print(f"  confidence          : {intent.confidence:.2f}")
    if intent.notes:
        print(f"  notes               : {intent.notes}")


def _run_once(order_text: str, as_json: bool) -> None:
    intent = parse_order(order_text)
    if as_json:
        print(intent.model_dump_json(indent=2))
    else:
        print_intent(intent)


def main() -> None:
    parser = argparse.ArgumentParser(description="Natural-language order agent")
    parser.add_argument(
        "order",
        nargs="?",
        help='Free-text order, e.g. "make 500 p12 by tomorrow 8am reuse stock"',
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Read orders from stdin, one per line. Ctrl-C to exit.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the parsed intent as JSON instead of a human-readable table.",
    )
    args = parser.parse_args()

    if args.interactive:
        print("[order-agent] interactive mode. Type an order and press enter. Ctrl-C to quit.")
        try:
            while True:
                try:
                    line = input("order> ").strip()
                except EOFError:
                    break
                if not line:
                    continue
                try:
                    _run_once(line, args.json)
                except Exception as exc:
                    print(f"[order-agent] ERROR: {exc}")
        except KeyboardInterrupt:
            print("\n[order-agent] bye.")
        return

    if not args.order:
        parser.error("provide an order string or use --interactive")

    try:
        _run_once(args.order, args.json)
    except Exception as exc:
        print(f"[order-agent] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
