"""Unit tests for the pure scoring/banding functions (no Rasa or network needed)."""
from actions.scoring import carbon_band, rank_options

OPTS = [
    {"mode": "rail", "co2_kg": 14.0, "price_eur": 70, "duration_h": 3.0},
    {"mode": "coach", "co2_kg": 11.0, "price_eur": 35, "duration_h": 7.5},
    {"mode": "flight", "co2_kg": 52.0, "price_eur": 90, "duration_h": 3.4},
]


def test_carbon_band_thresholds():
    assert carbon_band(10, 400) == "green"     # 0.025 kg/km
    assert carbon_band(40, 400) == "amber"     # 0.10 kg/km
    assert carbon_band(80, 400) == "red"       # 0.20 kg/km


def test_carbon_band_zero_distance_is_safe():
    assert carbon_band(5, 0) == "amber"


def test_high_sustainability_never_ranks_flight_first():
    ranked = rank_options(OPTS, "high", budget=None)["ranked"]
    assert ranked[0]["mode"] != "flight"
    assert ranked[-1]["mode"] == "flight"


def test_low_sustainability_favours_cheapest():
    ranked = rank_options(OPTS, "low", budget=None)["ranked"]
    assert ranked[0]["mode"] == "coach"


def test_budget_moves_expensive_options_to_over_budget():
    res = rank_options(OPTS, "medium", budget=60)
    assert [o["mode"] for o in res["ranked"]] == ["coach"]
    assert {o["mode"] for o in res["over_budget"]} == {"rail", "flight"}


def test_empty_input():
    assert rank_options([], "high", 100) == {"ranked": [], "over_budget": []}
