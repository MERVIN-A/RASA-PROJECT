"""
scoring.py - pure functions for carbon banding and weighted ranking.

Kept free of Rasa imports so it can be unit-tested in isolation (tests/test_scoring.py).

Score for each option (higher = better):

    S = w_c * (1 - C_norm) + w_p * (1 - P_norm) + w_u * U

    C_norm, P_norm : carbon (kg CO2e/person) and price (EUR/person), min-max normalised
                     across the candidate options of the current search
    U              : preference match in [0, 1] (preferred mode + journey time)

Weights depend on the user's stated sustainability level.
"""
from typing import Dict, List, Optional

# Weights (carbon, price, preference) per sustainability level
WEIGHTS: Dict[str, tuple] = {
    "high": (0.60, 0.20, 0.20),
    "medium": (0.40, 0.40, 0.20),
    "low": (0.25, 0.55, 0.20),
}

# Colour-band thresholds in kg CO2e per passenger-km
GREEN_MAX = 0.06
AMBER_MAX = 0.15


def carbon_band(kg_co2e: float, distance_km: float) -> str:
    """Return 'green', 'amber' or 'red' from emission intensity per passenger-km."""
    if distance_km <= 0:
        return "amber"
    intensity = kg_co2e / distance_km
    if intensity < GREEN_MAX:
        return "green"
    if intensity <= AMBER_MAX:
        return "amber"
    return "red"


def _normalise(values: List[float]) -> List[float]:
    """Min-max normalise to [0, 1]; identical values all map to 0 (no penalty)."""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def preference_match(option: Dict, preferred_mode: Optional[str], durations_norm: float) -> float:
    """U in [0,1]: half for matching the preferred mode, half for shorter journey time."""
    mode_part = 1.0 if (preferred_mode and option["mode"] == preferred_mode) else 0.5
    time_part = 1.0 - durations_norm
    return 0.5 * mode_part + 0.5 * time_part


def rank_options(options: List[Dict], level: str, budget: Optional[float],
                 preferred_mode: Optional[str] = None) -> Dict[str, List[Dict]]:
    """
    Score and sort transport options.

    Each option needs: mode, co2_kg, price_eur, duration_h.
    Options above budget are not penalised but moved to 'over_budget' so the
    bot can explain the trade-off transparently instead of silently hiding them.
    """
    if not options:
        return {"ranked": [], "over_budget": []}

    w_c, w_p, w_u = WEIGHTS.get(level or "medium", WEIGHTS["medium"])
    c_norm = _normalise([o["co2_kg"] for o in options])
    p_norm = _normalise([o["price_eur"] for o in options])
    d_norm = _normalise([o["duration_h"] for o in options])

    scored = []
    for i, opt in enumerate(options):
        u = preference_match(opt, preferred_mode, d_norm[i])
        score = w_c * (1 - c_norm[i]) + w_p * (1 - p_norm[i]) + w_u * u
        scored.append({**opt, "score": round(score, 3)})

    scored.sort(key=lambda o: o["score"], reverse=True)
    if budget:
        within = [o for o in scored if o["price_eur"] <= budget]
        over = [o for o in scored if o["price_eur"] > budget]
    else:
        within, over = scored, []
    return {"ranked": within, "over_budget": over}
