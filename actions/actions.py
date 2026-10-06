"""
actions.py - custom actions for the Rasa Eco-Travel Advisor.

Responsibilities
  * validate_trip_form      : clean and validate slot values collected by the trip form
  * action_search_travel    : Amadeus sandbox flight offers + hotel list (with fallback)
  * action_calculate_carbon : Climatiq emissions per transport mode (with fallback factors)
  * action_rank_options     : weighted carbon/price/preference ranking -> colour-coded cards
  * action_show_hotels      : eco-certified accommodation carousel
  * action_show_offsets     : curated carbon-offset programmes (reduce first, offset second)
  * action_detect_location  : GPS (reverse geocoding via OpenCage) or manual fallback
  * action_handover         : package full conversation context for a human advisor
  * action_delete_data      : GDPR right to erasure (resets the conversation)
  * action_default_fallback : two-stage clarification, then escalation

Every external call has a short timeout, catches all request/parse errors and falls back
to cached or static data, telling the user plainly when figures are estimates.

All secrets come from environment variables (see .env.example); nothing is hard-coded.
"""
import json
import logging
import math
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Text, Tuple

import requests
from rasa_sdk import Action, FormValidationAction, Tracker
from rasa_sdk.events import EventType, FollowupAction, Restarted, SlotSet
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.types import DomainDict

from actions.scoring import carbon_band, rank_options

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Configuration (environment variables)
# --------------------------------------------------------------------------
API_TIMEOUT = float(os.getenv("API_TIMEOUT_SECONDS", "2.0"))   # keeps turns < 3 s
CLIMATIQ_KEY = os.getenv("CLIMATIQ_API_KEY", "")
CLIMATIQ_DATA_VERSION = os.getenv("CLIMATIQ_DATA_VERSION", "^21")
AMADEUS_ID = os.getenv("AMADEUS_CLIENT_ID", "")
AMADEUS_SECRET = os.getenv("AMADEUS_CLIENT_SECRET", "")
AMADEUS_BASE = os.getenv("AMADEUS_BASE_URL", "https://test.api.amadeus.com")
OPENCAGE_KEY = os.getenv("OPENCAGE_API_KEY", "")
ADVISOR_WEBHOOK = os.getenv("ADVISOR_WEBHOOK_URL", "")
HANDOVER_DIR = Path(os.getenv("HANDOVER_DIR", "handover_queue"))

DATA_DIR = Path(__file__).parent / "data"


