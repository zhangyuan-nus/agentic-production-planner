"""Ask Gemini to turn the optimiser's JSON plan into a short report for a plant manager.

The LLM only explains the plan; every number in the report comes from the optimiser.
"""

from __future__ import annotations

from google import genai
from google.genai import types

from .config import MODEL
from .models import OptimizationPlan

SUMMARY_PROMPT = """\
You are a factory production planning assistant.

You will receive the JSON output of an algorithmic optimiser that computed a
production plan for a factory network. Your job is to turn it into a clear,
actionable report for a plant manager.

Notes on units and model:
- All times are in SIMULATION SECONDS (1 sim timestep = 1 second).
- Each active machine's rate depends on its current health state:
  state 0 -> 10/s, 1 -> 9, 2 -> 8, 3 -> 5, 4-9 -> 0.
- A machine recommended for enable is assumed to start at state 0 (10/s).
- `estimated_seconds` does NOT include startup ramp-up, lorry travel time
  between factories, or possible throughput de-rating from internal transport
  caps (c_m_move, m_c_move at 1 unit/step); those are surfaced in `warnings`.
- Each stage has `existing_stock` and `required_units`. If `goal.use_existing_stock`
  is true, the optimiser already subtracted `existing_stock` from the order
  quantity at that stage. Mention stock usage in your report when it materially
  reduces the plan.

Structure your response as:
1. **Goal recap** – one sentence restating part, quantity, and deadline.
2. **Existing stock usage** – ALWAYS include this section:
   - If `goal.use_existing_stock` is false: state "Existing stock not used;
     planning from scratch."
   - If true and `stocks_used` is non-empty: list each part and how many units
     are being drawn from existing stock, e.g. "p1: 80 units drawn from
     existing stock".
   - If true but `stocks_used` is empty: state "Stock check requested but no
     existing stock found."
3. **Feasibility verdict** – clearly state whether the goal can be met.
4. **Key numbers** – estimated seconds, max throughput per second, bottleneck stage.
5. **Recommended actions** – numbered list of machines to enable (machine ID,
   factory, part produced, capacity added).
6. **Risks & warnings** – any warnings from the optimiser, plus your own
   observations (e.g. single-point-of-failure stages, tight deadlines,
   startup ramp-up, cross-factory transport).
7. **Next steps** – 2-3 concrete things the operator should do now.

Keep the report concise (≤ 600 words). Use plain language, no raw JSON.
"""


def summarise_plan(plan: OptimizationPlan, client: genai.Client | None = None) -> str:
    """Return a manager-friendly summary, or a short notice if the LLM is unavailable."""
    try:
        client = client or genai.Client()
        response = client.models.generate_content(
            model=MODEL,
            contents=plan.model_dump_json(indent=2),
            config=types.GenerateContentConfig(system_instruction=SUMMARY_PROMPT),
        )
        return (response.text or "").strip()
    except Exception as exc:
        return f"[LLM summary unavailable: {exc}]"
