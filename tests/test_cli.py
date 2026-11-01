import json

import pytest

from production_planner import cli
from production_planner.order_agent import OrderIntent


def run(capsys, *argv: str) -> tuple[int, str]:
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


def test_offline_run_prints_the_payload_instead_of_publishing(capsys, example_snapshot_path):
    code, out = run(
        capsys, "--part", "p123", "--qty", "500", "--use-existing-stock",
        "--snapshot-file", str(example_snapshot_path), "--no-llm",
    )
    assert code == 0
    assert "<- bottleneck" in out
    payload = json.loads(out[out.index("\n{\n") + 1:])
    assert [a["machine_id"] for a in payload["actions"]] == ["F3M5", "F3M4b", "F2M3b"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--qty", "5"],
        ["--part", "p1", "--qty", "0"],
        ["--part", "p9", "--qty", "5"],
        ["--part", "p1", "--qty", "5", "--deadline", "soon"],
    ],
)
def test_bad_arguments_are_usage_errors(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


def test_infeasible_plan_exits_with_1(capsys, tmp_path):
    snapshot = tmp_path / "snapshot.json"
    machines = {"Machine 1": {"state": -2}, "Machine 1b": {"state": -2}}
    snapshot.write_text(json.dumps({"1": {"machines_information": machines}}))
    code, out = run(capsys, "--part", "p1", "--qty", "10", "--snapshot-file", str(snapshot),
                    "--no-llm")
    assert code == 1
    assert "not feasible" in out


def test_low_confidence_order_asks_before_planning(capsys, monkeypatch, example_snapshot_path):
    intent = OrderIntent(target_part="p12", quantity=40, confidence=0.3, notes="guessed the part")
    monkeypatch.setattr(cli, "parse_order", lambda text: intent)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    code, out = run(capsys, "--order", "some p12 maybe", "--snapshot-file",
                    str(example_snapshot_path), "--no-llm")
    assert code == 0
    assert "low confidence" in out
    assert "PRODUCTION PLAN" not in out


def test_confident_order_goes_straight_to_planning(capsys, monkeypatch, example_snapshot_path):
    intent = OrderIntent(target_part="p12", quantity=40, confidence=0.95)
    monkeypatch.setattr(cli, "parse_order", lambda text: intent)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected prompt"))
    code, out = run(capsys, "--order", "make 40 p12", "--snapshot-file",
                    str(example_snapshot_path), "--no-llm")
    assert code == 0
    assert "PRODUCTION PLAN" in out


@pytest.mark.parametrize(
    ("estimate", "timeout"),
    [(None, 3600.0), (0.0, 3600.0), (50.0, 300.0), (1000.0, 2000.0), (1e6, 86400.0)],
)
def test_monitor_timeout_scales_with_the_estimate(estimate, timeout):
    assert cli._auto_timeout(estimate) == timeout
