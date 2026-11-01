# Factory Agent (Google ADK)

A factory production optimiser. It connects to a live MQTT-based factory
simulator and uses Google Gemini (via Vertex AI) to:

1. Parse natural-language production orders into a structured goal, and
2. Summarise the optimiser's plan into a manager-friendly report.

The optimiser inspects current machine states across four factories,
decides which idle machines to enable to meet a target part/quantity by
a deadline, publishes the plan over MQTT, and then watches stock until
the order is fulfilled before sending a stop instruction.

---

## 1. Prerequisites

You will need:

- **Python 3.10+**
- **An MQTT broker** running on `localhost:1883`. The host can be
  changed in `production_planner/config.py` (`MQTT_BROKER`). Quick install:
  - **Windows:** download the installer from
    <https://mosquitto.org/download/>, install it, then start the
    service from `services.msc` (or `net start mosquitto` from an
    admin prompt).
  - **macOS:** `brew install mosquitto && brew services start mosquitto`
  - **Linux (Debian/Ubuntu):** `sudo apt install mosquitto && sudo systemctl start mosquitto`

  Verify it's listening with `netstat -an | findstr 1883` (Windows) or
  `lsof -i :1883` (macOS / Linux).

- **The factory simulator** (a separate project, not included here)
  publishing on the topics `Factory/<id>/Information`. Without it the
  optimiser will still run but will warn that no machines are active.
  The optimiser expects a simulator that:
  - Publishes one JSON message per factory on `Factory/<id>/Information`
    every few seconds, containing `machines_information` (machine state
    per machine), `container_information` (per-part stock levels), and
    optionally `lorry_cargo`.
  - Subscribes to `optimizer/instructions` and reacts to
    `optimization_plan` (enable machines) and `optimization_plan_stop`
    (disable machines) payloads.

  See `production_planner/snapshot.py` and `production_planner/optimization_runner.py`
  for the exact field shapes.

- **A Google Cloud project with Vertex AI enabled**, plus a
  service-account key JSON file with the `Vertex AI User` role.

- *(Optional)* A Telegram bot token and chat ID, only if you want
  Telegram alerts.

---

## 2. Install

From the repository root:

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

**macOS / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

---

## 3. Configure

1. Copy `.env.example` to `.env` in the repository root.
2. Place your GCP service-account key somewhere on disk (e.g.
   `gcp-key.json` in the repository root; it is git-ignored).
3. Edit `.env` and fill in:
   - `GOOGLE_CLOUD_PROJECT` — your GCP project ID.
   - `GOOGLE_APPLICATION_CREDENTIALS` — absolute path to your key file.
     On Windows use forward slashes or double backslashes, e.g.

     ```text
     C:/Users/you/projects/agentic-production-planner/gcp-key.json
     ```

   - `TELEGRAM_*` values, only if you're using the Telegram features.

---

## 4. Verify your setup (no MQTT needed)

The order-agent step only requires working Vertex AI credentials, so
use it to confirm setup:

```bash
python -m production_planner.order_agent "produce 300 p123"
```

If you see a parsed-order table printed, your Python deps and GCP auth
are good. If you see an authentication error, double-check
`GOOGLE_APPLICATION_CREDENTIALS` and that Vertex AI is enabled on the
project.

---

## 5. Run the full optimiser

Make sure your MQTT broker and the factory simulator are both running,
then from the project root:

**Natural-language order (uses Gemini to parse the request):**

```bash
python -m production_planner.optimization_runner --order "make 500 p12 by tomorrow 8am, use existing stock"
```

**Structured form (no LLM parsing of the request):**

```bash
python -m production_planner.optimization_runner --part p123 --qty 500
python -m production_planner.optimization_runner --part p12 --qty 200 --deadline "2027-01-15T18:00:00"
python -m production_planner.optimization_runner --part p234 --qty 500 --use-existing-stock
```

**Useful flags:**

- `--no-llm` — skip the Gemini plan-summary step.
- `--auto-confirm` — don't prompt for Y/N before publishing instructions
  to MQTT.
- `--no-monitor` — don't watch for order completion / auto-stop machines.
- `--mqtt-timeout 10` — how long to wait for factory snapshots (seconds).
- `--monitor-interval 2` and `--monitor-timeout 0` — completion-watch
  tuning.

---

## 6. What it does end-to-end

1. *(Optional)* Sends your free-text order to Gemini → structured
   `ProductionGoal`.
2. Subscribes to `Factory/+/Information` over MQTT to snapshot current
   machine states and stock levels.
3. Runs `optimizer.compute_plan(...)` to decide which machines to
   enable.
4. Prints the raw plan plus a Gemini-written manager-friendly summary.
5. After you confirm, publishes the action plan to the MQTT topic
   `optimizer/instructions`.
6. Watches stock until the order quantity has been produced, then
   publishes a stop payload that disables the machines it enabled.

---

## 7. Project layout

```text
agentic-production-planner/
  README.md
  pyproject.toml
  .env.example              ← copy to .env and fill in
  production_planner/
    config.py               ← MQTT broker, model name, parts/machines map
    order_agent.py          ← natural-language → ProductionGoal (Gemini)
    optimizer.py            ← core planning algorithm
    optimization_runner.py  ← CLI entry point (the one you'll run)
    snapshot.py             ← MQTT snapshot collector
    models.py               ← Pydantic schemas
```

---

## 8. Common issues

- **`ModuleNotFoundError: No module named 'production_planner'`** — you ran
  the script from inside `production_planner/` instead of the repository root.
  Run from the repository root and use
  `python -m production_planner.optimization_runner ...`.
- **`google.auth.exceptions.DefaultCredentialsError`** —
  `GOOGLE_APPLICATION_CREDENTIALS` is wrong, or Vertex AI isn't enabled
  on the project.
- **`No snapshots received`** — your MQTT broker isn't running, or the
  simulator isn't publishing on `Factory/+/Information`.
- **Connection refused on MQTT** — change `MQTT_BROKER` in
  `production_planner/config.py` if your broker is on another host.
