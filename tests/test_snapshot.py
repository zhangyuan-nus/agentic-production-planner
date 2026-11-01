import pytest

from production_planner.snapshot import extract_stocks, factory_id_from_topic, load_snapshots


def test_stock_includes_containers_and_lorry_cargo(example_snapshots):
    stocks = extract_stocks(example_snapshots)
    assert stocks["p1"] == 120  # 80 in Container 1 plus 40 loading onto Lorry 1
    assert stocks["p2"] == 80
    assert stocks["p3"] == 35
    assert stocks["p123"] == 0


def test_stock_accepts_alternative_payload_layouts():
    snapshots = {
        "1": {"containers": {"F1C_P1": 7}},
        "2": {"container_information": {"Container 3": {"partContained": "P 2", "level": "5"}}},
    }
    assert extract_stocks(snapshots) == {"p1": 7, "p2": 5}


def test_stock_ignores_malformed_snapshots():
    snapshots = {
        "1": {"error": "bad_json", "raw": "{"},
        "2": "not a dict",
        "3": {"lorry_cargo": []},
    }
    assert extract_stocks(snapshots) == {}


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("Factory/3/Information", "3"),
        ("Factory/3/Status", None),
        ("optimizer/instructions", None),
    ],
)
def test_factory_id_from_topic(topic, expected):
    assert factory_id_from_topic(topic) == expected


def test_load_snapshots_keys_by_factory_id(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text('{"1": {"machines_information": {}}}', encoding="utf-8")
    assert load_snapshots(path) == {"1": {"machines_information": {}}}


def test_load_snapshots_rejects_a_list(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_snapshots(path)
