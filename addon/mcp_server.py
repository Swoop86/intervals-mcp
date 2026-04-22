"""
Intervals.icu MCP Server — main entry point
Uses FastMCP (via mcp.server.fastmcp) for the /mcp endpoint and Starlette
for the webhook, coach, and health routes, all in a single ASGI app.
"""

from __future__ import annotations

import os
import json
import asyncio
import hmac
import ipaddress
import time
import logging

import jwt as pyjwt
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, PlainTextResponse
from starlette.routing import Mount, Route

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("intervals_mcp")

# ---------------------------------------------------------------------------
# Config — from environment (set by run.sh from HA addon options)
# ---------------------------------------------------------------------------
def _safe_int(val: str | None, default: int = 0) -> int:
    """Parse int from env var, handling HA's 'null' for unset optional fields."""
    if not val or val.strip().lower() in ("null", "none", ""):
        return default
    try:
        return int(val)
    except ValueError:
        return default

def _safe_str(val: str | None) -> str:
    """Return empty string if HA sends 'null' for unset optional fields."""
    if not val or val.strip().lower() in ("null", "none"):
        return ""
    return val.strip()

ATHLETE_ID             = _safe_str(os.environ.get("INTERVALS_ATHLETE_ID"))
API_KEY                = _safe_str(os.environ.get("INTERVALS_API_KEY"))
PORT                   = _safe_int(os.environ.get("INTERVALS_PORT"), 8765)
WEBHOOK_SECRET         = _safe_str(os.environ.get("INTERVALS_WEBHOOK_SECRET"))
WEBHOOK_HEADER_SECRET  = _safe_str(os.environ.get("INTERVALS_WEBHOOK_HEADER_SECRET"))
COACH_SECRET           = _safe_str(os.environ.get("COACH_SECRET"))
CF_TEAM_DOMAIN         = _safe_str(os.environ.get("CF_TEAM_DOMAIN"))
CF_ACCESS_AUD          = _safe_str(os.environ.get("CF_ACCESS_AUD"))
HA_TOKEN               = _safe_str(os.environ.get("HA_TOKEN"))
HA_URL                 = "http://supervisor/core"
BASE_URL               = "https://intervals.icu/api/v1"

# Tunables
HTTP_TIMEOUT      = httpx.Timeout(30.0, connect=10.0)
WEBHOOK_TOLERANCE = 300           # 5 min replay tolerance
MAX_BODY_BYTES    = 64 * 1024     # 64KB per request
RATE_LIMIT_RPS    = 5
RATE_BURST        = 10
RATE_BUCKET_TTL   = 3600

# ---------------------------------------------------------------------------
# Shared HTTP client
# ---------------------------------------------------------------------------
_httpx: Optional[httpx.AsyncClient] = None


def http() -> httpx.AsyncClient:
    global _httpx
    if _httpx is None or _httpx.is_closed:
        _httpx = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    return _httpx


# ---------------------------------------------------------------------------
# Rate limiter — token bucket per IP with TTL eviction
# ---------------------------------------------------------------------------
_rate_buckets: dict[str, dict] = defaultdict(
    lambda: {"tokens": float(RATE_BURST), "last": time.monotonic()}
)


def _prune_rate_buckets() -> None:
    cutoff = time.monotonic() - RATE_BUCKET_TTL
    stale = [ip for ip, b in _rate_buckets.items() if b["last"] < cutoff]
    for ip in stale:
        del _rate_buckets[ip]


def _check_rate_limit(ip: str) -> bool:
    now = time.monotonic()
    if len(_rate_buckets) > 10_000:
        _prune_rate_buckets()
    bucket = _rate_buckets[ip]
    elapsed = now - bucket["last"]
    bucket["tokens"] = min(RATE_BURST, bucket["tokens"] + elapsed * RATE_LIMIT_RPS)
    bucket["last"] = now
    if bucket["tokens"] >= 1:
        bucket["tokens"] -= 1
        return True
    return False


