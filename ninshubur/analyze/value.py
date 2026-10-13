"""Value, cost and confidence. Every weight comes from config or policy and the formula is
spelled out in the suggestion's reasons so a human can audit it."""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..policy import Policy


@dataclass(frozen=True)
class Estimate:
    value: float
    cost: float
    confidence: float
    cost_parts: dict
    formula: str


def confidence(reputation: float, completeness: float, agreeing_sources: int, tier: int, policy: Policy) -> float:
    c = reputation * completeness + 0.15 * max(0, agreeing_sources - 1)
    cap = float(policy.tier(tier).get("confidence_cap", 0.95))
    if tier >= 2 and agreeing_sources <= 1:
        cap = min(cap, 0.5)
    return round(max(0.0, min(cap, c)), 3)


def estimate(kind: str, *, cfg: Config, policy: Policy, tier: int, profile_weight: float, relevance: float,
             conf: float, timing_factor: float, minutes: float, travel_usd: float = 0.0, llm_usd: float = 0.0,
             calibration: float = 1.0) -> Estimate:
    base = float(cfg.get("base_value_usd", kind, default=100.0))
    tier_factor = float(policy.tier(tier).get("value_factor", 1.0))
    value = base * profile_weight * relevance * conf * timing_factor * tier_factor * calibration
    rate = float(cfg.get("budgets", "hourly_rate_usd", default=60.0))
    human = minutes / 60.0 * rate
    cost = human + travel_usd + llm_usd
    parts = {"human_minutes": minutes, "human_usd": round(human, 2), "travel_usd": round(travel_usd, 2), "llm_usd": round(llm_usd, 4)}
    formula = (f"value = {base:.0f} base x {profile_weight:.2f} weight x {relevance:.2f} relevance x {conf:.2f} confidence "
               f"x {timing_factor:.2f} timing x {tier_factor:.2f} tier x {calibration:.2f} calibration = {value:.0f} USD; "
               f"cost = {minutes:.0f} min x {rate:.0f}/h + {travel_usd:.0f} travel = {cost:.0f} USD")
    return Estimate(round(value, 2), round(cost, 2), conf, parts, formula)


def travel_band(home_base: str | None, city: str | None, home_country: str | None, country: str | None) -> float:
    """Phase 1 bands; Phase 4 replaces this with haversine from cities.json."""
    if not city or not home_base:
        return 600.0
    if city.strip().lower() == home_base.strip().lower():
        return 0.0
    if home_country and country and home_country.strip().lower() == country.strip().lower():
        return 150.0
    return 600.0