def _load(name: str) -> Dict:
    with open(DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


CITIES = {k: v for k, v in _load("cities.json").items() if not k.startswith("_")}
ECO_HOTELS = _load("eco_hotels.json")
OFFSETS = _load("offsets.json")["programmes"]

# Climatiq activity IDs per mode (verify in the Climatiq Data Explorer; overridable by env)
CLIMATIQ_ACTIVITY = {
    "rail": os.getenv("CLIMATIQ_RAIL_ID", "passenger_train-route_type_national_rail-fuel_source_na"),
    "coach": os.getenv("CLIMATIQ_COACH_ID", "passenger_vehicle-vehicle_type_coach-fuel_source_na-distance_na-engine_size_na"),
    "flight": os.getenv("CLIMATIQ_FLIGHT_ID", "passenger_flight-route_type_international-aircraft_type_na-distance_short_haul_lt_3700km-class_economy-rf_included-distance_uplift_included"),
    "car": os.getenv("CLIMATIQ_CAR_ID", "passenger_vehicle-vehicle_type_car-fuel_source_petrol-engine_size_na-vehicle_age_na-vehicle_weight_na"),
}

# Static fallback emission factors, kg CO2e per passenger-km (car: per vehicle-km)
FALLBACK_FACTORS = {"rail": 0.035, "coach": 0.027, "flight": 0.151, "car": 0.164}
# Real-world routing detours relative to great-circle distance
DETOUR = {"rail": 1.20, "coach": 1.25, "car": 1.25, "flight": 1.08}
MODE_LABEL = {"rail": "🚆 Train", "coach": "🚌 Coach", "flight": "✈️ Flight", "car": "🚗 Car"}

_cache: Dict[str, Any] = {}      # simple in-process cache (route+mode -> result)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def haversine_km(a: Dict, b: Dict) -> float:
    """Great-circle distance between two {'lat','lon'} points in km."""
    r = 6371.0
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp = p2 - p1
    dl = math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def geocode_city(name: Optional[str]) -> Optional[Dict]:
    """Return city geodata from the local table, else OpenCage forward geocoding."""
    if not name:
        return None
    key = name.strip().lower()
    if key in CITIES:
        return {**CITIES[key], "name": name.strip().title()}
    if not OPENCAGE_KEY:
        return None
    try:
        r = requests.get("https://api.opencagedata.com/geocode/v1/json",
                         params={"q": name, "key": OPENCAGE_KEY, "limit": 1, "no_annotations": 1},
                         timeout=API_TIMEOUT)
        r.raise_for_status()
        res = r.json()["results"][0]
        return {"lat": res["geometry"]["lat"], "lon": res["geometry"]["lng"], "iata": None,
                "country": res["components"].get("country_code", "").upper(),
                "rail_hub": True, "name": name.strip().title()}
    except Exception as exc:  # network error, quota, empty result, bad JSON
        logger.warning("OpenCage geocoding failed for %s: %s", name, exc)
        return None


def nearest_city(lat: float, lon: float) -> Tuple[str, Dict]:
    """Offline reverse-geocoding fallback: nearest city in the local table."""
    point = {"lat": lat, "lon": lon}
    name = min(CITIES, key=lambda c: haversine_km(point, CITIES[c]))
    return name.title(), CITIES[name]


def previous_slot_value(tracker: Tracker, slot: str) -> Any:
    """Value of a slot before the latest user message (used to undo accidental overwrites)."""
    events = tracker.events
    last_user = max((i for i, e in enumerate(events) if e.get("event") == "user"), default=len(events))
    value = None
    for e in events[:last_user]:
        if e.get("event") == "slot" and e.get("name") == slot:
            value = e.get("value")
    return value


def parse_budget(value: Any) -> Optional[float]:
    """Extract a positive number from '€300', '300 euros', 300 ..."""
    if value is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(value))
    if not m:
        return None
    amount = float(m.group().replace(",", "."))
    return amount if amount > 0 else None


# --------------------------------------------------------------------------
# External API clients (each with timeout + fallback)
# --------------------------------------------------------------------------
def climatiq_estimate(mode: str, distance_km: float, passengers: int) -> Tuple[float, str]:
    """
    Return (kg CO2e per person, source).
    Calls Climatiq /data/v1/estimate; on any failure uses static fallback factors.
    """
    cache_key = f"climatiq:{mode}:{round(distance_km)}:{passengers}"
    if cache_key in _cache:
        return _cache[cache_key]

    result: Tuple[float, str]
    if CLIMATIQ_KEY:
        try:
            params = {"distance": round(distance_km, 1), "distance_unit": "km"}
            if mode != "car":
                params["passengers"] = passengers
            body = {"emission_factor": {"activity_id": CLIMATIQ_ACTIVITY[mode],
                                        "data_version": CLIMATIQ_DATA_VERSION},
                    "parameters": params}
            r = requests.post("https://api.climatiq.io/data/v1/estimate", json=body,
                              headers={"Authorization": f"Bearer {CLIMATIQ_KEY}"},
                              timeout=API_TIMEOUT)
            r.raise_for_status()
            total = float(r.json()["co2e"])
            # passenger modes return the total for all passengers; car returns per vehicle
            per_person = total / max(passengers, 1)
            result = (round(per_person, 1), "Climatiq")
            _cache[cache_key] = result
            return result
        except Exception as exc:
            logger.warning("Climatiq failed for %s (%s); using fallback factor", mode, exc)

    factor = FALLBACK_FACTORS[mode]
    per_person = factor * distance_km / (max(passengers, 1) if mode == "car" else 1)
    result = (round(per_person, 1), "estimate")
    _cache[cache_key] = result
    return result


