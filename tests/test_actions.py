"""
Unit tests for custom actions with mocked API responses.
Each external call is tested for success, failure and edge cases.
Run:  pytest -q tests/
"""
from unittest.mock import MagicMock, patch

import pytest
import requests
from rasa_sdk import Tracker
from rasa_sdk.executor import CollectingDispatcher

import actions.actions as A


def make_tracker(slots=None, events=None, latest=None, sender="test-user"):
    base = {"origin": None, "destination": None, "travel_date": None, "budget": None,
            "travellers": 1, "sustainability_level": None, "transport_results": None,
            "hotel_results": None, "fallback_count": 0, "requested_slot": None}
    base.update(slots or {})
    return Tracker(sender, base, latest or {"intent": {"name": "inform"}, "entities": [], "text": ""},
                   events or [], False, None, {}, "action_listen")


@pytest.fixture(autouse=True)
def clear_cache():
    A._cache.clear()
    yield
    A._cache.clear()


# ---------------------------------------------------------------- Climatiq
def test_climatiq_success(monkeypatch):
    monkeypatch.setattr(A, "CLIMATIQ_KEY", "test-key")
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"co2e": 24.0}
    resp.raise_for_status.return_value = None
    with patch("actions.actions.requests.post", return_value=resp) as post:
        kg, src = A.climatiq_estimate("rail", 400, 2)
    assert src == "Climatiq" and kg == 12.0            # total split per passenger
    assert post.call_args.kwargs["timeout"] == A.API_TIMEOUT


def test_climatiq_timeout_falls_back(monkeypatch):
    monkeypatch.setattr(A, "CLIMATIQ_KEY", "test-key")
    with patch("actions.actions.requests.post", side_effect=requests.Timeout):
        kg, src = A.climatiq_estimate("flight", 400, 1)
    assert src == "estimate" and kg == pytest.approx(0.151 * 400, 0.01)


def test_climatiq_bad_json_falls_back(monkeypatch):
    monkeypatch.setattr(A, "CLIMATIQ_KEY", "test-key")
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"unexpected": True}
    with patch("actions.actions.requests.post", return_value=resp):
        _, src = A.climatiq_estimate("coach", 300, 1)
    assert src == "estimate"


def test_car_emissions_shared_between_travellers(monkeypatch):
    monkeypatch.setattr(A, "CLIMATIQ_KEY", "")
    one, _ = A.climatiq_estimate("car", 500, 1)
    four, _ = A.climatiq_estimate("car", 500, 4)
    assert four == pytest.approx(one / 4, 0.05)


# ---------------------------------------------------------------- Amadeus
def test_amadeus_no_credentials_returns_none(monkeypatch):
    monkeypatch.setattr(A, "AMADEUS_ID", "")
    assert A.amadeus_cheapest_flight("LON", "PAR", "2026-11-01", 1) is None


def test_amadeus_flight_success(monkeypatch):
    monkeypatch.setattr(A, "AMADEUS_ID", "id")
    monkeypatch.setattr(A, "AMADEUS_SECRET", "secret")
    tok = MagicMock(); tok.raise_for_status.return_value = None
    tok.json.return_value = {"access_token": "abc", "expires_in": 1799}
    offers = MagicMock(); offers.raise_for_status.return_value = None
    offers.json.return_value = {"data": [{"price": {"grandTotal": "180.00"}}, {"price": {"grandTotal": "120.00"}}]}
    with patch("actions.actions.requests.post", return_value=tok), \
         patch("actions.actions.requests.get", return_value=offers):
        assert A.amadeus_cheapest_flight("LON", "PAR", "2026-11-01", 2) == 60.0


def test_amadeus_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(A, "AMADEUS_ID", "id")
    monkeypatch.setattr(A, "AMADEUS_SECRET", "secret")
    with patch("actions.actions.requests.post", side_effect=requests.ConnectionError):
        assert A.amadeus_cheapest_flight("LON", "PAR", "2026-11-01", 1) is None