def _normalise_ip(raw: str) -> str:
    raw = raw.strip().strip("[]")  # remove surrounding brackets e.g. [::1]
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return raw  # not a valid IP literal — return as-is


def _get_ip(request: Request) -> str:
    raw = (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    return _normalise_ip(raw)


# ---------------------------------------------------------------------------
# Auth helpers — constant-time
# ---------------------------------------------------------------------------
def _safe_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _check_bearer(request: Request, secret: str) -> bool:
    if not secret:
        return True
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    return _safe_eq(auth[7:], secret)


def _check_header_token(request: Request, header: str, secret: str) -> bool:
    if not secret:
        return True
    return _safe_eq(request.headers.get(header.lower(), ""), secret)


# ---------------------------------------------------------------------------
# Cloudflare Access JWT validation
# ---------------------------------------------------------------------------
_cf_jwks: list[dict] = []
_cf_jwks_expires: float = 0.0
_JWKS_TTL = 3600.0


async def _ensure_cf_jwks() -> bool:
    """Fetch/refresh Cloudflare Access public keys. No-op if not configured."""
    global _cf_jwks, _cf_jwks_expires
    if not CF_TEAM_DOMAIN:
        return False
    if time.monotonic() < _cf_jwks_expires and _cf_jwks:
        return True
    url = f"https://{CF_TEAM_DOMAIN}/cdn-cgi/access/certs"
    try:
        r = await http().get(url, timeout=httpx.Timeout(10.0))
        r.raise_for_status()
        _cf_jwks = r.json().get("keys", [])
        _cf_jwks_expires = time.monotonic() + _JWKS_TTL
        log.info("CF Access JWKS loaded (%d keys)", len(_cf_jwks))
        return True
    except Exception as e:
        log.warning("CF Access JWKS fetch failed: %s", e)
        return False


def _validate_cf_jwt(token: str) -> bool:
    """Validate the CF-Access-Jwt-Assertion header against cached public keys."""
    if not CF_ACCESS_AUD:
        return True  # CF Access not configured — skip check
    if not token:
        return False
    if not _cf_jwks:
        log.warning("CF JWKS not loaded — rejecting /mcp request")
        return False
    for key_data in _cf_jwks:
        try:
            public_key = pyjwt.algorithms.RSAAlgorithm.from_jwk(key_data)
            pyjwt.decode(token, public_key, algorithms=["RS256"], audience=CF_ACCESS_AUD)
            return True
        except pyjwt.ExpiredSignatureError:
            log.warning("CF Access JWT expired")
            return False
        except pyjwt.InvalidAudienceError:
            log.warning("CF Access JWT wrong audience (expected %s)", CF_ACCESS_AUD)
            return False
        except pyjwt.DecodeError:
            continue  # try next key in the set
        except Exception:
            continue
    log.warning("CF Access JWT did not match any JWKS key")
    return False


# ---------------------------------------------------------------------------
# Intervals.icu API helpers
# ---------------------------------------------------------------------------
def _icu_auth() -> tuple[str, str]:
    return ("API_KEY", API_KEY)


async def icu_get(path: str, params: dict | None = None) -> Any:
    r = await http().get(f"{BASE_URL}/{path}", auth=_icu_auth(), params=params)
    r.raise_for_status()
    return r.json()


async def icu_post(path: str, payload: Any) -> Any:
    r = await http().post(f"{BASE_URL}/{path}", auth=_icu_auth(), json=payload)
    r.raise_for_status()
    return r.json()


async def icu_put(path: str, payload: Any) -> Any:
    r = await http().put(f"{BASE_URL}/{path}", auth=_icu_auth(), json=payload)
    r.raise_for_status()
    return r.json()


async def icu_delete(path: str) -> None:
    r = await http().delete(f"{BASE_URL}/{path}", auth=_icu_auth())
    r.raise_for_status()


# ---------------------------------------------------------------------------
# Home Assistant helpers
# ---------------------------------------------------------------------------
async def ha_notify(title: str, message: str, tag: str = "intervals_icu") -> None:
    if not HA_TOKEN:
        return
    try:
        r = await http().post(
            f"{HA_URL}/api/services/persistent_notification/create",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            json={"title": title, "message": message, "notification_id": tag},
        )
        if r.status_code >= 400:
            log.warning("ha_notify failed: %s", r.status_code)
    except Exception as e:
        log.warning("ha_notify error: %s", e)


async def ha_fire_event(event_type: str, data: dict) -> None:
    if not HA_TOKEN:
        return
    try:
        r = await http().post(
            f"{HA_URL}/api/events/{event_type}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            json=data,
        )
        if r.status_code >= 400:
            log.warning("ha_fire_event failed: %s", r.status_code)
    except Exception as e:
        log.warning("ha_fire_event error: %s", e)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------
def today_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def days_ago_iso(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# FastMCP server — tools defined as decorated functions
# ---------------------------------------------------------------------------
mcp = FastMCP("intervals-icu", stateless_http=True, json_response=True)


@mcp.tool()
async def get_activities(days: int = 14, oldest: str | None = None, newest: str | None = None) -> list[dict]:
    """Get completed workouts from intervals.icu with TSS, HR, pace/power, fitness impact.

    Args:
        days: Number of past days to fetch (default 14, max 365)
        oldest: Optional ISO date YYYY-MM-DD to override start date
        newest: Optional ISO date YYYY-MM-DD to override end date
    """
    params = {
        "oldest": oldest or days_ago_iso(days),
        "newest": newest or today_iso(),
    }
    data = await icu_get(f"athlete/{ATHLETE_ID}/activities", params=params)
    return [_summarise_activity(a) for a in data]


@mcp.tool()
async def get_wellness(days: int = 14) -> list[dict]:
    """Get wellness data: HRV, resting HR, sleep, weight, mood, motivation.

    Args:
        days: Number of past days to fetch (default 14, max 365)
    """
    return await icu_get(
        f"athlete/{ATHLETE_ID}/wellness",
        params={"oldest": days_ago_iso(days), "newest": today_iso()},
    )


@mcp.tool()
async def get_athlete() -> dict:
    """Get athlete profile: FTP, LTHR, weight, sport zones."""
    return await icu_get(f"athlete/{ATHLETE_ID}")


@mcp.tool()
async def get_fitness(days: int = 42) -> dict:
    """Get CTL (fitness), ATL (fatigue), TSB (form) history.

    Args:
        days: Days of history (default 42, max 365)
    """
    data = await icu_get(
        f"athlete/{ATHLETE_ID}/activities",
        params={"oldest": days_ago_iso(days), "newest": today_iso()},
    )
    history = [
        {
            "date": a.get("start_date_local", "")[:10],
            "name": a.get("name"),
            "type": a.get("type"),
            "tss": a.get("icu_training_load"),
            "ctl": a.get("icu_ctl"),
            "atl": a.get("icu_atl"),
            "tsb": a.get("icu_tsb"),
        }
        for a in data if a.get("icu_ctl") is not None
    ]
    history.reverse()  # intervals.icu returns newest first
    current = history[-1] if history else {}
    return {
        "current": {
            "ctl": current.get("ctl"),
            "atl": current.get("atl"),
            "tsb": current.get("tsb"),
        },
        "history": history,
    }


@mcp.tool()
async def get_planned_workouts(days_ahead: int = 14) -> list[dict]:
    """Get upcoming planned workouts from the calendar.

    Args:
        days_ahead: How many days ahead to look (default 14, max 90)
    """
    return await icu_get(
        f"athlete/{ATHLETE_ID}/events",
        params={
            "oldest": today_iso(),
            "newest": (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d"),
        },
    )


@mcp.tool()
async def create_workout(
    date: str,
    name: str,
    description: str,
    sport_type: str = "Run",
    moving_time: int | None = None,
    target_tss: float | None = None,
    workout_doc: dict | None = None,
) -> dict:
    """Create a planned workout on the calendar. Syncs to Garmin if connected.

    Args:
        date: ISO date YYYY-MM-DD
        name: Workout name
        description: Full workout description with structure and targets
        sport_type: Run, Ride, Swim, etc. (default Run)
        moving_time: Estimated duration in seconds
        target_tss: Target Training Stress Score
        workout_doc: Structured workout in intervals.icu format
    """
    payload = {
        "start_date_local": date,
        "name": name,
        "type": sport_type,
        "description": description,
    }
    if moving_time is not None:
        payload["moving_time"] = moving_time
    if target_tss is not None:
        payload["icu_training_load"] = target_tss
    if workout_doc is not None:
        payload["workout_doc"] = workout_doc
    return await icu_post(f"athlete/{ATHLETE_ID}/events", payload)


@mcp.tool()
async def get_activity_detail(activity_id: str) -> dict:
    """Get full detail for a specific activity.

    Args:
        activity_id: Activity ID e.g. 'i12345678'
    """
    return await icu_get(f"activity/{activity_id}")


@mcp.tool()
async def review_training(activity_id: str = "") -> dict:
    """Fetch comprehensive training context to perform a coaching review.

    Returns the last 28 days of activities, 14 days of wellness (HRV, resting HR,
    sleep, weight), current CTL/ATL/TSB fitness metrics, and upcoming planned
    workouts — everything needed to analyse training load, recovery status, and
    plan adjustments.

    Call this when the user asks to:
    - Review their training / recent workouts
    - Check how they are recovering
    - Analyse fitness trends (CTL, ATL, form)
    - Decide whether to adjust the plan

    After calling this tool, provide a coaching review covering:
    1. Latest workout analysis (effort vs intent, HR response, pacing)
    2. Recovery status using the Recovery Index (Section 11 framework, github.com/CrankAddict/section-11):
       RI = (HRV_today/HRV_baseline) / (RHR_today/RHR_baseline)
       - RI ≥ 0.8 = Green, RI 0.7–0.79 = Amber, RI < 0.6 = Red
    3. Training load assessment (ACWR):
       ACWR = 7-day TSS / 28-day average TSS
       - Safe range 0.8–1.3; flag ≥1.3; alarm ≥1.5
    4. Current training phase (Base/Build/Peak/Taper/Deload/Overreached)
    5. Specific plan adjustments if needed (use update_workout / delete_workout)

    Args:
        activity_id: Optional specific activity ID to focus on (e.g. 'i12345678').
                     Leave empty to use the most recent activity.
    """
    from claude_coach import fetch_context
    return await fetch_context(http(), activity_id)


@mcp.tool()
async def update_workout(
    event_id: int,
    name: str | None = None,
    description: str | None = None,
    moving_time: int | None = None,
    target_tss: float | None = None,
    date: str | None = None,
) -> dict:
    """Modify a planned workout on the calendar.

    Only fields you provide are updated. Syncs to Garmin if connected.

    Args:
        event_id: Calendar event ID (from get_planned_workouts)
        name: New workout name
        description: New description with structure/targets
        moving_time: New estimated duration in seconds
        target_tss: New target Training Stress Score
        date: New date as ISO YYYY-MM-DD (reschedule)
    """
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if moving_time is not None:
        payload["moving_time"] = moving_time
    if target_tss is not None:
        payload["icu_training_load"] = target_tss
    if date is not None:
        payload["start_date_local"] = date
    if not payload:
        return {"error": "No fields provided to update"}
    return await icu_put(f"athlete/{ATHLETE_ID}/events/{event_id}", payload)


@mcp.tool()
async def delete_workout(event_id: int) -> dict:
    """Delete a planned workout from the calendar.

    Use when removing a session is more appropriate than modifying it
    (e.g. extra rest day needed, duplicate entry, race replaces workout).

    Args:
        event_id: Calendar event ID (from get_planned_workouts)
    """
    await icu_delete(f"athlete/{ATHLETE_ID}/events/{event_id}")
    return {"status": "deleted", "event_id": event_id}


def _summarise_activity(a: dict) -> dict:
    return {
        "id": a.get("id"),
        "date": a.get("start_date_local", "")[:10],
        "name": a.get("name"),
        "type": a.get("type"),
        "duration_min": round(a.get("moving_time", 0) / 60, 1),
        "distance_km": round((a.get("distance", 0) or 0) / 1000, 2),
        "tss": a.get("icu_training_load"),
        "avg_hr": a.get("average_heartrate"),
        "avg_pace_per_km": a.get("icu_average_speed"),
        "avg_power": a.get("average_watts"),
        "elevation_m": a.get("total_elevation_gain"),
        "ctl": a.get("icu_ctl"),
        "atl": a.get("icu_atl"),
        "tsb": a.get("icu_tsb"),
    }


# ---------------------------------------------------------------------------
# MCP auth + rate limit middleware — applied only to /mcp path
# ---------------------------------------------------------------------------
class MCPAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/mcp"):
            return await call_next(request)

        ip = _get_ip(request)
        if not _check_rate_limit(ip):
            return PlainTextResponse("Too Many Requests", status_code=429)

        # Refresh JWKS if needed (no-op when CF_TEAM_DOMAIN is not configured)
        await _ensure_cf_jwks()
        token = request.headers.get("cf-access-jwt-assertion", "")
        if not _validate_cf_jwt(token):
            log.warning("CF Access JWT missing/invalid on /mcp from %s", ip)
            return PlainTextResponse("Unauthorized", status_code=401)

        return await call_next(request)


# ---------------------------------------------------------------------------
# Coaching trigger with dedupe
# ---------------------------------------------------------------------------
_coaching_lock = asyncio.Lock()
_last_coached_id: Optional[str] = None


async def _run_coaching_for(activity_id: str) -> None:
    global _last_coached_id
    async with _coaching_lock:
        if activity_id and activity_id == _last_coached_id:
            log.info("Skipping duplicate coaching for %s", activity_id)
            return
        _last_coached_id = activity_id
    try:
        from claude_coach import run_coaching_flow
        await run_coaching_flow(activity_id, http_client=http())
    except Exception as e:
        log.exception("Auto-coaching failed for %s", activity_id)
        await ha_notify("⚠️ Coach error", str(e), tag="claude_coach_error")


# ---------------------------------------------------------------------------
# Webhook replay protection
# ---------------------------------------------------------------------------
_seen_event_ids: dict[str, float] = {}


def _is_replay(event_id: str, timestamp_str: str) -> bool:
    now = time.time()
    stale = [eid for eid, ts in _seen_event_ids.items() if now - ts > WEBHOOK_TOLERANCE * 2]
    for eid in stale:
        del _seen_event_ids[eid]
    if timestamp_str:
        try:
            ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00")).timestamp()
            if abs(now - ts) > WEBHOOK_TOLERANCE:
                log.warning("Webhook timestamp out of tolerance: %s", timestamp_str)
                return True
        except Exception:
            pass
    if event_id and event_id in _seen_event_ids:
        return True
    if event_id:
        _seen_event_ids[event_id] = now
    return False


# ---------------------------------------------------------------------------
# Webhook handler
# ---------------------------------------------------------------------------
async def handle_webhook(request: Request) -> Response:
    ip = _get_ip(request)
    if not _check_rate_limit(ip):
        return PlainTextResponse("Too Many Requests", status_code=429)

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return PlainTextResponse("Payload Too Large", status_code=413)

    # Custom authorization header (set in intervals.icu webhook config)
    if WEBHOOK_HEADER_SECRET:
        incoming = request.headers.get("x-webhook-auth", "")
        if not _safe_eq(incoming, WEBHOOK_HEADER_SECRET):
            log.warning("Webhook header auth failed from %s", ip)
            return PlainTextResponse("OK", status_code=200)  # silent reject

    try:
        data = json.loads(body)
    except Exception:
        return PlainTextResponse("Bad JSON", status_code=400)

    if WEBHOOK_SECRET:
        if not _safe_eq(data.get("secret", ""), WEBHOOK_SECRET):
            log.warning("Webhook secret mismatch from %s", ip)
            return PlainTextResponse("OK", status_code=200)

    events = data.get("events", [])
    for event in events:
        event_type = event.get("type", "UNKNOWN")
        event_ts = event.get("timestamp", "")
        activity = event.get("activity", {})
        activity_id = activity.get("id", "")

        dedupe_key = f"{event_type}:{activity_id}:{event_ts}"
        if _is_replay(dedupe_key, event_ts):
            log.info("Dropping replay: %s", dedupe_key)
            continue

        log.info("Webhook received: %s from %s", event_type, ip)

        if event_type == "ACTIVITY_UPLOADED":
            await _handle_activity_uploaded(activity)
        elif event_type == "ACTIVITY_ANALYZED":
            await _handle_activity_analyzed(activity)
        elif event_type == "CALENDAR_UPDATED":
            await _handle_calendar_updated(event.get("events", []))

    return PlainTextResponse("OK", status_code=200)


async def _handle_activity_uploaded(activity: dict) -> None:
    name = activity.get("name", "Unknown workout")
    sport = activity.get("type", "")
    duration_min = round(activity.get("moving_time", 0) / 60, 1)
    distance_km = round((activity.get("distance", 0) or 0) / 1000, 2)
    tss = activity.get("icu_training_load", "?")
    msg = (
        f"**{sport}: {name}**\n"
        f"Duration: {duration_min} min | Distance: {distance_km} km | TSS: {tss}\n\n"
        f"🤖 Claude will review once analysis is ready..."
    )
    await ha_notify("🏃 New workout synced", msg, tag="intervals_activity")
    await ha_fire_event(
        "intervals_icu_activity_uploaded",
        {
            "activity_id": activity.get("id"),
            "name": name, "type": sport,
            "duration_min": duration_min, "distance_km": distance_km, "tss": tss,
        },
    )


async def _handle_activity_analyzed(activity: dict) -> None:
    name = activity.get("name", "workout")
    activity_id = activity.get("id", "")
    ctl = activity.get("icu_ctl")
    atl = activity.get("icu_atl")
    tsb = activity.get("icu_tsb")
    if ctl is None:
        return
    form_emoji = "✅" if tsb and tsb > -10 else "⚠️"
    msg = (
        f"Analysis ready for **{name}**\n"
        f"Fitness (CTL): {ctl:.1f} | Fatigue (ATL): {atl:.1f} | Form (TSB): {tsb:.1f} {form_emoji}\n\n"
        f"🤖 Claude is reviewing your training now..."
    )
    await ha_notify("📊 Workout analysed", msg, tag="intervals_analysis")
    await ha_fire_event(
        "intervals_icu_activity_analyzed",
        {"activity_id": activity_id, "name": name, "ctl": ctl, "atl": atl, "tsb": tsb},
    )
    task = asyncio.create_task(_run_coaching_for(activity_id))
    task.add_done_callback(
        lambda t: log.error("Coaching task raised: %s", t.exception()) if t.exception() else None
    )


async def _handle_calendar_updated(updated: list[dict]) -> None:
    if not updated:
        return
    names = [e.get("name", "Workout") for e in updated[:3]]
    await ha_notify(
        "📅 Training plan updated",
        f"Calendar updated: {', '.join(names)}",
        tag="intervals_calendar",
    )
    await ha_fire_event(
        "intervals_icu_calendar_updated",
        {
            "count": len(updated),
            "workouts": [
                {"name": e.get("name"), "date": e.get("start_date_local", "")[:10]}
                for e in updated[:5]
            ],
        },
    )


# ---------------------------------------------------------------------------
# Coach handler
# ---------------------------------------------------------------------------
async def handle_coach(request: Request) -> Response:
    ip = _get_ip(request)
    if not _check_rate_limit(ip):
        return PlainTextResponse("Too Many Requests", status_code=429)
    if not _check_header_token(request, "X-Coach-Token", COACH_SECRET):
        log.warning("Unauthorized /coach from %s", ip)
        return PlainTextResponse("Unauthorized", status_code=401)

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return PlainTextResponse("Payload Too Large", status_code=413)

    try:
        data = json.loads(body) if body else {}
    except Exception:
        data = {}

    activity_id = data.get("activity_id", "")
    if not isinstance(activity_id, str) or len(activity_id) > 50:
        return JSONResponse({"status": "error", "message": "Invalid activity_id"}, status_code=400)

    try:
        from claude_coach import run_coaching_flow
        result = await run_coaching_flow(activity_id, http_client=http())
        return JSONResponse(result)
    except Exception as e:
        log.exception("Coach request failed")
        await ha_notify("⚠️ Coach error", str(e), tag="claude_coach_error")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Health handler
# ---------------------------------------------------------------------------
async def handle_health(request: Request) -> Response:
    return JSONResponse({
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "auth": {
            "mcp_cf_access": bool(CF_ACCESS_AUD),
            "coach": bool(COACH_SECRET),
            "webhook": bool(WEBHOOK_SECRET),
            "webhook_header": bool(WEBHOOK_HEADER_SECRET),
        },
    })


# ---------------------------------------------------------------------------
# Lifespan — manage FastMCP session manager + shared httpx client
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: Starlette):
    async with mcp_app.router.lifespan_context(app):
        if CF_TEAM_DOMAIN:
            await _ensure_cf_jwks()
        try:
            yield
        finally:
            global _httpx
            if _httpx and not _httpx.is_closed:
                await _httpx.aclose()


# ---------------------------------------------------------------------------
# Build the ASGI app
# ---------------------------------------------------------------------------
if not ATHLETE_ID or not API_KEY:
    log.error("athlete_id and api_key are required — check addon config")
    raise SystemExit(1)

if not CF_ACCESS_AUD:
    log.warning("CF_ACCESS_AUD not set — /mcp endpoint has no Cloudflare Access validation!")
if not COACH_SECRET:
    log.warning("COACH_SECRET not set — /coach endpoint is unauthenticated!")
if not WEBHOOK_SECRET:
    log.warning("WEBHOOK_SECRET not set — /webhook accepts unsigned payloads!")

# FastMCP generates a Starlette app with a single route at /mcp
mcp_app = mcp.streamable_http_app()

app = Starlette(
    routes=[
        # Our routes MUST come before the Mount catch-all
        Route("/webhook", handle_webhook, methods=["POST"]),
        Route("/coach", handle_coach, methods=["POST"]),
        Route("/health", handle_health, methods=["GET"]),
        # Mount FastMCP last — it handles /mcp
        Mount("/", app=mcp_app),
    ],
    middleware=[Middleware(MCPAuthMiddleware)],
    lifespan=lifespan,
)


if __name__ == "__main__":
    log.info("MCP endpoint:     http://[::]:%d/mcp  (CF Access: %s)", PORT, "yes" if CF_ACCESS_AUD else "NO")
    log.info("Webhook receiver: http://[::]:%d/webhook  (secret: %s)", PORT, "yes" if WEBHOOK_SECRET else "NO")
    log.info("Coach endpoint:   http://[::]:%d/coach  (auth: %s)", PORT, "yes" if COACH_SECRET else "NO")
    log.info("Health endpoint:  http://[::]:%d/health", PORT)
    log.info("Athlete ID: %s", ATHLETE_ID)
    uvicorn.run(app, host="::", port=PORT, log_config=None)