def amadeus_token() -> Optional[str]:
    """OAuth2 client-credentials token for the Amadeus sandbox (cached until expiry)."""
    tok = _cache.get("amadeus_token")
    if tok and tok["expires"] > time.time():
        return tok["value"]
    if not (AMADEUS_ID and AMADEUS_SECRET):
        return None
    try:
        r = requests.post(f"{AMADEUS_BASE}/v1/security/oauth2/token",
                          data={"grant_type": "client_credentials", "client_id": AMADEUS_ID,
                                "client_secret": AMADEUS_SECRET}, timeout=API_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        _cache["amadeus_token"] = {"value": data["access_token"],
                                   "expires": time.time() + int(data.get("expires_in", 1700)) - 60}
        return data["access_token"]
    except Exception as exc:
        logger.warning("Amadeus auth failed: %s", exc)
        return None


def amadeus_cheapest_flight(origin_iata: str, dest_iata: str, travel_date: str, adults: int) -> Optional[float]:
    """Cheapest sandbox flight price per person in EUR, or None on failure."""
    token = amadeus_token()
    if not token or not origin_iata or not dest_iata:
        return None
    try:
        r = requests.get(f"{AMADEUS_BASE}/v2/shopping/flight-offers",
                         headers={"Authorization": f"Bearer {token}"},
                         params={"originLocationCode": origin_iata, "destinationLocationCode": dest_iata,
                                 "departureDate": travel_date, "adults": adults, "currencyCode": "EUR",
                                 "max": 5}, timeout=API_TIMEOUT)
        r.raise_for_status()
        offers = r.json().get("data", [])
        if not offers:
            return None
        return min(float(o["price"]["grandTotal"]) for o in offers) / max(adults, 1)
    except Exception as exc:
        logger.warning("Amadeus flight search failed: %s", exc)
        return None


def amadeus_hotel_names(city_iata: str) -> List[str]:
    """Hotel names from the Amadeus sandbox hotel list (used to cross-check the curated DB)."""
    token = amadeus_token()
    if not token or not city_iata:
        return []
    try:
        r = requests.get(f"{AMADEUS_BASE}/v1/reference-data/locations/hotels/by-city",
                         headers={"Authorization": f"Bearer {token}"},
                         params={"cityCode": city_iata, "radius": 5}, timeout=API_TIMEOUT)
        r.raise_for_status()
        return [h.get("name", "") for h in r.json().get("data", [])[:20]]
    except Exception as exc:
        logger.warning("Amadeus hotel list failed: %s", exc)
        return []


def iso_date(text: Optional[str]) -> str:
    """Best-effort conversion of the travel_date slot to YYYY-MM-DD for Amadeus."""
    try:
        from dateutil import parser
        d = parser.parse(text, fuzzy=True, default=datetime(date.today().year, 1, 1))
        if d.date() < date.today():
            d = d.replace(year=d.year + 1)
        return d.date().isoformat()
    except Exception:
        return (date.today().replace(day=1)).isoformat()


# --------------------------------------------------------------------------
# Form validation
# --------------------------------------------------------------------------
class ValidateTripForm(FormValidationAction):
    """Validates and normalises trip-intake slots; enables adaptive questioning."""

    def name(self) -> Text:
        return "validate_trip_form"

    async def extract_origin(self, dispatcher, tracker: Tracker, domain: DomainDict) -> Dict[Text, Any]:
        """If we asked for the origin and the user typed only a city (tagged as destination
        or untagged), treat it as the origin."""
        if tracker.get_slot("requested_slot") != "origin":
            return {}
        if next(tracker.get_latest_entity_values("origin"), None):
            return {}
        city = next(tracker.get_latest_entity_values("destination"), None) or tracker.latest_message.get("text", "")
        if geocode_city(city):
            return {"origin": city.strip().title()}
        return {}

    def validate_origin(self, value: Any, dispatcher, tracker: Tracker, domain: DomainDict) -> Dict[Text, Any]:
        city = geocode_city(value)
        if not city:
            dispatcher.utter_message(text=f"Sorry, I couldn't find '{value}'. Could you type the nearest city?")
            return {"origin": None}
        return {"origin": city["name"]}

    def validate_destination(self, value: Any, dispatcher, tracker: Tracker, domain: DomainDict) -> Dict[Text, Any]:
        city = geocode_city(value)
        if not city:
            dispatcher.utter_message(text=f"I don't have data for '{value}' yet. Try a nearby major city.")
            return {"destination": None}
        if tracker.get_slot("origin") and city["name"].lower() == str(tracker.get_slot("origin")).lower():
            if tracker.get_slot("requested_slot") == "origin":
                return {"destination": previous_slot_value(tracker, "destination")}
            dispatcher.utter_message(text="Your destination is the same as your starting point — where would you like to go?")
            return {"destination": None}
        return {"destination": city["name"]}

    def validate_travel_date(self, value: Any, dispatcher, tracker: Tracker, domain: DomainDict) -> Dict[Text, Any]:
        if not value or len(str(value).strip()) < 3:
            dispatcher.utter_message(text="Please give a date, e.g. '12 June' or 'next Friday'.")
            return {"travel_date": None}
        return {"travel_date": str(value).strip()}

    def validate_budget(self, value: Any, dispatcher, tracker: Tracker, domain: DomainDict) -> Dict[Text, Any]:
        amount = parse_budget(value)
        if amount is None:
            dispatcher.utter_message(text="Please enter your budget as a number, e.g. 300.")
            return {"budget": None}
        return {"budget": int(amount) if amount.is_integer() else amount}

    def validate_sustainability_level(self, value: Any, dispatcher, tracker: Tracker, domain: DomainDict) -> Dict[Text, Any]:
        v = str(value).lower()
        if v in ("high", "medium", "low"):
            return {"sustainability_level": v}
        dispatcher.utter_message(text="Please choose High, Medium or Low.")
        return {"sustainability_level": None}


# --------------------------------------------------------------------------
# Search, carbon calculation and ranking
# --------------------------------------------------------------------------
class ActionSearchTravel(Action):
    """Builds candidate transport options with prices (Amadeus for flights, estimates otherwise)."""

    def name(self) -> Text:
        return "action_search_travel"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        o, d = geocode_city(tracker.get_slot("origin")), geocode_city(tracker.get_slot("destination"))
        if not o or not d:
            dispatcher.utter_message(text="I couldn't locate one of the cities, so I can't search right now.")
            return [SlotSet("transport_results", None)]
        travellers = int(tracker.get_slot("travellers") or 1)
        gc_km = haversine_km(o, d)
        options = []
        for mode in ("rail", "coach", "flight", "car"):
            api_price = None
            if mode in ("rail", "coach") and not (o.get("rail_hub") and d.get("rail_hub")):
                continue                                   # no land connection in demo data
            if mode == "flight" and gc_km < 150:
                continue                                   # flights not sensible for short hops
            km = gc_km * DETOUR[mode]
            if mode == "rail":
                price, hours = 0.13 * km, km / 110 + 0.5
            elif mode == "coach":
                price, hours = 0.06 * km, km / 70 + 0.3
            elif mode == "car":
                price, hours = 0.12 * km / travellers, km / 85
            else:
                api_price = amadeus_cheapest_flight(o.get("iata"), d.get("iata"),
                                                    iso_date(tracker.get_slot("travel_date")), travellers)
                price = api_price if api_price else 60 + 0.08 * km
                hours = km / 750 + 2.5                     # incl. airport transfer/check-in
            options.append({"mode": mode, "label": MODE_LABEL[mode], "distance_km": round(km),
                            "price_eur": round(price), "duration_h": round(hours, 1),
                            "price_source": "Amadeus" if api_price else "estimate"})
        return [SlotSet("transport_results", options)]


class ActionCalculateCarbon(Action):
    """Adds kg CO2e per person and a colour band to every transport option."""

    def name(self) -> Text:
        return "action_calculate_carbon"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        options = tracker.get_slot("transport_results") or []
        travellers = int(tracker.get_slot("travellers") or 1)
        estimated = False
        # Query all modes in parallel so the worst case is one API timeout, not four (latency < 3 s)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda o: climatiq_estimate(o["mode"], o["distance_km"], travellers), options))
        for opt, (kg, source) in zip(options, results):
            opt.update({"co2_kg": kg, "co2_source": source, "band": carbon_band(kg, opt["distance_km"])})
            estimated = estimated or source != "Climatiq"
        if estimated and options:
            reason = "didn't respond in time" if CLIMATIQ_KEY else "isn't configured in this deployment"
            dispatcher.utter_message(text=f"ℹ️ The live carbon service {reason}, so I'm using standard emission factors. Figures are estimates.")
        return [SlotSet("transport_results", options)]


class ActionRankOptions(Action):
    """Weighted carbon/price/preference ranking, rendered as colour-coded result cards."""

    def name(self) -> Text:
        return "action_rank_options"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        options = [o for o in (tracker.get_slot("transport_results") or []) if "co2_kg" in o]
        if not options:
            dispatcher.utter_message(text="I couldn't find transport options for this route. Would you like a human advisor to help?",
                                     buttons=[{"title": "Talk to an advisor", "payload": "/request_human"}])
            return []
        preferred = next(tracker.get_latest_entity_values("transport_mode"), None)
        result = rank_options(options, tracker.get_slot("sustainability_level"),
                              tracker.get_slot("budget"), preferred)
        ranked, over = result["ranked"], result["over_budget"]

        if not ranked:
            cheapest = min(over, key=lambda x: x["price_eur"])
            dispatcher.utter_message(text=f"None of the options fit your €{tracker.get_slot('budget'):.0f} budget. "
                                          f"The cheapest is {cheapest['label']} at €{cheapest['price_eur']}. "
                                          "Would you like to try other dates or a nearer destination?")
            ranked = over
        else:
            best = ranked[0]
            dispatcher.utter_message(text=f"Here are your options, best match first. Top pick: {best['label']} "
                                          f"— {best['co2_kg']} kg CO₂e per person, about €{best['price_eur']}.")

        dispatcher.utter_message(json_message={"type": "results_cards", "items": ranked + over})

        # Alert for high-emission options, always paired with a greener alternative
        greenest = min(options, key=lambda x: x["co2_kg"])
        for o in options:
            if o["band"] == "red" and greenest["co2_kg"] > 0:
                factor = o["co2_kg"] / greenest["co2_kg"]
                dispatcher.utter_message(json_message={
                    "type": "alert", "level": "red",
                    "text": f"{o['label']} emits about {factor:.0f}× more CO₂e than {greenest['label']} on this route."})
        return [SlotSet("transport_results", ranked + over)]


class ActionShowHotels(Action):
    """Eco-certified hotels (curated DB, cross-checked with Amadeus hotel list if available)."""

    def name(self) -> Text:
        return "action_show_hotels"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        dest = tracker.get_slot("destination") or next(tracker.get_latest_entity_values("destination"), None)
        if not dest:
            dispatcher.utter_message(response="utter_ask_destination")
            return []
        city = geocode_city(dest)
        hotels = ECO_HOTELS.get(dest.lower(), ECO_HOTELS["_default"])
        amadeus_names = amadeus_hotel_names(city.get("iata") if city else None)
        budget = tracker.get_slot("budget")
        items = []
        for h in sorted(hotels, key=lambda x: x["co2_per_night_kg"]):
            items.append({**h, "in_amadeus": any(h["name"].lower() in n.lower() for n in amadeus_names),
                          "within_budget": (not budget) or h["price_per_night"] <= budget})
        dispatcher.utter_message(text=f"Eco-certified places to stay in {dest.title()} (lowest footprint first):")
        dispatcher.utter_message(json_message={"type": "hotel_carousel", "items": items,
                                               "source": "Curated eco-certification database (demo)"})
        return [SlotSet("hotel_results", items)]


class ActionShowOffsets(Action):
    """Offsets shown only after reduction advice, with transparency notes (anti-greenwashing)."""

    def name(self) -> Text:
        return "action_show_offsets"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        options = tracker.get_slot("transport_results") or []
        chosen = options[0] if options else None
        text = "First, the best way to cut emissions is choosing a lower-carbon option. "
        if chosen:
            tonnes = chosen["co2_kg"] / 1000
            costs = ", ".join(f"{p['name']} ≈ €{p['price_per_tonne_eur'] * tonnes:.2f}" for p in OFFSETS)
            text += f"For your top option ({chosen['co2_kg']} kg CO₂e), offsetting would cost about: {costs}."
        else:
            text += "Here are vetted programmes:"
        dispatcher.utter_message(text=text)
        dispatcher.utter_message(json_message={"type": "offsets", "items": OFFSETS,
                                               "disclaimer": "Offsets do not make travel 'zero impact'. Removal credits are more durable than avoidance credits."})
        return []


# --------------------------------------------------------------------------
# Location
# --------------------------------------------------------------------------
class ActionDetectLocation(Action):
    """Uses GPS coordinates sent in message metadata (with consent); stores city only."""

    def name(self) -> Text:
        return "action_detect_location"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        loc = (tracker.latest_message.get("metadata") or {}).get("location")
        if not loc:
            dispatcher.utter_message(text="I couldn't access your location. Please allow location in your browser, or just type your city.")
            return []
        lat, lon = round(float(loc["lat"]), 2), round(float(loc["lon"]), 2)   # coarse = privacy
        city_name = None
        if OPENCAGE_KEY:
            try:
                r = requests.get("https://api.opencagedata.com/geocode/v1/json",
                                 params={"q": f"{lat},{lon}", "key": OPENCAGE_KEY, "limit": 1},
                                 timeout=API_TIMEOUT)
                r.raise_for_status()
                comp = r.json()["results"][0]["components"]
                city_name = comp.get("city") or comp.get("town") or comp.get("village")
            except Exception as exc:
                logger.warning("OpenCage reverse geocoding failed: %s", exc)
        if not city_name:
            city_name, _ = nearest_city(lat, lon)
        dispatcher.utter_message(text=f"📍 You seem to be near {city_name}. I'll use that as your starting point (coordinates are not stored).")
        return [SlotSet("origin", city_name), SlotSet("location_consent", True)]


# --------------------------------------------------------------------------
# Human handover
# --------------------------------------------------------------------------
class ActionHandover(Action):
    """Packages full conversational context and sends it to the advisor queue."""

    def name(self) -> Text:
        return "action_handover"

    @staticmethod
    def build_payload(tracker: Tracker, reason: str) -> Dict[str, Any]:
        transcript = []
        for e in tracker.events:
            if e.get("event") == "user":
                transcript.append({"from": "user", "text": e.get("text"),
                                   "intent": (e.get("parse_data") or {}).get("intent", {}).get("name")})
            elif e.get("event") == "bot" and e.get("text"):
                transcript.append({"from": "bot", "text": e.get("text")})
        slot_keys = ["origin", "destination", "travel_date", "budget", "travellers", "sustainability_level"]
        return {
            "ticket_id": f"ECO-{uuid.uuid4().hex[:8].upper()}",
            "created_at": datetime.utcnow().isoformat() + "Z",
            "conversation_id": tracker.sender_id,
            "reason": reason,
            "trip": {k: tracker.get_slot(k) for k in slot_keys},
            "ranked_transport": tracker.get_slot("transport_results"),
            "hotels_viewed": tracker.get_slot("hotel_results"),
            "transcript": transcript,
            "consent_given": True,
        }

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        reason = "repeated_misunderstanding" if (tracker.get_slot("fallback_count") or 0) >= 2 else "user_request"
        payload = self.build_payload(tracker, reason)
        delivered = False
        if ADVISOR_WEBHOOK:
            try:
                r = requests.post(ADVISOR_WEBHOOK, json=payload, timeout=API_TIMEOUT)
                r.raise_for_status()
                delivered = True
            except Exception as exc:
                logger.error("Advisor webhook failed: %s", exc)
        if not delivered:                        # fallback: local queue file read by advisors
            try:
                HANDOVER_DIR.mkdir(parents=True, exist_ok=True)
                with open(HANDOVER_DIR / f"{payload['ticket_id']}.json", "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
                delivered = True
            except OSError as exc:
                logger.error("Could not write handover file: %s", exc)
        if delivered:
            dispatcher.utter_message(text=f"👤 You're now in the queue for a travel advisor (ticket {payload['ticket_id']}). "
                                          "They can see your trip details and this chat, so you won't need to repeat yourself.")
            dispatcher.utter_message(json_message={"type": "handover", "status": "queued", "ticket": payload["ticket_id"]})
            return [SlotSet("handover_active", True), SlotSet("fallback_count", 0)]
        dispatcher.utter_message(text="Sorry, I couldn't reach an advisor right now. Please email advisors@example.com or try again shortly.")
        return []


class ActionDeleteData(Action):
    """GDPR right to erasure: wipes all slots and conversation state."""

    def name(self) -> Text:
        return "action_delete_data"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        dispatcher.utter_message(text="Done — I've deleted your trip details and this conversation's data. 🗑️")
        return [Restarted()]


# --------------------------------------------------------------------------
# Error recovery: two-stage clarification, then escalation
# --------------------------------------------------------------------------
INTENT_LABELS = {
    "plan_trip": "Plan a trip", "compare_transport": "Compare transport", "ask_carbon": "Check carbon footprint",
    "find_hotels": "Find eco hotels", "ask_offset": "Carbon offsets", "request_human": "Talk to a person",
    "ask_privacy": "Privacy", "explain_metric": "Explain the scores", "share_location": "Use my location",
}


def consecutive_fallbacks(tracker: Tracker) -> int:
    """Number of consecutive nlu_fallback user turns at the end of the conversation."""
    count = 0
    for e in reversed(tracker.events):
        if e.get("event") == "user":
            if (e.get("parse_data") or {}).get("intent", {}).get("name") == "nlu_fallback":
                count += 1
            else:
                break
    return count


class ActionDefaultFallback(Action):
    """Stage 1: 'did you mean' top intents. Stage 2: constrained menu. Stage 3: handover."""

    def name(self) -> Text:
        return "action_default_fallback"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        stage = consecutive_fallbacks(tracker)
        if stage <= 1:
            ranking = [r for r in tracker.latest_message.get("intent_ranking", [])
                       if r.get("name") in INTENT_LABELS][:2]
            buttons = [{"title": INTENT_LABELS[r["name"]], "payload": f"/{r['name']}"} for r in ranking]
            buttons.append({"title": "Something else", "payload": "/out_of_scope"})
            dispatcher.utter_message(text="Sorry, I'm not sure I understood. Did you mean:", buttons=buttons)
            return [SlotSet("fallback_count", 1)]
        if stage == 2:
            buttons = [{"title": INTENT_LABELS[i], "payload": f"/{i}"}
                       for i in ("plan_trip", "compare_transport", "ask_carbon", "request_human")]
            dispatcher.utter_message(text="Let's try it this way — please pick one of these:", buttons=buttons)
            return [SlotSet("fallback_count", 2)]
        dispatcher.utter_message(text="I'm still having trouble understanding, so a human advisor may help best.")
        return [SlotSet("fallback_count", 3), FollowupAction("utter_ask_handover_consent")]