# ---------------------------------------------------------------- Actions
def test_search_calculate_rank_pipeline(monkeypatch):
    monkeypatch.setattr(A, "CLIMATIQ_KEY", "")
    monkeypatch.setattr(A, "AMADEUS_ID", "")
    d = CollectingDispatcher()
    slots = {"origin": "London", "destination": "Paris", "travel_date": "next Friday",
             "budget": 400, "sustainability_level": "high"}
    ev = A.ActionSearchTravel().run(d, make_tracker(slots), {})
    slots["transport_results"] = ev[0]["value"]
    assert {o["mode"] for o in slots["transport_results"]} == {"rail", "coach", "flight", "car"}
    ev = A.ActionCalculateCarbon().run(d, make_tracker(slots), {})
    slots["transport_results"] = ev[0]["value"]
    assert all("band" in o for o in slots["transport_results"])
    A.ActionRankOptions().run(d, make_tracker(slots), {})
    cards = [m for m in d.messages if m.get("custom", {}).get("type") == "results_cards"]
    assert cards and cards[0]["custom"]["items"][0]["mode"] in ("rail", "coach")
    assert any(m.get("custom", {}).get("type") == "alert" for m in d.messages)  # flight flagged


def test_search_unknown_city_handled(monkeypatch):
    monkeypatch.setattr(A, "OPENCAGE_KEY", "")
    d = CollectingDispatcher()
    ev = A.ActionSearchTravel().run(d, make_tracker({"origin": "Atlantis", "destination": "Paris"}), {})
    assert ev[0]["value"] is None and "couldn't" in d.messages[0]["text"]


def test_rank_with_no_options_offers_handover():
    d = CollectingDispatcher()
    A.ActionRankOptions().run(d, make_tracker({"transport_results": []}), {})
    assert d.messages[0]["buttons"][0]["payload"] == "/request_human"


def test_handover_writes_context(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "HANDOVER_DIR", tmp_path)
    monkeypatch.setattr(A, "ADVISOR_WEBHOOK", "")
    events = [{"event": "user", "text": "plan a trip", "parse_data": {"intent": {"name": "plan_trip"}}},
              {"event": "bot", "text": "Where from?"}]
    d = CollectingDispatcher()
    ev = A.ActionHandover().run(d, make_tracker({"destination": "Paris"}, events), {})
    files = list(tmp_path.glob("ECO-*.json"))
    assert len(files) == 1 and '"destination": "Paris"' in files[0].read_text()
    assert {"event": "slot", "timestamp": None, "name": "handover_active", "value": True} in ev


def test_handover_webhook_failure_uses_file(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "HANDOVER_DIR", tmp_path)
    monkeypatch.setattr(A, "ADVISOR_WEBHOOK", "https://advisors.example.com/hook")
    with patch("actions.actions.requests.post", side_effect=requests.Timeout):
        A.ActionHandover().run(CollectingDispatcher(), make_tracker(), {})
    assert len(list(tmp_path.glob("ECO-*.json"))) == 1


def test_two_stage_fallback_then_handover():
    def fb_event():
        return {"event": "user", "text": "asdf", "parse_data": {"intent": {"name": "nlu_fallback"}}}
    latest = {"intent": {"name": "nlu_fallback"}, "text": "asdf",
              "intent_ranking": [{"name": "nlu_fallback"}, {"name": "plan_trip"}, {"name": "ask_carbon"}]}
    act = A.ActionDefaultFallback()
    d1 = CollectingDispatcher(); act.run(d1, make_tracker(events=[fb_event()], latest=latest), {})
    assert [b["payload"] for b in d1.messages[0]["buttons"]][:2] == ["/plan_trip", "/ask_carbon"]
    d2 = CollectingDispatcher(); act.run(d2, make_tracker(events=[fb_event()] * 2, latest=latest), {})
    assert len(d2.messages[0]["buttons"]) == 4
    d3 = CollectingDispatcher(); ev = act.run(d3, make_tracker(events=[fb_event()] * 3, latest=latest), {})
    assert ev[-1]["name"] == "utter_ask_handover_consent"


def test_detect_location_uses_nearest_city_offline(monkeypatch):
    monkeypatch.setattr(A, "OPENCAGE_KEY", "")
    latest = {"intent": {"name": "share_location"}, "metadata": {"location": {"lat": 52.37, "lon": 4.89}}}
    ev = A.ActionDetectLocation().run(CollectingDispatcher(), make_tracker(latest=latest), {})
    assert ev[0]["value"] == "Amsterdam"


def test_validate_budget_parses_currency():
    v = A.ValidateTripForm()
    assert v.validate_budget("€350", CollectingDispatcher(), make_tracker(), {}) == {"budget": 350}
    assert v.validate_budget("lots", CollectingDispatcher(), make_tracker(), {}) == {"budget": None}
