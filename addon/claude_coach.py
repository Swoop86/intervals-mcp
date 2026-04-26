"""
claude_coach.py
---------------
Fetches training context from intervals.icu, calls Claude with tool-use
for structured coaching, applies calendar adjustments, notifies HA.
"""

from __future__ import annotations

import base64
import hashlib
import os
import json
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger("claude_coach")

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
def _safe_str(val: str | None) -> str:
    if not val or val.strip().lower() in ("null", "none"):
        return ""
    return val.strip()

ATHLETE_ID        = _safe_str(os.environ.get("INTERVALS_ATHLETE_ID"))
API_KEY           = _safe_str(os.environ.get("INTERVALS_API_KEY"))
ANTHROPIC_API_KEY = _safe_str(os.environ.get("ANTHROPIC_API_KEY"))
COACH_SECRET      = _safe_str(os.environ.get("COACH_SECRET"))
CLAUDE_MODEL      = _safe_str(os.environ.get("CLAUDE_MODEL")) or "claude-sonnet-4-6"
HA_TOKEN          = _safe_str(os.environ.get("HA_TOKEN"))
HA_MOBILE_SERVICE = _safe_str(os.environ.get("HA_MOBILE_SERVICE"))
HA_URL            = "http://supervisor/core"
BASE_URL          = "https://intervals.icu/api/v1"

ACTIVITIES_DAYS  = 28
WELLNESS_DAYS    = 14
PLANNED_DAYS     = 21

MAX_TOKENS       = 2048
MAX_RETRIES      = 2
RETRY_DELAY      = 3

_PROFILE_PATH = "/data/athlete_profile.json"
_GOAL_PATH    = "/data/athlete_goal.json"

_DEFAULT_PROFILE_STR = """Athlete profile not yet configured.
Use the update_profile and set_race_goal MCP tools to personalise coaching."""

# ---------------------------------------------------------------------------
# Encrypted JSON storage helpers (mirrors mcp_server.py)
# ---------------------------------------------------------------------------
try:
    from cryptography.fernet import Fernet as _Fernet, InvalidToken as _FernetInvalidToken
    _CRYPTO_AVAILABLE = True
except ImportError:
    _Fernet = None
    _FernetInvalidToken = Exception
    _CRYPTO_AVAILABLE = False


def _fernet():
    if not _CRYPTO_AVAILABLE or not COACH_SECRET:
        return None
    dk = hashlib.pbkdf2_hmac("sha256", COACH_SECRET.encode(), ATHLETE_ID.encode(), 100_000)
    return _Fernet(base64.urlsafe_b64encode(dk))


def _read_json_file(path: str) -> dict | None:
    try:
        raw = open(path, "rb").read()
    except FileNotFoundError:
        return None
    f = _fernet()
    if f:
        try:
            raw = f.decrypt(raw)
        except _FernetInvalidToken:
            log.warning("Could not decrypt %s", path)
            return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def load_athlete_profile() -> str:
    data = _read_json_file(_PROFILE_PATH)
    if data:
        lines = []
        field_labels = {
            "sport": "Sport",
            "age": "Age",
            "location": "Training location",
            "timezone": "Timezone",
            "preferred_units": "Preferred units",
            "training_days_per_week": "Training days/week",
            "easy_pace_min_per_km": "Easy pace (min/km)",
            "threshold_pace_min_per_km": "Threshold pace (min/km)",
            "weekly_volume_km": "Weekly volume (km)",
            "known_limiters": "Known limiters",
            "notes": "Notes",
        }
        for key, label in field_labels.items():
            val = data.get(key)
            if val is None or val == "" or val == []:
                continue
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val)
            lines.append(f"- {label}: {val}")
        return "\n".join(lines) if lines else _DEFAULT_PROFILE_STR
    return _DEFAULT_PROFILE_STR


def load_race_goal() -> dict | None:
    return _read_json_file(_GOAL_PATH)


