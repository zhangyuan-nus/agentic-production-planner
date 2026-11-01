"""
Order Agent.

Takes a free-text production request from an operator (e.g. "make 500 p12 by
tomorrow 8am using existing stock") and uses Gemini (via the same credentials
as the rest of workflow 3) to translate it into a strict, validated
``ProductionGoal`` that the optimiser can consume.

Standalone usage (for testing):

    python -m production_planner.order_agent "produce 300 p123 by 2026-05-01, reuse stock"
    python -m production_planner.order_agent --interactive
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Optional

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
    deadline: Optional[str] = Field(
        default=None,
        description=(
            "Optional ISO-8601 deadline (e.g. '2026-05-01T08:00:00+00:00'). "
            "Convert relative phrases like 'tomorrow morning' to an absolute "
            "UTC ISO-8601 timestamp using the 'current_time_utc' provided. "
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
    notes: Optional[str] = Field(
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
        if v < 0.0:
            return 0.0
        if v > 1.0:
            return 1.0
        return v


_SYSTEM_PROMPT = """\
You translate natural-language factory orders into a strict JSON object.

Valid parts in this factory network:
  {parts}

Rules:
- `target_part` must be one of the valid parts above, lowercase, no spaces
  ('p12' not 'P 12' or 'part12').
- `quantity` must be a positive integer (the number of units of `target_part`).
- If the user gives a relative deadline like "by tomorrow 8am", "in 2 hours",
  "next Monday", convert it to an absolute ISO-8601 timestamp in UTC using
  `current_time_utc` as "now". If the user gives no deadline, set `deadline`
  to null. Do NOT invent deadlines.
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


def _build_prompt(order_text: str) -> list[str]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    system = _SYSTEM_PROMPT.format(parts=", ".join(_VALID_PARTS))
    user = json.dumps(
        {
            "current_time_utc": now,
            "operator_request": order_text,
        },
        indent=2,
    )
    return [system, user]


def parse_order(order_text: str) -> OrderIntent:
    """Call Gemini and return a validated ``OrderIntent``.

    Raises ``RuntimeError`` if the LLM call fails or returns something
    that does not match the schema.
    """
    if not order_text or not order_text.strip():
        raise ValueError("order_text is empty")

    client = genai.Client()
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=_build_prompt(order_text),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=OrderIntent,
                temperature=0.0,
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


def intent_to_goal(intent: OrderIntent) -> ProductionGoal:
    """Convert an ``OrderIntent`` to the optimiser's ``ProductionGoal``.

    Parses the string ISO-8601 deadline into a timezone-aware ``datetime``.
    """
    deadline_dt = None
    if intent.deadline:
        try:
            deadline_dt = datetime.fromisoformat(intent.deadline)
            if deadline_dt.tzinfo is None:
                deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)
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
