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
from datetime import datetime, timedelta
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


async def icu_get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> Any:
    r = await client.get(f"{BASE_URL}/{path}", auth=_auth(), params=params)
    r.raise_for_status()
    return r.json()


async def icu_put(client: httpx.AsyncClient, path: str, payload: dict) -> Any:
    r = await client.put(f"{BASE_URL}/{path}", auth=_auth(), json=payload)
    r.raise_for_status()
    return r.json()


async def icu_delete(client: httpx.AsyncClient, path: str) -> None:
    r = await client.delete(f"{BASE_URL}/{path}", auth=_auth())
    r.raise_for_status()


# ---------------------------------------------------------------------------
# Context fetching
# ---------------------------------------------------------------------------
async def fetch_context(client: httpx.AsyncClient, activity_id: str) -> dict:
    athlete = f"athlete/{ATHLETE_ID}"

    activities, wellness, planned = await asyncio.gather(
        icu_get(client, f"{athlete}/activities",
                {"oldest": days_ago_iso(ACTIVITIES_DAYS), "newest": today_iso()}),
        icu_get(client, f"{athlete}/wellness",
                {"oldest": days_ago_iso(WELLNESS_DAYS), "newest": today_iso()}),
        icu_get(client, f"{athlete}/events",
                {"oldest": today_iso(), "newest": in_days_iso(PLANNED_DAYS)}),
    )

    activities_sorted = sorted(activities, key=lambda a: a.get("start_date_local", ""))

    latest = None
    if activity_id:
        latest = next((a for a in activities if a.get("id") == activity_id), None)
    if not latest and activities_sorted:
        latest = activities_sorted[-1]

    return {
        "latest_activity": _clean_activity(latest) if latest else None,
        "recent_activities": [_clean_activity(a) for a in activities_sorted],
        "wellness": [_clean_wellness(w) for w in wellness],
        "planned_workouts": [
            _clean_planned(e) for e in planned if e.get("type") != "Note"
        ],
        "current_fitness": {
            "ctl": latest.get("icu_ctl") if latest else None,
            "atl": latest.get("icu_atl") if latest else None,
            "tsb": latest.get("icu_tsb") if latest else None,
        },
    }


def _clean_activity(a: dict) -> dict:
    return {
        "id": a.get("id"),
        "date": a.get("start_date_local", "")[:10],
        "name": a.get("name"),
        "type": a.get("type"),
        "duration_min": round(a.get("moving_time", 0) / 60, 1),
        "distance_km": round((a.get("distance", 0) or 0) / 1000, 2),
        "tss": a.get("icu_training_load"),
        "avg_hr": a.get("average_heartrate"),
        "avg_power": a.get("average_watts"),
        "ctl": a.get("icu_ctl"),
        "atl": a.get("icu_atl"),
        "tsb": a.get("icu_tsb"),
        "perceived_effort": a.get("perceived_exertion"),
    }


def _clean_wellness(w: dict) -> dict:
    return {
        "date": w.get("id"),
        "hrv": w.get("hrv"),
        "resting_hr": w.get("restingHR"),
        "sleep_hours": round(w["sleepSecs"] / 3600, 1) if w.get("sleepSecs") else None,
        "weight_kg": w.get("weight"),
        "mood": w.get("mood"),
        "motivation": w.get("motivation"),
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

    return f"""You are an expert endurance running coach specialising in training periodization,
heart rate variability, and load management.

You have real-time access to an athlete's training data. After each workout,
you analyse it and proactively adjust the upcoming plan if needed.

ATHLETE PROFILE:
{athlete_profile}
{methodology_section}{mode_context}
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