def load_coaching_style() -> tuple[str, str] | None:
    """Returns (display_name, description) or None if no style is set."""
    data = _read_json_file(_PROFILE_PATH)
    if data:
        name = data.get("coaching_methodology", "")
        desc = data.get("coaching_description", "")
        if name and desc:
            return name, desc
    return None


def today_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def days_ago_iso(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def in_days_iso(n: int) -> str:
    return (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Intervals.icu API (uses shared httpx client)
# ---------------------------------------------------------------------------
def _auth() -> tuple[str, str]:
    return ("API_KEY", API_KEY)


def _icu_raise(r: httpx.Response) -> None:
    if r.is_error:
        body = r.text[:800]
        log.error("intervals.icu %s %s → %d: %s", r.request.method, r.request.url, r.status_code, body)
        raise httpx.HTTPStatusError(
            f"HTTP {r.status_code} from intervals.icu: {body}",
            request=r.request,
            response=r,
        )


async def icu_get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> Any:
    r = await client.get(f"{BASE_URL}/{path}", auth=_auth(), params=params)
    _icu_raise(r)
    return r.json()


async def icu_put(client: httpx.AsyncClient, path: str, payload: dict) -> Any:
    r = await client.put(f"{BASE_URL}/{path}", auth=_auth(), json=payload)
    _icu_raise(r)
    return r.json()


async def icu_delete(client: httpx.AsyncClient, path: str) -> None:
    r = await client.delete(f"{BASE_URL}/{path}", auth=_auth())
    _icu_raise(r)


# ---------------------------------------------------------------------------
# Context fetching
# ---------------------------------------------------------------------------
def _extract_athlete_zones(athlete_data: dict) -> dict:
    """Pull coaching-relevant zone data out of sportSettings into a flat dict."""
    import math as _math

    def ms_to_min_km(v):
        return round(1000 / (v * 60), 2) if v and v > 0 else None

    def ms_to_min_100m(v):
        return round(100 / (v * 60), 2) if v and v > 0 else None

    zones: dict = {
        "ftp_watts": athlete_data.get("ftp"),
        "lthr_bpm": athlete_data.get("lthr"),
        "weight_kg": athlete_data.get("weight"),
    }

    for ss in athlete_data.get("sportSettings") or []:
        sport = ss.get("activity_type")
        if not sport:
            continue
        p = sport.lower()
        if ss.get("lthr"):
            zones[f"{p}_lthr_bpm"] = ss["lthr"]
        if ss.get("max_heart_rate"):
            zones[f"{p}_max_hr_bpm"] = ss["max_heart_rate"]
        if ss.get("zones_heart_rate"):
            zones[f"{p}_hr_zones_bpm"] = ss["zones_heart_rate"]
        if sport == "Run":
            if ss.get("threshold_pace"):
                zones["running_threshold_pace_min_per_km"] = ms_to_min_km(ss["threshold_pace"])
            if ss.get("pace_zones"):
                zones["running_pace_zones_min_per_km"] = [
                    ms_to_min_km(v) for v in ss["pace_zones"] if v
                ]
        elif sport == "Ride":
            ftp = ss.get("ftp") or ss.get("threshold_power")
            if ftp:
                zones["cycling_ftp_watts"] = ftp
            if ss.get("zones_power"):
                zones["cycling_power_zones_watts"] = ss["zones_power"]
        elif sport == "Swim":
            if ss.get("threshold_pace"):
                zones["swim_css_min_per_100m"] = ms_to_min_100m(ss["threshold_pace"])

    return zones


async def fetch_context(client: httpx.AsyncClient, activity_id: str) -> dict:
    athlete = f"athlete/{ATHLETE_ID}"

    activities, wellness, planned, athlete_data = await asyncio.gather(
        icu_get(client, f"{athlete}/activities",
                {"oldest": days_ago_iso(ACTIVITIES_DAYS), "newest": today_iso()}),
        icu_get(client, f"{athlete}/wellness",
                {"oldest": days_ago_iso(WELLNESS_DAYS), "newest": today_iso()}),
        icu_get(client, f"{athlete}/events",
                {"oldest": today_iso(), "newest": in_days_iso(PLANNED_DAYS)}),
        icu_get(client, athlete),
    )

    activities_sorted = sorted(activities, key=lambda a: a.get("start_date_local", ""))

    latest = None
    if activity_id:
        latest = next((a for a in activities if a.get("id") == activity_id), None)
    if not latest and activities_sorted:
        latest = activities_sorted[-1]

    clean_wellness = [_clean_wellness(w) for w in wellness]
    clean_activities = [_clean_activity(a) for a in activities_sorted]

    return {
        "latest_activity": _clean_activity(latest) if latest else None,
        "recent_activities": clean_activities,
        "wellness": clean_wellness,
        "planned_workouts": [
            _clean_planned(e) for e in planned if e.get("type") != "Note"
        ],
        "current_fitness": {
            "ctl": latest.get("icu_ctl") if latest else None,
            "atl": latest.get("icu_atl") if latest else None,
            "tsb": latest.get("icu_tsb") if latest else None,
        },
        "readiness_metrics": _compute_readiness_metrics(clean_wellness, clean_activities),
        "athlete_zones": _extract_athlete_zones(athlete_data),
    }


_RUN_TYPES = frozenset({"Run", "VirtualRun", "TrailRun", "Treadmill"})


def _ms_to_min_per_km(ms: float | None) -> float | None:
    if not ms:
        return None
    return round(1000 / (ms * 60), 3)


def _clean_activity(a: dict) -> dict:
    cadence = a.get("average_cadence")
    if cadence is not None and a.get("type") in _RUN_TYPES:
        cadence_fields = {
            "avg_cadence_spm_per_foot": cadence,
            "avg_cadence_total_spm": round(cadence * 2, 1),
        }
    elif cadence is not None:
        cadence_fields = {"avg_cadence_rpm": cadence}
    else:
        cadence_fields = {}

    d = {
        "id": a.get("id"),
        "date": a.get("start_date_local", "")[:10],
        "name": a.get("name"),
        "type": a.get("type"),
        "duration_min": round(a.get("moving_time", 0) / 60, 1),
        "distance_km": round((a.get("distance", 0) or 0) / 1000, 2),
        "tss": a.get("icu_training_load"),
        "avg_hr": a.get("average_heartrate"),
        "max_hr": a.get("max_heartrate"),
        "avg_power": a.get("average_watts"),
        "elevation_m": a.get("total_elevation_gain"),
        "avg_temperature_c": a.get("average_temp"),
        "ctl": a.get("icu_ctl"),
        "atl": a.get("icu_atl"),
        "tsb": a.get("icu_tsb"),
        "perceived_effort": a.get("perceived_exertion"),
        "training_effect_aerobic": a.get("total_training_effect"),
        "training_effect_anaerobic": a.get("total_anaerobic_training_effect"),
        "training_effect_label": a.get("training_effect_label"),
    }
    d.update(cadence_fields)
    # Running dynamics and detected thresholds from Garmin (run types only)
    if a.get("type") in _RUN_TYPES:
        for src, dst in (
            ("avg_vertical_oscillation", "vertical_oscillation_cm"),
            ("avg_ground_contact_time", "ground_contact_time_ms"),
            ("avg_stride_length", "stride_length_m"),
            ("avg_vertical_ratio", "vertical_ratio_pct"),
        ):
            if a.get(src) is not None:
                d[dst] = a[src]
        if a.get("lthr_detected") is not None:
            d["lthr_detected_bpm"] = a["lthr_detected"]
        if a.get("lt_pace_detected") is not None:
            d["lt_pace_detected_min_per_km"] = _ms_to_min_per_km(a["lt_pace_detected"])
    return d


def _clean_wellness(w: dict) -> dict:
    return {
        "date": w.get("id"),
        "hrv": w.get("hrv"),
        "hrv_score": w.get("hrvScore"),        # HRV4Training readiness score (0–10)
        "resting_hr": w.get("restingHR"),
        "sleep_hours": round(w["sleepSecs"] / 3600, 1) if w.get("sleepSecs") else None,
        "sleep_quality": w.get("sleepQuality"), # 1–5 subjective rating
        "sleep_score": w.get("sleepScore"),     # device sleep score if available
        "spo2": w.get("spO2"),
        "weight_kg": w.get("weight"),
        "mood": w.get("mood"),
        "motivation": w.get("motivation"),
        "soreness": w.get("soreness"),
        "fatigue": w.get("fatigue"),
        "stress": w.get("stress"),
    }


def _compute_readiness_metrics(wellness: list[dict], activities: list[dict]) -> dict:
    """Pre-compute HRV and load metrics so Claude gets labelled numbers, not raw lists."""
    import math

    # --- HRV metrics ---
    hrv_vals = [w["hrv"] for w in wellness if w.get("hrv")]
    today_hrv = hrv_vals[-1] if hrv_vals else None
    hrv_7 = hrv_vals[-7:] if len(hrv_vals) >= 2 else hrv_vals

    hrv_mean = sum(hrv_7) / len(hrv_7) if hrv_7 else None
    hrv_std = None
    hrv_cv = None
    hrv_zscore = None

    if hrv_7 and len(hrv_7) >= 2 and hrv_mean:
        variance = sum((v - hrv_mean) ** 2 for v in hrv_7) / len(hrv_7)
        hrv_std = math.sqrt(variance)
        hrv_cv = round(hrv_std / hrv_mean * 100, 1)
        if today_hrv is not None:
            hrv_zscore = round((today_hrv - hrv_mean) / hrv_std, 2) if hrv_std > 0 else 0.0

    # HRV-CV interpretation (IntervalCoach thresholds)
    hrv_cv_flag = None
    if hrv_cv is not None:
        if hrv_cv < 15:
            hrv_cv_flag = "stable — consistent recovery, intensity permitted"
        elif hrv_cv < 25:
            hrv_cv_flag = "moderate — standard programming"
        else:
            hrv_cv_flag = "volatile — restrict intensity despite daily readings"

    # --- Resting HR trend ---
    rhr_vals = [w["resting_hr"] for w in wellness if w.get("resting_hr")]
    rhr_7 = rhr_vals[-7:] if len(rhr_vals) >= 2 else rhr_vals
    rhr_mean = round(sum(rhr_7) / len(rhr_7), 1) if rhr_7 else None
    today_rhr = rhr_vals[-1] if rhr_vals else None

    # --- Recovery Index (RI) ---
    ri = None
    ri_flag = None
    if today_hrv and hrv_mean and today_rhr and rhr_mean and rhr_mean > 0:
        ri = round((today_hrv / hrv_mean) / (today_rhr / rhr_mean), 2)
        if ri >= 0.8:
            ri_flag = "green"
        elif ri >= 0.7:
            ri_flag = "amber"
        else:
            ri_flag = "red"

    # --- ACWR ---
    # Activities sorted oldest→newest; TSS per day
    acts_sorted = sorted(activities, key=lambda a: a.get("date", ""))
    tss_by_date: dict[str, float] = {}
    for a in acts_sorted:
        d = (a.get("date") or "")[:10]
        if d:
            tss_by_date[d] = tss_by_date.get(d, 0) + (a.get("tss") or 0)

    today_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    dates_28 = [(datetime.now(tz=timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(28)]
    dates_7 = dates_28[:7]

    tss_7 = sum(tss_by_date.get(d, 0) for d in dates_7)
    tss_28 = sum(tss_by_date.get(d, 0) for d in dates_28)
    avg_daily_28 = tss_28 / 28

    acwr = round(tss_7 / (avg_daily_28 * 7), 2) if avg_daily_28 > 0 else None
    acwr_flag = None
    if acwr is not None:
        if acwr < 0.8:
            acwr_flag = "underloading"
        elif acwr <= 1.3:
            acwr_flag = "optimal"
        elif acwr <= 1.5:
            acwr_flag = "caution — injury risk rising"
        else:
            acwr_flag = "danger — high injury risk, reduce load"

    return {
        "hrv_today": today_hrv,
        "hrv_7day_mean": round(hrv_mean, 1) if hrv_mean else None,
        "hrv_7day_std": round(hrv_std, 1) if hrv_std else None,
        "hrv_cv_7day_pct": hrv_cv,
        "hrv_cv_flag": hrv_cv_flag,
        "hrv_zscore_today": hrv_zscore,
        "rhr_today": today_rhr,
        "rhr_7day_mean": rhr_mean,
        "recovery_index": ri,
        "recovery_index_flag": ri_flag,
        "acwr": acwr,
        "acwr_flag": acwr_flag,
        "tss_last_7_days": round(tss_7, 1),
        "tss_last_28_days": round(tss_28, 1),
    }


def _clean_planned(e: dict) -> dict:
    return {
        "id": e.get("id"),
        "date": e.get("start_date_local", "")[:10],
        "name": e.get("name"),
        "type": e.get("type"),
        "description": e.get("description", ""),
        "target_tss": e.get("icu_training_load"),
        "duration_min": round(e.get("moving_time", 0) / 60, 1) if e.get("moving_time") else None,
    }


# ---------------------------------------------------------------------------
# Claude API — tool use for structured output
# ---------------------------------------------------------------------------
COACHING_TOOL = {
    "name": "submit_coaching_review",
    "description": (
        "Submit your coaching analysis and any plan adjustments. "
        "Call this exactly once with your complete response."
    ),
    "input_schema": {
        "type": "object",
        "required": ["analysis", "alert_level", "adjustments"],
        "properties": {
            "analysis": {
                "type": "string",
                "description": "2-3 paragraph coaching summary. Warm, direct tone.",
            },
            "alert_level": {
                "type": "string",
                "enum": ["green", "yellow", "red"],
            },
            "alert_reason": {
                "type": "string",
                "description": "One-line summary of status",
            },
            "adjustments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["event_id", "action", "reason"],
                    "properties": {
                        "event_id": {"type": "integer"},
                        "action": {"type": "string", "enum": ["modify", "remove"]},
                        "reason": {"type": "string"},
                        "changes": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "moving_time": {"type": "integer"},
                                "icu_training_load": {"type": "number"},
                                "start_date_local": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
}


def _build_system_prompt(
    athlete_profile: str,
    race_goal: dict | None = None,
    coaching_style: tuple[str, str] | None = None,
) -> str:
    if coaching_style:
        style_name, style_desc = coaching_style
        methodology_section = f"\nCOACHING METHODOLOGY: {style_name}\n{style_desc}\n"
    else:
        methodology_section = "\nCOACHING METHODOLOGY: General endurance principles — no specific methodology set.\n"

    if race_goal:
        goal_section = (
            f"\nRACE GOAL: {race_goal.get('event_name')} on {race_goal.get('event_date')}"
            f" ({race_goal.get('distance_km')} km)\n"
        )
        if race_goal.get("target_time"):
            goal_section += f"Target: {race_goal['target_time']}\n"
        goal_section += (
            f"Current phase: {race_goal.get('current_phase', 'unknown').upper()}"
            f" ({race_goal.get('weeks_to_race', '?')} weeks to race)\n"
        )
        if race_goal.get("notes"):
            goal_section += f"Notes: {race_goal['notes']}\n"
        goal_section += """
PERIODIZATION PHASES:
- Base (>16 weeks out): build aerobic base, high volume, low intensity (80/20 easy/hard)
- Build (8-16 weeks): introduce quality sessions — tempo, intervals, progression runs
- Peak (4-8 weeks): race-specific work, simulate race conditions, peak fitness
- Taper (1-4 weeks): reduce volume ~30%, maintain intensity, arrive fresh
- Race week (<1 week): minimal stress, short sharp efforts, conserve energy
"""
        mode_context = goal_section
    else:
        mode_context = "\nMODE: General improvement — no specific event targeted. Focus on building aerobic fitness and long-term consistency.\n"

    # Pull preferred units + timezone from profile text if present
    units_note = ""
    if "Preferred units: miles" in athlete_profile:
        units_note = "\nUse miles and min/mile for all distances and paces in your response.\n"
    elif "Preferred units: km" in athlete_profile or not athlete_profile:
        units_note = "\nUse kilometres and min/km for all distances and paces.\n"

    return f"""You are an expert endurance running coach specialising in training periodization,
heart rate variability, and load management.

You have real-time access to an athlete's training data. After each workout,
you analyse it and proactively adjust the upcoming plan if needed.

ATHLETE PROFILE:
{athlete_profile}
{units_note}{methodology_section}{mode_context}
YOUR JOB after each workout:
1. Analyse the completed workout — effort vs intent, HR response, pacing, TSS
2. Check wellness trends (HRV, resting HR, sleep) for recovery signals
3. Review upcoming planned sessions
4. Decide if adjustments are needed based on fatigue (ATL), form (TSB), and recovery

ADJUSTMENT RULES:
- TSB < -30: flag overreaching, reduce next 2-3 days intensity/volume
- TSB > +20 with race >2 weeks away: athlete may be undertraining
- HRV drop >20% vs 7-day average: recommend easy day regardless of plan
- Resting HR elevated >5bpm vs baseline: flag potential illness/overreach
- Always protect the long run and key quality sessions — adjust easier sessions first
- Never increase weekly TSS more than 10% week-over-week

Call submit_coaching_review exactly once with your complete analysis.
"""


def _build_user_message(context: dict) -> str:
    return f"""A workout just synced. Full context:

LATEST WORKOUT:
{json.dumps(context['latest_activity'], indent=2)}

CURRENT FITNESS:
{json.dumps(context['current_fitness'], indent=2)}

WELLNESS — last {WELLNESS_DAYS} days:
{json.dumps(context['wellness'], indent=2)}

ACTIVITIES — last {ACTIVITIES_DAYS} days (oldest first):
{json.dumps(context['recent_activities'], indent=2)}

PLANNED SESSIONS — next {PLANNED_DAYS} days:
{json.dumps(context['planned_workouts'], indent=2)}

Analyse and submit your coaching review via the tool."""


async def call_claude(client: httpx.AsyncClient, context: dict) -> dict:
    profile = load_athlete_profile()
    race_goal = load_race_goal()
    coaching_style = load_coaching_style()
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": MAX_TOKENS,
        "system": _build_system_prompt(profile, race_goal, coaching_style),
        "messages": [{"role": "user", "content": _build_user_message(context)}],
        "tools": [COACHING_TOOL],
        "tool_choice": {"type": "tool", "name": "submit_coaching_review"},
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }

    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            # Use longer timeout for Claude since coaching generation can be slow
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=httpx.Timeout(120.0, connect=10.0),
            )
            if r.status_code == 429 and attempt < MAX_RETRIES:
                log.warning("Claude API rate limited, retrying in %ds", RETRY_DELAY)
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                continue
            r.raise_for_status()
            return _extract_tool_input(r.json())
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                log.warning("Claude API error (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                continue
            raise

    raise RuntimeError(f"Claude API failed after retries: {last_exc}")


def _extract_tool_input(data: dict) -> dict:
    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "submit_coaching_review":
            return block.get("input", {})
    raise ValueError(
        f"Claude did not call submit_coaching_review. Stop reason: {data.get('stop_reason')}"
    )


# ---------------------------------------------------------------------------
# Apply adjustments
# ---------------------------------------------------------------------------
ALLOWED_FIELDS = frozenset({
    "name", "description", "moving_time", "icu_training_load", "start_date_local"
})


async def apply_adjustments(
    client: httpx.AsyncClient,
    adjustments: list,
    planned_workouts: list,
) -> list[str]:
    athlete = f"athlete/{ATHLETE_ID}"
    planned_ids = {e["id"] for e in planned_workouts}
    applied: list[str] = []

    for adj in adjustments:
        event_id = adj.get("event_id")
        action = adj.get("action", "keep")
        reason = adj.get("reason", "")

        if event_id not in planned_ids:
            log.warning("Skipping unknown event_id %s", event_id)
            continue

        try:
            if action == "modify":
                changes = adj.get("changes", {})
                safe = {k: v for k, v in changes.items() if k in ALLOWED_FIELDS}
                if not safe:
                    continue
                await icu_put(client, f"{athlete}/events/{event_id}", safe)
                label = safe.get("name", f"event {event_id}")
                applied.append(f"✏️ Modified: {label} — {reason}")
            elif action == "remove":
                await icu_delete(client, f"{athlete}/events/{event_id}")
                applied.append(f"🗑️ Removed event {event_id} — {reason}")
        except Exception as e:
            log.exception("Failed to update event %s", event_id)
            applied.append(f"⚠️ Failed to update event {event_id}: {e}")

    return applied


# ---------------------------------------------------------------------------
# HA notifications
# ---------------------------------------------------------------------------
async def _ha_post(client: httpx.AsyncClient, path: str, payload: dict) -> None:
    if not HA_TOKEN:
        return
    try:
        r = await client.post(
            f"{HA_URL}{path}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            json=payload,
        )
        if r.status_code >= 400:
            log.warning("HA POST %s failed: %s", path, r.status_code)
    except Exception as e:
        log.warning("HA POST %s error: %s", path, e)


async def ha_notify(client: httpx.AsyncClient, title: str, message: str,
                    tag: str = "claude_coach") -> None:
    await _ha_post(client, "/api/services/persistent_notification/create", {
        "title": title,
        "message": message,
        "notification_id": tag,
    })


async def ha_mobile_notify(client: httpx.AsyncClient, title: str,
                            message: str, alert_level: str = "green") -> None:
    if not HA_MOBILE_SERVICE:
        log.info("HA_MOBILE_SERVICE not configured, skipping mobile push")
        return

    color = {"green": "#2ecc71", "yellow": "#f39c12", "red": "#e74c3c"}.get(alert_level, "#2ecc71")
    await _ha_post(client, f"/api/services/notify/{HA_MOBILE_SERVICE}", {
        "title": title,
        "message": message[:1000],
        "data": {
            "color": color,
            "tag": "claude_coach",
            "group": "training",
            "url": "https://intervals.icu",
            "actions": [
                {"action": "URI", "title": "Open intervals.icu", "uri": "https://intervals.icu"},
            ],
        },
    })


# ---------------------------------------------------------------------------
# Main coaching flow — called by webhook auto-trigger and /coach endpoint
# ---------------------------------------------------------------------------
async def run_coaching_flow(
    activity_id: str,
    http_client: httpx.AsyncClient,
) -> dict:
    log.info("Coaching flow starting for activity_id=%r", activity_id)

    context = await fetch_context(http_client, activity_id)
    workout_name = (context.get("latest_activity") or {}).get("name", "your workout")
    log.info("Fetched context, calling Claude for: %s", workout_name)

    result = await call_claude(http_client, context)
    analysis = result.get("analysis", "").strip() or "No analysis returned."
    adjustments = result.get("adjustments", [])
    alert_level = result.get("alert_level", "green")
    alert_reason = result.get("alert_reason", "")

    applied: list[str] = []
    if adjustments:
        applied = await apply_adjustments(http_client, adjustments, context["planned_workouts"])
        log.info("Applied %d adjustment(s)", len(applied))

    alert_emoji = {"green": "✅", "yellow": "⚠️", "red": "🚨"}.get(alert_level, "✅")
    title = f"{alert_emoji} Coach review: {workout_name}"
    body = analysis
    if applied:
        body += "\n\n**Plan adjustments made:**\n" + "\n".join(applied)
    if alert_reason:
        body += f"\n\n_{alert_reason}_"

    await ha_notify(http_client, title, body)
    await ha_mobile_notify(http_client, title, analysis, alert_level)

    log.info("Coaching flow complete. Alert=%s, adjustments=%d", alert_level, len(applied))
    return {
        "status": "ok",
        "alert_level": alert_level,
        "adjustments_applied": len(applied),
    }
