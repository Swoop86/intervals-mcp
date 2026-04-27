"""
Intervals.icu MCP Server — main entry point
Uses FastMCP (via mcp.server.fastmcp) for the /mcp endpoint and Starlette
for the webhook, coach, and health routes, all in a single ASGI app.
"""

from __future__ import annotations

import html
import os
import re
import json
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import secrets
import time
import logging

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, PlainTextResponse
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

# Suppress noisy HA Supervisor watchdog health-check pings from access logs
class _HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "GET /health" not in record.getMessage()

logging.getLogger("uvicorn.access").addFilter(_HealthCheckFilter())

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

ATHLETE_ID     = _safe_str(os.environ.get("INTERVALS_ATHLETE_ID"))
API_KEY        = _safe_str(os.environ.get("INTERVALS_API_KEY"))
PORT           = _safe_int(os.environ.get("INTERVALS_PORT"), 8765)
WEBHOOK_SECRET = _safe_str(os.environ.get("INTERVALS_WEBHOOK_SECRET"))
COACH_SECRET   = _safe_str(os.environ.get("COACH_SECRET"))
READ_ONLY      = os.environ.get("READ_ONLY", "").lower() in ("true", "1", "yes")
HA_TOKEN       = _safe_str(os.environ.get("HA_TOKEN"))
HA_URL         = "http://supervisor/core"
BASE_URL       = "https://intervals.icu/api/v1"

# Tunables
HTTP_TIMEOUT      = httpx.Timeout(30.0, connect=10.0)
WEBHOOK_TOLERANCE = 300        # 5 min replay tolerance
MAX_BODY_BYTES    = 64 * 1024  # 64 KB
RATE_LIMIT_RPS    = 5
RATE_BURST        = 10
RATE_BUCKET_TTL   = 3600

# ---------------------------------------------------------------------------
# Athlete profile + race goal — optional Fernet-encrypted JSON on /data
# ---------------------------------------------------------------------------
try:
    from cryptography.fernet import Fernet as _Fernet, InvalidToken as _FernetInvalidToken
    _CRYPTO_AVAILABLE = True
except ImportError:
    _Fernet = None
    _FernetInvalidToken = Exception
    _CRYPTO_AVAILABLE = False

_PROFILE_PATH = "/data/athlete_profile.json"
_GOAL_PATH    = "/data/athlete_goal.json"

_DEFAULT_PROFILE: dict = {
    "sport": "running",
    "age": None,
    "location": "",
    "timezone": "",
    "preferred_units": "km",
    "training_days_per_week": None,
    "easy_pace_min_per_km": None,
    "threshold_pace_min_per_km": None,
    "weekly_volume_km": None,
    "known_limiters": [],
    "notes": "",
    "coaching_methodology": "",
    "coaching_description": "",
}

# Built-in coaching methodology presets: slug → (display_name, coaching_instructions)
_METHODOLOGY_PRESETS: dict[str, tuple[str, str]] = {
    "polarized": (
        "Polarized (80/20)",
        """Polarized training: 80% of sessions at easy/conversational effort (Zone 1, <75% HRmax, RPE ≤3), 20% at hard effort (Zone 3, >87% HRmax, RPE ≥7). Actively avoid the 'moderate/threshold' middle zone — it creates fatigue without sufficient adaptation stimulus.

Key sessions: long easy runs, fartlek (unstructured speed play by feel), 4–6×4–8min VO2max intervals, strides.
Weekly structure: 4–5 easy sessions, 1–2 quality sessions maximum.
TSS should come primarily from volume, not intensity.
Flag any session where HR drifted into Zone 2 (75–87% HRmax) as a polarization leak — recommend keeping future efforts either easier or harder.
Prescribe VO2max work in this ratio: for every 5 easy sessions, allow 1 hard session.""",
    ),
    "maffetone": (
        "Maffetone / MAF",
        """Maffetone Method: Build aerobic base exclusively below MAF heart rate (default 180 − age, ±5 for health/training history).

No intervals, tempo runs, or race efforts until aerobic base is established (typically 3–6 months minimum).
Monitor pace at MAF HR over weeks — it must improve (get faster) as aerobic capacity develops. Stagnation means more base work is needed.
Flag any session exceeding MAF HR as a methodology violation — recommend slowing to stay aerobic.
MAF HR formula adjustments:
  − Subtract 5 if injury-prone, sick frequently, or inconsistent training history
  + 5 if training >2 years with no injuries and recent progress
Progress to intensity ONLY once MAF pace has clearly plateaued AND base is solid (typically 6+ months).""",
    ),
    "jack_daniels": (
        "Jack Daniels VDOT",
        """Jack Daniels VDOT system: Derive all training paces from the athlete's most recent race performance using their VDOT score.

Five training types (use current VDOT to look up paces):
  E  — Easy (59–74% VO2max): conversational pace, all recovery and base runs
  M  — Marathon pace (75–84% VO2max): goal marathon effort
  T  — Threshold/Tempo (83–88% VO2max): 'comfortably hard', 20–40min continuous or cruise intervals
  I  — Intervals (97–100% VO2max): 3–5min reps, full recovery between
  R  — Repetitions (105–120% VO2max): short fast reps (200–400m), full recovery

Weekly quality work limits: T pace ≤10% of weekly volume; I/R work 1 session/week max.
E pace must dominate weekly volume.
Update VDOT and paces after each race result.
Quality session examples: 6×1km at T pace (cruise intervals), 5×3min at I pace, 10×200m at R pace.""",
    ),
    "norwegian": (
        "Norwegian Double Threshold",
        """Norwegian Double Threshold method: Two threshold sessions per week at low-lactate accumulation effort (~75–80% HRmax, RPE 5–6, equivalent to ~2–2.5 mmol/L blood lactate). All other sessions fully easy.

Threshold session structure: 8–12×1km at threshold pace (2–3min rest), or 4–6×2km (3–4min rest), or 30–40min continuous at threshold. Sessions feel controlled — never hard.
High total weekly volume at easy pace (~80% of sessions).
Do NOT push into VO2max territory in threshold sessions — controlled aerobic accumulation is the mechanism. Flag sessions where the athlete reported pushing too hard.
Adapted without lactate monitor: threshold pace = pace sustainable for ~60min in a race, HR 75–80% max.
VO2max sessions rarely used (maybe once per month near competition).""",
    ),
    "pyramidal": (
        "Pyramidal",
        """Pyramidal intensity distribution: most volume easy, significant threshold, limited high intensity.

Approximate zone distribution:
  ~70% Zone 1–2 (easy, <75% HRmax): base runs, recovery, long runs
  ~20% Zone 3 (threshold, 75–87% HRmax): tempo runs, marathon pace, sustained efforts
  ~10% Zone 4–5 (hard, >87% HRmax): VO2max intervals, race-pace sessions

Key sessions: 20–40min tempo runs, 4–6×1km cruise intervals, one hard quality session/week.
More traditional and intuitive than polarized — allows sustained threshold efforts.
Good for recreational runners and those who find purely easy/hard (polarized) difficult to sustain.
Flag if the athlete is doing more than one hard session and one threshold session in the same week.""",
    ),
}


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
            log.warning("Could not decrypt %s — key mismatch?", path)
            return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _write_json_file(path: str, data: dict) -> None:
    raw = json.dumps(data, indent=2).encode()
    f = _fernet()
    if f:
        raw = f.encrypt(raw)
    with open(path, "wb") as fh:
        fh.write(raw)


def _load_profile() -> dict:
    return _read_json_file(_PROFILE_PATH) or dict(_DEFAULT_PROFILE)


def _load_goal() -> dict | None:
    return _read_json_file(_GOAL_PATH)


def _clear_goal() -> None:
    try:
        os.remove(_GOAL_PATH)
    except FileNotFoundError:
        pass


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
from collections import defaultdict

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
    raw = raw.strip().strip("[]")
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return raw


def _get_ip(request: Request) -> str:
    raw = (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    return _normalise_ip(raw)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _safe_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _check_header_token(request: Request, header: str, secret: str) -> bool:
    if not secret:
        return True
    return _safe_eq(request.headers.get(header.lower(), ""), secret)


# ---------------------------------------------------------------------------
# Intervals.icu API helpers
# ---------------------------------------------------------------------------
def _icu_auth() -> tuple[str, str]:
    return ("API_KEY", API_KEY)


def _icu_raise(r: httpx.Response) -> None:
    """Raise HTTPStatusError with the response body included so Claude sees the detail."""
    if r.is_error:
        body = r.text[:800]
        log.error("intervals.icu %s %s → %d: %s", r.request.method, r.request.url, r.status_code, body)
        raise httpx.HTTPStatusError(
            f"HTTP {r.status_code} from intervals.icu: {body}",
            request=r.request,
            response=r,
        )


async def icu_get(path: str, params: dict | None = None) -> Any:
    r = await http().get(f"{BASE_URL}/{path}", auth=_icu_auth(), params=params)
    _icu_raise(r)
    return r.json()


async def icu_post(path: str, payload: Any) -> Any:
    r = await http().post(f"{BASE_URL}/{path}", auth=_icu_auth(), json=payload)
    _icu_raise(r)
    return r.json()


async def icu_put(path: str, payload: Any, params: dict | None = None) -> Any:
    r = await http().put(f"{BASE_URL}/{path}", auth=_icu_auth(), json=payload, params=params)
    _icu_raise(r)
    return r.json()


async def icu_delete(path: str) -> None:
    r = await http().delete(f"{BASE_URL}/{path}", auth=_icu_auth())
    _icu_raise(r)


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

    Returns one entry per day with all available fields:
      hrv             Raw HRV value (ms)
      hrv_score       HRV4Training readiness score (0–10) if connected
      resting_hr      Resting heart rate (bpm)
      sleep_hours     Total sleep duration
      sleep_quality   Subjective quality rating (1–5)
      sleep_score     Device sleep score if available
      spo2            Blood oxygen saturation (%)
      mood / motivation / soreness / fatigue  Subjective 1–5 ratings

    For readiness analysis (HRV z-score, HRV-CV, ACWR, Recovery Index),
    use review_training which pre-computes these in readiness_metrics.

    Args:
        days: Number of past days to fetch (default 14, max 365)
    """
    raw = await icu_get(
        f"athlete/{ATHLETE_ID}/wellness",
        params={"oldest": days_ago_iso(days), "newest": today_iso()},
    )
    return [
        {
            "date": w.get("id"),
            "hrv": w.get("hrv"),
            "hrv_score": w.get("hrvScore"),
            "resting_hr": w.get("restingHR"),
            "sleep_hours": round(w["sleepSecs"] / 3600, 1) if w.get("sleepSecs") else None,
            "sleep_quality": w.get("sleepQuality"),
            "sleep_score": w.get("sleepScore"),
            "spo2": w.get("spO2"),
            "weight_kg": w.get("weight"),
            "mood": w.get("mood"),
            "motivation": w.get("motivation"),
            "soreness": w.get("soreness"),
            "fatigue": w.get("fatigue"),
            "stress": w.get("stress"),
        }
        for w in raw
    ]


def _ms_to_min_per_km(speed_ms: float) -> float | None:
    """Convert m/s to min/km, return None if speed is zero/invalid."""
    if not speed_ms or speed_ms <= 0:
        return None
    return round(1000 / (speed_ms * 60), 2)


def _ms_to_min_per_100m(speed_ms: float) -> float | None:
    """Convert m/s to min/100m for swimming, return None if speed is zero/invalid."""
    if not speed_ms or speed_ms <= 0:
        return None
    return round(100 / (speed_ms * 60), 2)


def _min_km_to_ms(min_per_km: float) -> float:
    """Convert min/km to m/s for the sport-settings API."""
    return round(1000 / (min_per_km * 60), 6)


def _format_pace(min_per_km: float) -> str:
    """Format decimal min/km as mm:ss/km string (e.g. 5.555 → '5:33/km')."""
    minutes = int(min_per_km)
    seconds = round((min_per_km - minutes) * 60)
    if seconds == 60:
        minutes += 1
        seconds = 0
    return f"{minutes}:{seconds:02d}/km"


def _extract_sport_zones(data: dict) -> None:
    """Extract per-sport coaching fields from sportSettings and add them to data."""
    sport_settings = data.get("sportSettings") or []
    for ss in sport_settings:
        sport = ss.get("activity_type")
        if not sport:
            continue

        prefix = sport.lower()  # "run", "ride", "swim", etc.

        # LTHR per sport
        if ss.get("lthr"):
            data[f"{prefix}_lthr_bpm"] = ss["lthr"]

        # Max HR per sport
        if ss.get("max_heart_rate"):
            data[f"{prefix}_max_hr_bpm"] = ss["max_heart_rate"]

        # HR zone boundaries (bpm) — same for all sports
        hr_zones = ss.get("zones_heart_rate") or []
        if hr_zones:
            data[f"{prefix}_hr_zones_bpm"] = hr_zones

        if sport == "Run":
            tp = ss.get("threshold_pace")
            if tp:
                data["running_threshold_pace_min_per_km"] = _ms_to_min_per_km(tp)
            pz = ss.get("pace_zones") or []
            if pz:
                data["running_pace_zones_min_per_km"] = [
                    _ms_to_min_per_km(v) for v in pz if v
                ]

        elif sport == "Ride":
            # Prefer sport-specific FTP; fall back to threshold_power
            ride_ftp = ss.get("ftp") or ss.get("threshold_power")
            if ride_ftp:
                data["cycling_ftp_watts"] = ride_ftp
            pwr_zones = ss.get("zones_power") or []
            if pwr_zones:
                data["cycling_power_zones_watts"] = pwr_zones

        elif sport == "Swim":
            # CSS (Critical Swim Speed) stored as threshold_pace in m/s
            css = ss.get("threshold_pace")
            if css:
                data["swim_css_min_per_100m"] = _ms_to_min_per_100m(css)


@mcp.tool()
async def get_athlete() -> dict:
    """Get athlete data from intervals.icu: FTP, LTHR, weight, and per-sport zones
    synced from Garmin or configured in the intervals.icu GUI.

    Post-processes sportSettings to surface coaching values at the top level:

    Running:
      running_threshold_pace_min_per_km  threshold pace synced from Garmin
      running_pace_zones_min_per_km      all pace zone boundaries
      run_lthr_bpm                       running-specific LTHR
      run_max_hr_bpm                     running max HR
      run_hr_zones_bpm                   running HR zone boundaries

    Cycling:
      cycling_ftp_watts                  cycling FTP
      cycling_power_zones_watts          power zone boundaries
      ride_lthr_bpm                      cycling LTHR
      ride_max_hr_bpm                    cycling max HR
      ride_hr_zones_bpm                  cycling HR zone boundaries

    Swimming:
      swim_css_min_per_100m              critical swim speed
      swim_lthr_bpm / swim_hr_zones_bpm  swimming HR zones

    Use running_threshold_pace_min_per_km for Pace targets in workout descriptions.
    Use run_lthr_bpm (or top-level lthr) for LTHR% targets.
    Prefer these intervals.icu values (Garmin-synced) over get_profile fallbacks.
    """
    data = await icu_get(f"athlete/{ATHLETE_ID}")
    _extract_sport_zones(data)
    return data


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


def _normalise_date(date: str) -> str:
    """Ensure start_date_local includes a time component as required by intervals.icu."""
    return date if "T" in date else f"{date}T00:00:00"


if not READ_ONLY:
    @mcp.tool()
    async def create_workout(
        date: str,
        name: str,
        description: str,
        sport_type: str = "Run",
        category: str = "WORKOUT",
        moving_time: int | None = None,
        distance_km: float | None = None,
        target_tss: float | None = None,
    ) -> dict:
        """Create a planned workout on the calendar. Syncs to Garmin if connected.

        HOW GARMIN NATIVE WORKOUTS WORK
        intervals.icu parses the description text and generates a structured workout
        that syncs step-by-step to the Garmin watch (like Garmin Coach) — with HR
        alerts, pace zones, and rep countdowns per step. The description IS the
        structured workout. Use the syntax below.

        DESCRIPTION SYNTAX — always use this structured format.
        intervals.icu parses it and creates step-by-step Garmin guidance.

        BEFORE WRITING ANY TARGETS — call get_athlete first (required)
        ─────────────────────────────────────────
        get_athlete → running_threshold_pace_min_per_km, run_lthr_bpm,
                      running_pace_zones_min_per_km (athlete's configured zones, if set)
        get_profile → fallback if get_athlete returns nothing

        COMPUTING PACE TARGETS (quality sessions: tempo, threshold, VO2max, strides)
        ─────────────────────────────────────────
        ALWAYS use absolute pace — "% Pace" or "Z2 Pace" silently produces "run until
        press lap" on Garmin when pace zones are not fully configured.

        If get_athlete returns running_pace_zones_min_per_km, use those boundaries
        directly — they reflect what the athlete has configured or Garmin has synced,
        so targets derived from them will match both systems exactly.
        Otherwise compute from threshold using the multiplier table below.

        Formula: step_pace_min_km = threshold_pace_min_km × (100 / target_pct)
        Convert to mm:ss: minutes = int(v), seconds = round((v % 1) × 60)

        Effort targets and multipliers (apply to running_threshold_pace_min_per_km):
          Strides / rep     115–120%  → multiply by 0.833–0.870  (fastest)
          VO2max            105–110%  → multiply by 0.909–0.952
          Threshold          93–97%  → multiply by 1.031–1.075
          Tempo              85–90%  → multiply by 1.111–1.176
          Marathon pace      78–82%  → multiply by 1.220–1.282
          Easy / aerobic     70–78%  → multiply by 1.282–1.429  (slowest)

        Write as a ±3–4% range, fast end first:
          e.g. threshold=5:00/km (5.0), tempo 85–90%:
            fast: 5.0 × 100/90 = 5.555 → 5:33/km
            slow: 5.0 × 100/85 = 5.882 → 5:53/km  →  "5:33-5:53/km Pace"

        COMPUTING HR TARGETS (easy runs, warmup, cooldown, recovery intervals)
        ─────────────────────────────────────────
        Use "% LTHR" — intervals.icu resolves it to absolute BPM using the stored
        LTHR. Do NOT use Z1–Z5 HR (Garmin's zone numbers may not match intervals.icu).

          Recovery / rest interval   65–72% LTHR
          Easy / aerobic             72–80% LTHR
          Steady / upper aerobic     80–87% LTHR   (use for warmup before quality)
          Threshold / hard           90–95% LTHR

        Fallback only: use Z1–Z5 HR if run_lthr_bpm is unavailable.

        ONE TARGET PER STEP — pace OR % LTHR, never both on the same line.
          Quality steps (tempo/threshold/VO2max/strides) → absolute pace
          All other steps (easy, warmup, cooldown, recovery) → % LTHR

        RUNNING WORKOUT TYPES
        ─────────────────────────────────────────
        Examples use threshold=5:00/km, LTHR=165bpm. Recompute for actual athlete.

        Easy / Recovery  (RPE 2–3):
            Warmup\\n- 5m 65-72% LTHR\\n\\nMain Set\\n- 40m 72-80% LTHR\\n\\nCooldown\\n- 5m 65-72% LTHR

        Long run  (RPE 3–4, same HR as easy):
            Main Set\\n- 90m 72-80% LTHR

        Aerobic with strides:
            Main Set\\n- 35m 72-80% LTHR\\n\\nStrides 4x\\n- 20s 4:10-4:22/km Pace\\n- 90s 65-72% LTHR

        Tempo run  (comfortably hard, 20–40min, RPE 6–7):
            Warmup\\n- 15m 72-80% LTHR\\n\\nMain Set\\n- 25m 5:33-5:53/km Pace\\n\\nCooldown\\n- 10m 72-78% LTHR

        Threshold / Cruise intervals  (RPE 7–8):
            Warmup\\n- 15m 72-80% LTHR\\n\\nMain Set 4x\\n- 8m 5:09-5:22/km Pace\\n- 90s 65-72% LTHR\\n\\nCooldown\\n- 10m 72-80% LTHR

        VO2max intervals  (near-maximal, RPE 9):
            Warmup\\n- 15m 72-80% LTHR\\n\\nMain Set 6x\\n- 3m 4:33-4:46/km Pace\\n- 3m 65-72% LTHR\\n\\nCooldown\\n- 10m 72-80% LTHR

        Marathon-pace run:
            Warmup\\n- 20m 72-78% LTHR\\n\\nAerobic\\n- 40m 72-80% LTHR\\n\\nMarathon Pace\\n- 20m 6:06-6:25/km Pace\\n\\nCooldown\\n- 10m 72-78% LTHR

        Hill repeats  (power/strength):
            Warmup\\n- 15m 72-80% LTHR\\n\\nMain Set 8x\\n- 60s 4:21-4:46/km Pace\\n- 90s 65-72% LTHR walk\\n\\nCooldown\\n- 10m 72-80% LTHR

        METHODOLOGY NOTES
        ─────────────────────────────────────────
        Polarized: Easy (72–80% LTHR) OR VO2max pace — skip Tempo/Threshold
        Maffetone: Easy at <MAF pace (MAF HR = 180−age), no intensity until base solid
        Norwegian:  two Threshold/Cruise sessions/week, everything else Easy
        Pyramidal:  Easy + Tempo + limited VO2max (traditional mix)
        Jack Daniels: E=70%, M=78%, T=88%, I=98%, R=110% (multiply threshold pace)

        SYNTAX REFERENCE
        ─────────────────────────────────────────
        Pace syntax:     4:45/km Pace / 4:45-5:05/km Pace   (always absolute for runs)
        HR syntax:       72-80% LTHR / 65-72% LTHR           (always % LTHR for runs)
        Power syntax:    Z2 / 75% / 220w / ramp 55-75%       (% = %FTP, cycling)
        Duration syntax: 10m / 1h / 30s / 1h30m / 500mtr / 2km  (mtr not m for metres)
        Repeats:         Nx on its own line before the steps (blank lines around block)
        Sections:        Warmup / Main Set / Cooldown on their own lines

        Args:
            date:         ISO date YYYY-MM-DD (time component added automatically)
            name:         Workout name
            description:  Structured workout using the syntax above — intervals.icu
                          parses this and sends step-by-step targets to the Garmin watch
            sport_type:   Run, Ride, Swim, etc. (default Run)
            category:     WORKOUT (default), RACE, NOTES, TARGET,
                          FITNESS_DAYS, SET_FITNESS, or SET_EFTP
            moving_time:  Estimated duration in seconds (sum of all step durations)
            distance_km:  Target distance in kilometres (e.g. 10.0 for a 10 km run)
            target_tss:   Target Training Stress Score
        """
        payload = {
            "start_date_local": _normalise_date(date),
            "name": name,
            "type": sport_type,
            "category": category,
            "description": description,
        }
        if moving_time is not None:
            payload["moving_time"] = moving_time
        if distance_km is not None:
            payload["distance"] = round(distance_km * 1000)
        if target_tss is not None:
            payload["icu_training_load"] = target_tss
        return await icu_post(f"athlete/{ATHLETE_ID}/events", payload)


@mcp.tool()
async def get_activity_detail(activity_id: str) -> dict:
    """Get full detail for a specific activity.

    Args:
        activity_id: Activity ID e.g. 'i12345678'
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", activity_id):
        return {"error": "Invalid activity_id"}
    return await icu_get(f"activity/{activity_id}")


@mcp.tool()
async def get_activity_intervals(activity_id: str) -> list[dict]:
    """Get the structured intervals/laps for a specific activity.

    Returns each interval with duration, distance, power, HR, pace and TSS —
    useful for analysing effort distribution, pacing execution, and zone compliance
    within a single workout.

    Args:
        activity_id: Activity ID e.g. 'i12345678'
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", activity_id):
        return [{"error": "Invalid activity_id"}]
    return await icu_get(f"activity/{activity_id}/intervals")


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

    For longer-term progress questions ("am I improving?", "is my training working?",
    "recommend a training program") also call get_progress to get monthly trend data
    before answering.

    The returned context includes `preferred_units` (km or miles) and `athlete_timezone`.
    Use these for all distances, paces, and date references in your response.

    The returned context includes `athlete_zones` with Garmin-synced thresholds:
      running_threshold_pace_min_per_km, run_lthr_bpm, run_hr_zones_bpm,
      cycling_ftp_watts, cycling_power_zones_watts, ride_lthr_bpm
    Use these for all workout targets — no need to call get_athlete separately.

    Each activity in recent_activities now includes Garmin-sourced fields when available:
      max_hr, avg_temperature_c (wrist-sensor estimate — treat as relative indicator only,
        not an accurate ambient reading; useful for comparing sessions, not absolute values)
      training_effect_aerobic (0–5), training_effect_anaerobic (0–5), training_effect_label
        (Recovery/Base/Tempo/Threshold/VO2Max/Anaerobic/Sprint)
      Running dynamics (run types only): vertical_oscillation_cm (ideal 6–8 cm),
        ground_contact_time_ms (lower = better), stride_length_m, vertical_ratio_pct
    Wellness entries include `stress` (Garmin stress score) when available.

    Also includes a pre-computed `readiness_metrics` block with:
      hrv_today, hrv_7day_mean, hrv_cv_7day_pct, hrv_cv_flag
      hrv_zscore_today, recovery_index, recovery_index_flag
      acwr, acwr_flag, tss_last_7_days, tss_last_28_days

    Use these directly — do not recompute from the raw wellness list.

    COACHING REVIEW — cover these points after calling this tool:

    1. Latest workout — effort vs intent, HR response, pacing execution

    2. Recovery status
       Use readiness_metrics.recovery_index (RI) and recovery_index_flag:
         Green (≥0.8): normal/hard session permitted
         Amber (0.7–0.79): reduce intensity, no new hard sessions
         Red (<0.7): easy/rest only
       Also check hrv_zscore_today: below −1.5 = suppressed, above +1.5 = elevated

    3. HRV pattern (compound signal — do NOT rely on a single day)
       hrv_cv_7day_pct interpretation (readiness_metrics.hrv_cv_flag):
         <15% stable: consistent recovery, intensity permitted
         15–25% moderate: standard programming
         >25% volatile: restrict intensity even if today's HRV looks fine —
           this indicates underlying chronic stress the daily reading misses

    4. Training load
       Use readiness_metrics.acwr and acwr_flag:
         0.8–1.3 optimal | <0.8 underloading | >1.3 caution | >1.5 danger
       Check tss_last_7_days vs tss_last_28_days for weekly trend

    5. Action hierarchy (apply in order, stop at first match):
         REST      — RI < 0.6 OR HRV z-score < −2.0 OR ACWR > 1.5
         REDUCE    — RI 0.6–0.79 OR HRV z-score < −1.5 OR hrv_cv > 25%
         CAP ZONES — single metric borderline (one flag amber, rest green)
         MONITOR   — all green, no action needed

    6. Mid-week steering
       If earlier sessions this week exceeded TSS targets → scale down remaining days
       If behind weekly target → scale up within ACWR safety limits

    7. Current training phase (Base/Build/Peak/Taper) and plan adjustments

    8. Threshold drift check (check when reviewing hard/race activities)
       Each hard or race activity may include:
         lthr_detected_bpm           — Garmin-detected LTHR for that effort
         lt_pace_detected_min_per_km — Garmin-detected threshold pace
       Compare against athlete_zones.run_lthr_bpm and
       athlete_zones.running_threshold_pace_min_per_km.

       Suggest calling update_sport_settings when:
         - lthr_detected_bpm differs from stored LTHR by ≥3 bpm across ≥2 sessions
         - lt_pace_detected_min_per_km differs from stored threshold pace by ≥0:10/km
           across ≥2 sessions
         - After a race or structured time trial that serves as a threshold benchmark
       Always confirm with the athlete before updating.

    IMPORTANT: Use compound patterns — do not recommend rest based on a single
    metric spike. Require multiple signals before major load changes.

    Args:
        activity_id: Optional specific activity ID to focus on (e.g. 'i12345678').
                     Leave empty to use the most recent activity.
    """
    from claude_coach import fetch_context
    ctx = await fetch_context(http(), activity_id)
    profile = _load_profile()
    ctx["athlete_profile"] = profile
    ctx["race_goal"] = _load_goal()
    ctx["preferred_units"] = profile.get("preferred_units") or "km"
    ctx["athlete_timezone"] = profile.get("timezone") or "unknown"
    return ctx


if not READ_ONLY:
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
            date: New date as ISO YYYY-MM-DD (reschedule; time component added automatically)
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
            payload["start_date_local"] = _normalise_date(date)
        if not payload:
            return {"error": "No fields provided to update"}
        return await icu_put(f"athlete/{ATHLETE_ID}/events/{event_id}", payload)


if not READ_ONLY:
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


if not READ_ONLY:
    @mcp.tool()
    async def create_plan(workouts: list[dict]) -> list[dict]:
        """Create multiple planned workouts in one call — use this to build a training plan.

        Prefer this over calling create_workout repeatedly. Workouts sync to Garmin Connect
        automatically (up to 7 days ahead) if the athlete has connected Garmin in settings.

        HOW GARMIN NATIVE WORKOUTS WORK
        intervals.icu parses the description text and generates a structured workout
        that syncs step-by-step to the Garmin watch — with HR alerts, pace zones, and
        rep countdowns per step. The description IS the structured workout.

        Each dict in the list supports these fields:
            start_date_local  (required) ISO date YYYY-MM-DD (time added automatically)
            name              (required) Workout name
            type              (required) Sport: Run, Ride, Swim, VirtualRide, etc.
            description       (required) Structured workout using the syntax below
            category          WORKOUT (default), RACE, NOTES, TARGET,
                              FITNESS_DAYS, SET_FITNESS, SET_EFTP
            moving_time       Estimated duration in seconds (sum of all step durations)
            distance_km       Target distance in kilometres — converted to metres automatically
            icu_training_load Target TSS

        BEFORE WRITING ANY TARGETS — call get_athlete first (required)
        ─────────────────────────────────────────
        get_athlete → running_threshold_pace_min_per_km, run_lthr_bpm,
                      running_pace_zones_min_per_km (athlete's configured zones, if set)
        get_profile → fallback if get_athlete returns nothing

        COMPUTING PACE TARGETS (quality sessions: tempo, threshold, VO2max, strides)
        ─────────────────────────────────────────
        ALWAYS use absolute pace — "% Pace" or "Z2 Pace" silently produces "run until
        press lap" on Garmin when pace zones are not fully configured.

        If get_athlete returns running_pace_zones_min_per_km, use those boundaries
        directly — they reflect what the athlete has configured or Garmin has synced,
        so targets derived from them will match both systems exactly.
        Otherwise compute from threshold using the multiplier table below.

        Formula: step_pace_min_km = threshold_pace_min_km × (100 / target_pct)
        Convert to mm:ss: minutes = int(v), seconds = round((v % 1) × 60)

        Effort targets and multipliers (apply to running_threshold_pace_min_per_km):
          Strides / rep     115–120%  → multiply by 0.833–0.870  (fastest)
          VO2max            105–110%  → multiply by 0.909–0.952
          Threshold          93–97%  → multiply by 1.031–1.075
          Tempo              85–90%  → multiply by 1.111–1.176
          Marathon pace      78–82%  → multiply by 1.220–1.282
          Easy / aerobic     70–78%  → multiply by 1.282–1.429  (slowest)

        Write as a ±3–4% range, fast end first:
          e.g. threshold=5:00/km (5.0), tempo 85–90%:
            fast: 5.0 × 100/90 = 5.555 → 5:33/km
            slow: 5.0 × 100/85 = 5.882 → 5:53/km  →  "5:33-5:53/km Pace"

        COMPUTING HR TARGETS (easy runs, warmup, cooldown, recovery intervals)
        ─────────────────────────────────────────
        Use "% LTHR" — DO NOT use Z1–Z5 HR (Garmin's zone numbers may not match).

          Recovery / rest interval   65–72% LTHR
          Easy / aerobic             72–80% LTHR
          Steady / upper aerobic     80–87% LTHR   (use for warmup before quality)
          Threshold / hard           90–95% LTHR

        Fallback only: use Z1–Z5 HR if run_lthr_bpm is unavailable.

        ONE TARGET PER STEP — pace OR % LTHR, never both on the same line.
          Quality steps (tempo/threshold/VO2max/strides) → absolute pace
          All other steps (easy, warmup, cooldown, recovery) → % LTHR

        RUNNING WORKOUT TYPES AND DESCRIPTION SYNTAX
        ─────────────────────────────────────────
        Examples use threshold=5:00/km. Recompute for actual athlete.

        Easy/Recovery:   "Warmup\\n- 5m 65-72% LTHR\\n\\nMain Set\\n- 40m 72-80% LTHR\\n\\nCooldown\\n- 5m 65-72% LTHR"
        Long run:        "Main Set\\n- 90m 72-80% LTHR"
        Strides:         "Main Set\\n- 35m 72-80% LTHR\\n\\nStrides 4x\\n- 20s 4:10-4:22/km Pace\\n- 90s 65-72% LTHR"
        Tempo run:       "Warmup\\n- 15m 72-80% LTHR\\n\\nMain Set\\n- 25m 5:33-5:53/km Pace\\n\\nCooldown\\n- 10m 72-78% LTHR"
        Threshold/Cruise:"Warmup\\n- 15m 72-80% LTHR\\n\\nMain Set 4x\\n- 8m 5:09-5:22/km Pace\\n- 90s 65-72% LTHR\\n\\nCooldown\\n- 10m 72-80% LTHR"
        VO2max:          "Warmup\\n- 15m 72-80% LTHR\\n\\nMain Set 6x\\n- 3m 4:33-4:46/km Pace\\n- 3m 65-72% LTHR\\n\\nCooldown\\n- 10m 72-80% LTHR"
        Hill repeats:    "Warmup\\n- 15m 72-80% LTHR\\n\\nMain Set 8x\\n- 60s 4:21-4:46/km Pace\\n- 90s 65-72% LTHR walk\\n\\nCooldown\\n- 10m 72-80% LTHR"
        Marathon pace:   "Warmup\\n- 20m 72-78% LTHR\\n\\nAerobic\\n- 40m 72-80% LTHR\\n\\nMarathon Pace\\n- 20m 6:06-6:25/km Pace\\n\\nCooldown\\n- 10m 72-78% LTHR"

        METHODOLOGY NOTES
        ─────────────────────────────────────────
        Polarized: Easy (72–80% LTHR) OR VO2max pace — skip Tempo/Threshold
        Maffetone: Easy at <MAF pace (MAF HR = 180−age), no intensity until base solid
        Norwegian:  two Threshold/Cruise sessions/week, everything else Easy
        Pyramidal:  Easy + Tempo + limited VO2max (traditional mix)
        Jack Daniels: E=70%, M=78%, T=88%, I=98%, R=110% (multiply threshold pace)

        SYNTAX REFERENCE
        ─────────────────────────────────────────
        Pace syntax:     4:45/km Pace / 4:45-5:05/km Pace   (always absolute for runs)
        HR syntax:       72-80% LTHR / 65-72% LTHR           (always % LTHR for runs)
        Power syntax:    Z2 / 75% / 220w / ramp 55-75%       (% = %FTP, cycling)
        Duration syntax: 10m / 1h / 30s / 1h30m / 500mtr / 2km  (mtr not m for metres)
        Repeats:         Nx on its own line before the steps (blank lines around block)
        Sections:        Warmup / Main Set / Cooldown on their own lines

        Args:
            workouts: List of workout objects to create on the calendar.
        """
        _PLAN_FIELDS = frozenset({
            "start_date_local", "name", "type", "category", "description",
            "moving_time", "distance", "icu_training_load",
        })
        safe = []
        for w in workouts:
            entry = {k: v for k, v in w.items() if k in _PLAN_FIELDS}
            entry.setdefault("category", "WORKOUT")
            if "distance_km" in w and "distance" not in entry:
                entry["distance"] = round(w["distance_km"] * 1000)
            if "start_date_local" in entry:
                entry["start_date_local"] = _normalise_date(entry["start_date_local"])
            safe.append(entry)
        return await icu_post(f"athlete/{ATHLETE_ID}/events/bulk", safe)


if not READ_ONLY:
    @mcp.tool()
    async def update_sport_settings(
        sport: str,
        threshold_pace_min_per_km: float | None = None,
        lthr_bpm: int | None = None,
        recalc_hr_zones: bool = True,
        pace_zones_min_per_km: list[float] | None = None,
    ) -> dict:
        """Update threshold pace, LTHR, and/or pace zones for a sport in intervals.icu.

        Call this when detected thresholds (lthr_detected_bpm, lt_pace_detected_min_per_km
        from recent hard/race activities) suggest the stored values are out of date.
        Updated thresholds are synced to Garmin automatically and will be used for all
        future workout pace/HR targets.

        Use coaching judgement before updating:
        - Require ≥1 confirmed hard effort (race, time trial, or structured threshold test)
          showing a consistent result — do not update from a single training run
        - Detected LTHR from Garmin is reliable; detected pace is more variable (wind,
          course profile, fatigue). Average 2–3 data points when possible.
        - Always confirm with the athlete before writing ("your recent 10 km showed a
          threshold pace of 4:52/km — shall I update your settings?")

        When to update thresholds:
        - lthr_detected_bpm differs from run_lthr_bpm by ≥3 bpm across ≥2 sessions
        - lt_pace_detected_min_per_km differs from running_threshold_pace_min_per_km
          by ≥0:10/km across ≥2 sessions
        - After a race or time trial that serves as a threshold benchmark

        For pace zones: prefer setup_run_pace_zones which auto-computes correct boundaries.
        Only pass pace_zones_min_per_km manually if you need custom zone placement.
        pace_zones_min_per_km: 6 values, upper boundary of Z1–Z6 slowest→fastest,
          e.g. for threshold=5:00/km: [6:40, 5:53, 5:22, 5:00, 4:38, 4:20]

        Args:
            sport:                     Sport key e.g. "Run", "Ride", "Swim"
            threshold_pace_min_per_km: New threshold pace in min/km (e.g. 4.87 for 4:52/km).
                                       Converted to m/s internally.
            lthr_bpm:                  New lactate threshold heart rate in bpm.
            recalc_hr_zones:           Recalculate HR zones from new LTHR (default True).
            pace_zones_min_per_km:     6 zone upper-boundary paces in min/km (use
                                       setup_run_pace_zones instead for auto-computation).
        """
        if threshold_pace_min_per_km is None and lthr_bpm is None and pace_zones_min_per_km is None:
            return {"error": "Provide at least one of threshold_pace_min_per_km, lthr_bpm, or pace_zones_min_per_km"}
        payload: dict = {}
        if threshold_pace_min_per_km is not None:
            payload["threshold_pace"] = _min_km_to_ms(threshold_pace_min_per_km)
        if lthr_bpm is not None:
            payload["lthr"] = lthr_bpm
        if pace_zones_min_per_km is not None:
            if len(pace_zones_min_per_km) != 6:
                return {"error": "pace_zones_min_per_km must have exactly 6 values (Z1–Z6 upper boundaries)"}
            payload["pace_zones"] = [_min_km_to_ms(p) for p in pace_zones_min_per_km]
        params = {"recalcHrZones": "true"} if (lthr_bpm is not None and recalc_hr_zones) else None
        return await icu_put(f"athlete/{ATHLETE_ID}/sport-settings/{sport}", payload, params=params)

    @mcp.tool()
    async def setup_run_pace_zones(
        threshold_pace_min_per_km: float | None = None,
        force: bool = False,
    ) -> dict:
        """Read or write running pace zones in intervals.icu.

        DEFAULT BEHAVIOUR (force=False) — read-only:
          Always fetches the current pace zones and threshold from intervals.icu first.
          If zones are already configured, returns them WITHOUT writing anything.
          Use this to inspect what is currently set before deciding to change anything.

        WRITE BEHAVIOUR (force=True):
          Computes new zone boundaries from the given (or stored) threshold pace and
          writes them. Only do this when the athlete explicitly asks to reset zones, or
          when no zones exist yet. Never call with force=True speculatively.

        Computed zones use Garmin's 5-zone model extended with a 6th anaerobic zone,
        as percentages of threshold SPEED:

          Z1 Recovery     < 78% threshold speed   (easy jogging)
          Z2 Endurance   78–86% threshold speed   (aerobic base)
          Z3 Tempo       86–93% threshold speed   (comfortably hard)
          Z4 Threshold   93–100% threshold speed  (lactate threshold)
          Z5 VO2max     100–108% threshold speed  (near-maximal)
          Z6 Anaerobic  108–115% threshold speed  (sprint/max effort)

        Z1–Z4 match Garmin's auto-calculated zone boundaries exactly, so intervals.icu
        and Garmin zone analysis will agree. Z5/Z6 split the above-threshold range.

        Args:
            threshold_pace_min_per_km: Threshold pace for zone computation (force=True only).
                                       If omitted, reads from stored running_threshold_pace.
            force:                     Set True to overwrite existing zones. Default False
                                       (read-only — inspect before changing).
        """
        athlete = await icu_get(f"athlete/{ATHLETE_ID}")
        sport_settings = athlete.get("sportSettings") or []

        existing_zones: list[float] = []
        stored_threshold: float | None = None
        for ss in sport_settings:
            if ss.get("activity_type") == "Run":
                if ss.get("threshold_pace"):
                    stored_threshold = _ms_to_min_per_km(ss["threshold_pace"])
                if ss.get("pace_zones"):
                    existing_zones = [_ms_to_min_per_km(v) for v in ss["pace_zones"] if v]
                break

        if existing_zones and not force:
            return {
                "status": "already_configured",
                "message": "Pace zones are already set in intervals.icu. Pass force=True to overwrite.",
                "threshold_pace": _format_pace(stored_threshold) if stored_threshold else None,
                "current_zones": {
                    f"Z{i+1}": {"upper_boundary": _format_pace(z)} if z else None
                    for i, z in enumerate(existing_zones)
                },
            }

        threshold = threshold_pace_min_per_km or stored_threshold
        if not threshold:
            return {"error": "No threshold pace available. Pass threshold_pace_min_per_km or set it via update_sport_settings first."}

        t_ms = _min_km_to_ms(threshold)
        zone_pcts = [0.78, 0.86, 0.93, 1.00, 1.08, 1.15]
        zone_ms   = [round(pct * t_ms, 6) for pct in zone_pcts]
        zone_paces = [_ms_to_min_per_km(z) for z in zone_ms]

        result = await icu_put(f"athlete/{ATHLETE_ID}/sport-settings/Run", {"pace_zones": zone_ms})

        return {
            "status": "written",
            "threshold_pace": _format_pace(threshold),
            "zones_written": {
                f"Z{i+1}": {"upper_boundary": _format_pace(zone_paces[i]), "pct_threshold": int(zone_pcts[i]*100)}
                for i in range(6)
            },
            "result": result,
        }


@mcp.tool()
async def get_profile() -> dict:
    """Return the stored athlete profile (training preferences, paces, limiters).

    Use update_profile to change any field. Use set_race_goal to switch into
    event-prep (periodized) coaching mode.
    """
    return _load_profile()


if not READ_ONLY:
    @mcp.tool()
    async def update_profile(updates: dict) -> dict:
        """Update one or more fields in the athlete profile.

        Merges the given fields into the existing profile. Pass only the fields
        you want to change. Supported fields:
            sport                      e.g. "running", "cycling", "triathlon"
            age                        integer (years)
            location                   city or "lat,lon" — used by get_weather
            timezone                   IANA timezone e.g. "Europe/Oslo" — used for
                                       correct date/scheduling decisions
            preferred_units            "km" (default) or "miles" — Claude uses this
                                       for all distances in responses
            training_days_per_week     integer
            easy_pace_min_per_km       float (e.g. 6.0 for 6:00/km)
            threshold_pace_min_per_km  float
            weekly_volume_km           float
            known_limiters             list of strings
            notes                      free-text string

        Args:
            updates: Dict of fields to update.
        """
        _PROFILE_FIELDS = frozenset({
            "sport", "age", "location", "timezone", "preferred_units",
            "training_days_per_week", "easy_pace_min_per_km",
            "threshold_pace_min_per_km", "weekly_volume_km",
            "known_limiters", "notes",
        })
        profile = _load_profile()
        safe = {k: v for k, v in updates.items() if k in _PROFILE_FIELDS}
        if not safe:
            return {"error": "No valid fields provided"}
        profile.update(safe)
        _write_json_file(_PROFILE_PATH, profile)
        log.info("Athlete profile updated: %s", list(safe.keys()))
        return profile


    @mcp.tool()
    async def set_coaching_style(
        methodology: str,
        custom_description: str | None = None,
    ) -> dict:
        """Choose a coaching methodology that persists across all conversations.

        The selected methodology is stored in the athlete profile and automatically
        applied to every coaching review — both via Claude.ai (review_training) and
        the automated /coach endpoint.

        Built-in presets (pass the slug as methodology):
          polarized     — 80% easy (Zone 1), 20% hard (Zone 3). Avoid threshold.
                          Key sessions: long easy runs, fartlek, VO2max intervals.
          maffetone     — Train below MAF HR (180 − age) to build aerobic base.
                          No intervals until base is established.
          jack_daniels  — VDOT-based paces from recent race time. Five zones:
                          Easy, Marathon, Threshold, Interval, Repetition.
          norwegian     — Two threshold sessions/week at ~75–80% HRmax.
                          High volume, controlled effort. All else easy.
          pyramidal     — ~70% easy, ~20% threshold, ~10% hard.
                          Traditional approach; allows comfortably-hard efforts.
          custom        — Define your own (requires custom_description).

        Args:
            methodology:        Preset slug (see above) or "custom"
            custom_description: Required when methodology="custom". Free-text
                                description of the training philosophy, session
                                types, intensity distribution, and priorities.
        """
        if methodology == "custom":
            if not custom_description:
                return {"error": "custom_description is required when methodology='custom'"}
            display_name = "Custom"
            description = custom_description
        elif methodology in _METHODOLOGY_PRESETS:
            display_name, description = _METHODOLOGY_PRESETS[methodology]
        else:
            valid = list(_METHODOLOGY_PRESETS.keys()) + ["custom"]
            return {"error": f"Unknown methodology {methodology!r}. Valid options: {valid}"}

        profile = _load_profile()
        profile["coaching_methodology"] = display_name
        profile["coaching_description"] = description
        _write_json_file(_PROFILE_PATH, profile)
        log.info("Coaching style set: %s", display_name)
        return {"coaching_methodology": display_name, "coaching_description": description}


    @mcp.tool()
    async def set_race_goal(
        event_name: str,
        event_date: str,
        distance_km: float,
        target_time: str | None = None,
        notes: str | None = None,
    ) -> dict:
        """Store a race goal — switches coaching into periodized event-prep mode.

        Once a race goal is set, coaching reviews will structure training across
        Base → Build → Peak → Taper phases leading to the event date.
        The current phase is inferred automatically from weeks remaining.

        Args:
            event_name:  Name of the event, e.g. "Oslo Half Marathon"
            event_date:  Race date ISO YYYY-MM-DD
            distance_km: Race distance in km (e.g. 10.0, 21.1, 42.2)
            target_time: Optional target e.g. "sub-2:00" or "1:55:00"
            notes:       Optional context e.g. "first marathon, focus on finishing"
        """
        from datetime import date as _date
        try:
            race_date = _date.fromisoformat(event_date)
        except ValueError:
            return {"error": f"Invalid event_date {event_date!r} — use YYYY-MM-DD"}
        weeks_out = (race_date - _date.today()).days / 7
        if weeks_out < 0:
            return {"error": "event_date is in the past"}
        if weeks_out > 16:
            phase = "base"
        elif weeks_out > 8:
            phase = "build"
        elif weeks_out > 4:
            phase = "peak"
        elif weeks_out > 1:
            phase = "taper"
        else:
            phase = "race_week"
        goal = {
            "event_name": event_name,
            "event_date": event_date,
            "distance_km": distance_km,
            "target_time": target_time,
            "notes": notes,
            "current_phase": phase,
            "weeks_to_race": round(weeks_out, 1),
            "set_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_file(_GOAL_PATH, goal)
        log.info("Race goal set: %s on %s (phase=%s, %.1f weeks out)",
                 event_name, event_date, phase, weeks_out)
        return goal


    @mcp.tool()
    async def clear_race_goal() -> dict:
        """Remove the current race goal — returns coaching to general improvement mode.

        Use this after the race is done or if plans have changed.
        """
        _clear_goal()
        log.info("Race goal cleared")
        return {"status": "cleared"}


@mcp.tool()
async def get_weather(days: int = 7) -> dict:
    """Get the weather forecast for the athlete's stored location.

    Uses Open-Meteo (no API key required). Returns daily precipitation,
    temperature, and wind speed for each day.

    Call this when:
    - The athlete asks about upcoming weather
    - Deciding whether to move a long run to a drier day
    - Asking "should I wear rain gear on Thursday?"
    - Checking if conditions are suitable for an outdoor session

    Requires `location` to be set in the athlete profile via update_profile.

    Args:
        days: Number of forecast days to fetch (1-14, default 7)
    """
    profile = _load_profile()
    location = profile.get("location", "").strip()
    if not location:
        return {
            "error": "No location set in athlete profile. "
                     "Ask the athlete where they train and call update_profile with location='City, Country'."
        }

    days = max(1, min(14, days))

    lat: float | None = None
    lon: float | None = None
    resolved_name = location

    # Accept "lat,lon" form directly
    parts = location.replace(" ", "").split(",")
    if len(parts) == 2:
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            pass

    if lat is None:
        geo_r = await http().get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "en", "format": "json"},
        )
        geo_r.raise_for_status()
        results = geo_r.json().get("results", [])
        if not results:
            return {"error": f"Could not find location: {location!r}"}
        lat = results[0]["latitude"]
        lon = results[0]["longitude"]
        resolved_name = f"{results[0]['name']}, {results[0].get('country', '')}".strip(", ")

    forecast_r = await http().get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": ",".join([
                "weather_code",
                "precipitation_sum",
                "precipitation_probability_max",
                "temperature_2m_max",
                "temperature_2m_min",
                "wind_speed_10m_max",
            ]),
            "forecast_days": days,
            "timezone": "auto",
        },
    )
    forecast_r.raise_for_status()
    data = forecast_r.json()
    daily = data.get("daily", {})
    dates = daily.get("time", [])

    def _wmo(code: int) -> str:
        if code == 0:            return "Clear sky"
        if code == 1:            return "Mainly clear"
        if code == 2:            return "Partly cloudy"
        if code == 3:            return "Overcast"
        if code in (45, 48):     return "Fog"
        if code in (51, 53):     return "Light drizzle"
        if code == 55:           return "Dense drizzle"
        if code in (61, 63):     return "Rain"
        if code == 65:           return "Heavy rain"
        if code in (71, 73):     return "Snow"
        if code == 75:           return "Heavy snow"
        if code in (80, 81):     return "Rain showers"
        if code == 82:           return "Violent showers"
        if code in (85, 86):     return "Snow showers"
        if code == 95:           return "Thunderstorm"
        if code in (96, 99):     return "Thunderstorm with hail"
        return "Unknown"

    codes   = daily.get("weather_code", [])
    precip  = daily.get("precipitation_sum", [])
    precip_prob = daily.get("precipitation_probability_max", [])
    tmax    = daily.get("temperature_2m_max", [])
    tmin    = daily.get("temperature_2m_min", [])
    wind    = daily.get("wind_speed_10m_max", [])

    forecast = []
    for i, date in enumerate(dates):
        forecast.append({
            "date": date,
            "conditions": _wmo(codes[i]) if i < len(codes) else "Unknown",
            "precipitation_mm": precip[i] if i < len(precip) else None,
            "precipitation_probability_pct": precip_prob[i] if i < len(precip_prob) else None,
            "temp_max_c": tmax[i] if i < len(tmax) else None,
            "temp_min_c": tmin[i] if i < len(tmin) else None,
            "wind_max_kmh": wind[i] if i < len(wind) else None,
        })

    return {"location": resolved_name, "forecast": forecast}


@mcp.tool()
async def get_progress(months: int = 3) -> dict:
    """Assess training progress over a multi-month window.

    Returns monthly summaries of volume, training load (TSS), CTL (fitness),
    and wellness metrics (HRV, sleep, resting HR). Use this when the athlete
    asks:
    - "Am I improving?" / "Is my training working?"
    - "How has my fitness changed over the past few months?"
    - "I don't think I'm progressing — what's going wrong?"
    - Before recommending a new training program (understand the baseline trend)

    How to interpret the results:
    - Rising CTL month-over-month → fitness is building
    - Stable or falling CTL despite consistent training → accumulating fatigue or not enough stimulus
    - Rising HRV trend → improving recovery capacity / aerobic adaptation
    - Falling resting HR over months → aerobic base development
    - Volume increasing while wellness holds → good adaptation
    - Volume increasing while HRV falls + resting HR rises → overreaching risk

    Args:
        months: Months of history to analyse (1–12, default 3)
    """
    from collections import defaultdict

    months = max(1, min(12, months))
    days = months * 31 + 7  # small buffer to capture full first month

    activities, wellness = await asyncio.gather(
        icu_get(f"athlete/{ATHLETE_ID}/activities",
                params={"oldest": days_ago_iso(days), "newest": today_iso()}),
        icu_get(f"athlete/{ATHLETE_ID}/wellness",
                params={"oldest": days_ago_iso(days), "newest": today_iso()}),
    )

    # Group by YYYY-MM
    by_month_acts: dict[str, list] = defaultdict(list)
    for a in activities:
        m = (a.get("start_date_local") or "")[:7]
        if m:
            by_month_acts[m].append(a)

    by_month_well: dict[str, list] = defaultdict(list)
    for w in wellness:
        m = (w.get("id") or "")[:7]
        if m:
            by_month_well[m].append(w)

    def _avg(items: list, key: str) -> float | None:
        vals = [v[key] for v in items if v.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    monthly: list[dict] = []
    for month in sorted(by_month_acts.keys()):
        acts = sorted(by_month_acts[month], key=lambda a: a.get("start_date_local", ""))
        wells = by_month_well.get(month, [])

        sleep_vals = [w["sleepSecs"] / 3600 for w in wells if w.get("sleepSecs")]

        # Easy-pace proxy: runs with avg HR < 155 bpm and distance > 1km
        easy_runs = [
            a for a in acts
            if a.get("type") in ("Run", "VirtualRun")
            and (a.get("average_heartrate") or 999) < 155
            and (a.get("distance") or 0) > 1000
        ]
        easy_paces = [
            a["moving_time"] / a["distance"] * 1000 / 60
            for a in easy_runs
            if a.get("moving_time") and a.get("distance")
        ]

        monthly.append({
            "month": month,
            "activity_count": len(acts),
            "total_km": round(sum((a.get("distance") or 0) for a in acts) / 1000, 1),
            "total_tss": round(sum(a.get("icu_training_load") or 0 for a in acts)),
            "ctl_end": acts[-1].get("icu_ctl") if acts else None,
            "avg_easy_pace_min_per_km": round(sum(easy_paces) / len(easy_paces), 2) if easy_paces else None,
            "avg_hrv": _avg(wells, "hrv"),
            "avg_resting_hr": _avg(wells, "restingHR"),
            "avg_sleep_h": round(sum(sleep_vals) / len(sleep_vals), 1) if sleep_vals else None,
        })

    # Trim to requested months (buffer may have added an extra partial month)
    monthly = monthly[-months:]

    ctl_start = monthly[0].get("ctl_end") if monthly else None
    ctl_end   = monthly[-1].get("ctl_end") if monthly else None

    return {
        "period_months": months,
        "monthly_summaries": monthly,
        "trend": {
            "ctl_start": ctl_start,
            "ctl_end": ctl_end,
            "ctl_change": round(ctl_end - ctl_start, 1) if ctl_start and ctl_end else None,
            "total_km": round(sum(m["total_km"] for m in monthly), 1),
            "total_activities": sum(m["activity_count"] for m in monthly),
        },
    }


@mcp.tool()
async def get_best_efforts(months: int = 12) -> dict:
    """Get personal best paces at standard running distances over recent history.

    Scans completed run activities and finds the fastest average pace recorded
    at each standard distance bracket (±15% tolerance). Most accurate for race
    efforts and time trials where average pace = best effort pace; for training
    runs it reflects the fastest full-activity average at that distance.

    Use this to:
    - Answer "am I getting faster at 10km / half marathon?"
    - Identify the athlete's current benchmark pace at key distances
    - Provide context for setting race goals and workout targets
    - Show progress over time by comparing to older bests

    Args:
        months: How many months of history to scan (default 12, max 24)
    """
    months = min(max(months, 1), 24)
    data = await icu_get(
        f"athlete/{ATHLETE_ID}/activities",
        params={"oldest": days_ago_iso(months * 30), "newest": today_iso()},
    )

    _RUN_SET = frozenset({"Run", "VirtualRun", "TrailRun", "Treadmill"})
    runs = [
        a for a in data
        if a.get("type") in _RUN_SET
        and (a.get("moving_time") or 0) > 0
        and (a.get("distance") or 0) > 0
    ]

    targets = [
        ("1km",          1.0),
        ("3km",          3.0),
        ("5km",          5.0),
        ("8km",          8.0),
        ("10km",        10.0),
        ("15km",        15.0),
        ("Half Marathon", 21.0975),
        ("Marathon",     42.195),
    ]

    bests: dict[str, dict] = {}
    for label, target_km in targets:
        low  = target_km * 0.85
        high = target_km * 1.15
        best: dict | None = None
        for a in runs:
            dist_km = a["distance"] / 1000
            if not (low <= dist_km <= high):
                continue
            pace = a["moving_time"] / a["distance"] * 1000 / 60  # min/km
            if best is None or pace < best["pace_min_per_km"]:
                best = {
                    "pace_min_per_km": round(pace, 3),
                    "pace_formatted": _format_pace(pace),
                    "finish_time": _format_duration(a["moving_time"]),
                    "distance_km": round(dist_km, 2),
                    "date": a.get("start_date_local", "")[:10],
                    "activity_name": a.get("name"),
                }
        if best:
            bests[label] = best

    return {"period_months": months, "best_efforts": bests}


def _format_duration(seconds: int) -> str:
    """Format seconds as H:MM:SS or M:SS."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


_RUN_TYPES = frozenset({"Run", "VirtualRun", "TrailRun", "Treadmill"})


def _cadence_fields(a: dict) -> dict:
    """Return cadence field(s) with unambiguous names.

    Garmin records running cadence as one-foot SPM (a single leg), so the raw
    value stored by intervals.icu is half the true turnover rate. Cycling cadence
    is already in RPM (both legs), so no doubling is needed.
    """
    cadence = a.get("average_cadence")
    if cadence is None:
        return {}
    if a.get("type") in _RUN_TYPES:
        return {
            "avg_cadence_spm_per_foot": cadence,
            "avg_cadence_total_spm": round(cadence * 2, 1),
        }
    return {"avg_cadence_rpm": cadence}


def _summarise_activity(a: dict) -> dict:
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
        "avg_pace_per_km": a.get("icu_average_speed"),
        "avg_power": a.get("average_watts"),
        "elevation_m": a.get("total_elevation_gain"),
        "avg_temperature_c": a.get("average_temp"),
        "ctl": a.get("icu_ctl"),
        "atl": a.get("icu_atl"),
        "tsb": a.get("icu_tsb"),
        "training_effect_aerobic": a.get("total_training_effect"),
        "training_effect_anaerobic": a.get("total_anaerobic_training_effect"),
        "training_effect_label": a.get("training_effect_label"),
        # Garmin's per-activity threshold estimates (present on hard/race efforts)
        "lthr_detected_bpm": a.get("lthr_detected"),
        "lt_pace_detected_min_per_km": _ms_to_min_per_km(a["lt_pace_detected"]) if a.get("lt_pace_detected") else None,
    }
    d.update(_cadence_fields(a))
    # Running dynamics fields from Garmin (only present for run types)
    if a.get("type") in _RUN_TYPES:
        for src, dst in (
            ("avg_vertical_oscillation", "vertical_oscillation_cm"),
            ("avg_ground_contact_time", "ground_contact_time_ms"),
            ("avg_stride_length", "stride_length_m"),
            ("avg_vertical_ratio", "vertical_ratio_pct"),
        ):
            if a.get(src) is not None:
                d[dst] = a[src]
    return d


# ---------------------------------------------------------------------------
# OAuth 2.1 — built-in authorization server
# State is in-memory. Tokens survive until expiry or process restart.
# ---------------------------------------------------------------------------
_oauth_clients: dict[str, dict] = {}     # client_id → registration data
_oauth_codes: dict[str, dict] = {}       # code → {client_id, redirect_uri, ...}
_oauth_tokens: dict[str, dict] = {}      # sha256(token) → {client_id, expires_at}
_authorize_failures: dict[str, dict] = {} # ip → {count, locked_until}
_oauth_lock = asyncio.Lock()

_TOKEN_STORE = "/data/oauth_tokens.json"

_TOKEN_EXPIRY       = _safe_int(os.environ.get("TOKEN_EXPIRY_DAYS"), 180) * 86400
_CODE_EXPIRY        = 600   # seconds
_MAX_LOGIN_FAILURES = 5
_LOCKOUT_SECONDS    = 3600  # 1 hour
_MAX_CLIENTS        = 10
_CLIENT_TTL         = 86400  # evict stale client registrations after 24 h


def _gen_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


def _ts() -> int:
    return int(time.time())


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _save_tokens() -> None:
    """Persist the hashed token store atomically. Silent on I/O error."""
    try:
        tmp = _TOKEN_STORE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_oauth_tokens, f)
        os.replace(tmp, _TOKEN_STORE)
    except OSError:
        pass


def _load_tokens() -> None:
    """Load persisted hashed tokens, discarding any that have already expired."""
    try:
        with open(_TOKEN_STORE) as f:
            data = json.load(f)
        now = _ts()
        loaded = {k: v for k, v in data.items() if v.get("expires_at", 0) > now}
        _oauth_tokens.update(loaded)
        log.info("Loaded %d persisted OAuth token(s)", len(loaded))
    except OSError:
        pass


def _pkce_verify(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(computed, challenge)


def _is_locked_out(ip: str) -> bool:
    entry = _authorize_failures.get(ip)
    if not entry:
        return False
    if not entry["locked_until"]:
        return False  # failures recorded but threshold not yet reached
    if _ts() < entry["locked_until"]:
        return True
    del _authorize_failures[ip]  # lock expired — evict
    return False


def _record_failure(ip: str) -> None:
    entry = _authorize_failures.setdefault(ip, {"count": 0, "locked_until": 0})
    entry["count"] += 1
    if entry["count"] >= _MAX_LOGIN_FAILURES:
        entry["locked_until"] = _ts() + _LOCKOUT_SECONDS
        log.warning("IP %s locked out after %d failed login attempts", ip, entry["count"])


def _clear_failures(ip: str) -> None:
    _authorize_failures.pop(ip, None)


def _validate_oauth_token(request: Request) -> bool:
    if not COACH_SECRET:
        return True  # no auth configured — allow all (warn at startup)
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    token = auth[7:].strip()
    h = _hash_token(token)
    token_data = _oauth_tokens.get(h)
    if not token_data:
        return False
    if token_data["expires_at"] < _ts():
        _oauth_tokens.pop(h, None)
        return False
    return True


async def handle_oauth_server_metadata(request: Request) -> Response:
    host = request.headers.get("host", "")
    base = f"https://{host}"
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
        "scopes_supported": ["mcp"],
    })


async def handle_oauth_resource_metadata(request: Request) -> Response:
    host = request.headers.get("host", "")
    base = f"https://{host}"
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
    })


async def handle_register(request: Request) -> Response:
    ip = _get_ip(request)
    if not _check_rate_limit(ip):
        return JSONResponse({"error": "too_many_requests"}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    redirect_uris = body.get("redirect_uris")
    if not redirect_uris or not isinstance(redirect_uris, list):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)

    client_id = _gen_token(16)
    async with _oauth_lock:
        # Evict stale registrations before enforcing the cap
        if len(_oauth_clients) >= _MAX_CLIENTS:
            now = _ts()
            stale = [cid for cid, c in _oauth_clients.items()
                     if now - c.get("registered_at", 0) > _CLIENT_TTL]
            for cid in stale:
                del _oauth_clients[cid]
        if len(_oauth_clients) >= _MAX_CLIENTS:
            return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
        _oauth_clients[client_id] = {
            "redirect_uris": redirect_uris,
            "client_name": body.get("client_name", ""),
            "registered_at": _ts(),
        }
    log.info("OAuth client registered: %s name=%r uris=%s", client_id, body.get("client_name", ""), redirect_uris)
    return JSONResponse({
        "client_id": client_id,
        "redirect_uris": redirect_uris,
        "client_name": body.get("client_name", ""),
        "client_id_issued_at": _ts(),
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, status_code=201)


_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _prune_oauth_codes() -> None:
    """Remove expired auth codes — called inside _oauth_lock."""
    now = _ts()
    expired = [c for c, d in _oauth_codes.items() if d["expires_at"] < now]
    for c in expired:
        del _oauth_codes[c]


_LOGIN_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Intervals.icu MCP — Connect</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f3f4f6;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.card{{background:#fff;border-radius:12px;padding:40px;width:360px;box-shadow:0 4px 24px rgba(0,0,0,.1)}}
h2{{margin:0 0 6px;font-size:1.2rem;color:#111}}
p{{color:#6b7280;font-size:.9rem;margin:0 0 20px}}
input{{width:100%;padding:10px 12px;border:1px solid #d1d5db;border-radius:8px;
       font-size:1rem;outline:none;margin-bottom:4px}}
input:focus{{border-color:#6366f1;box-shadow:0 0 0 3px rgba(99,102,241,.15)}}
button{{width:100%;padding:11px;background:#6366f1;color:#fff;border:none;
        border-radius:8px;font-size:1rem;font-weight:500;cursor:pointer;margin-top:16px}}
button:hover{{background:#4f46e5}}
.err{{color:#dc2626;font-size:.85rem;margin:0 0 14px}}
</style>
</head>
<body>
<div class="card">
  <h2>Intervals.icu MCP</h2>
  <p>Enter your <strong>coach_secret</strong> to connect Claude.ai to your training data.</p>
  {error}
  <form method="post" action="/authorize?{qs}">
    <input type="password" name="password" placeholder="Access password" autofocus required />
    <button type="submit">Connect</button>
  </form>
</div>
</body>
</html>"""


async def handle_authorize(request: Request) -> Response:
    params = dict(request.query_params)
    client_id    = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    state        = params.get("state", "")
    code_challenge        = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "S256")
    qs = html.escape(request.url.query)  # prevent attribute injection in form action

    if client_id not in _oauth_clients:
        return PlainTextResponse(
            "Unknown client_id — re-add the MCP server in Claude settings", status_code=400
        )
    if redirect_uri not in _oauth_clients[client_id]["redirect_uris"]:
        return PlainTextResponse("redirect_uri not registered", status_code=400)

    # PKCE S256 is mandatory — all compliant MCP clients send it
    if not code_challenge:
        return PlainTextResponse("code_challenge required (PKCE S256)", status_code=400)
    if code_challenge_method != "S256":
        return PlainTextResponse("only S256 code_challenge_method is supported", status_code=400)

    if request.method == "GET":
        return HTMLResponse(_LOGIN_TEMPLATE.format(error="", qs=qs), headers=_SECURITY_HEADERS)

    # POST — validate password
    ip = _get_ip(request)
    if _is_locked_out(ip):
        return HTMLResponse(
            _LOGIN_TEMPLATE.format(
                error='<p class="err">Too many failed attempts — try again in 1 hour.</p>',
                qs=qs,
            ),
            status_code=429,
            headers=_SECURITY_HEADERS,
        )

    form_data = await request.form()
    password = form_data.get("password", "")

    if not COACH_SECRET:
        return HTMLResponse(
            _LOGIN_TEMPLATE.format(
                error='<p class="err">No password configured — set coach_secret in addon settings.</p>',
                qs=qs,
            ),
            status_code=503,
            headers=_SECURITY_HEADERS,
        )

    if not _safe_eq(password, COACH_SECRET):
        _record_failure(ip)
        return HTMLResponse(
            _LOGIN_TEMPLATE.format(error='<p class="err">Incorrect password.</p>', qs=qs),
            status_code=401,
            headers=_SECURITY_HEADERS,
        )

    _clear_failures(ip)
    code = _gen_token(32)
    async with _oauth_lock:
        _oauth_codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "expires_at": _ts() + _CODE_EXPIRY,
        }

    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}"
    if state:
        location += f"&state={state}"
    return Response(status_code=302, headers={"Location": location})


async def handle_token(request: Request) -> Response:
    ip = _get_ip(request)
    if not _check_rate_limit(ip):
        return JSONResponse({"error": "too_many_requests"}, status_code=429)

    try:
        form_data = await request.form()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    grant_type   = form_data.get("grant_type", "")
    code         = form_data.get("code", "")
    redirect_uri = form_data.get("redirect_uri", "")
    client_id    = form_data.get("client_id", "")
    code_verifier = form_data.get("code_verifier", "")

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    async with _oauth_lock:
        _prune_oauth_codes()
        code_data = _oauth_codes.pop(code, None)  # single-use

    if not code_data:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if code_data["expires_at"] < _ts():
        return JSONResponse({"error": "invalid_grant", "error_description": "code expired"}, status_code=400)
    if code_data["client_id"] != client_id:
        return JSONResponse({"error": "invalid_client"}, status_code=400)
    if code_data["redirect_uri"] != redirect_uri:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    if code_data.get("code_challenge"):
        if not code_verifier:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "code_verifier required"},
                status_code=400,
            )
        if code_data.get("code_challenge_method", "S256") != "S256":
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "only S256 supported"},
                status_code=400,
            )
        if not _pkce_verify(code_verifier, code_data["code_challenge"]):
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "PKCE mismatch"},
                status_code=400,
            )

    token = _gen_token(32)
    async with _oauth_lock:
        _oauth_tokens[_hash_token(token)] = {
            "client_id": client_id,
            "expires_at": _ts() + _TOKEN_EXPIRY,
        }
        _save_tokens()
    log.info("OAuth token issued to client %s", client_id)
    return JSONResponse({
        "access_token": token,
        "token_type": "bearer",
        "expires_in": _TOKEN_EXPIRY,
    })


async def handle_revoke(request: Request) -> Response:
    """Revoke all active OAuth tokens. Requires X-Coach-Token header."""
    if not _check_header_token(request, "X-Coach-Token", COACH_SECRET):
        log.warning("Unauthorized /revoke from %s", _get_ip(request))
        return PlainTextResponse("Unauthorized", status_code=401)
    async with _oauth_lock:
        count = len(_oauth_tokens)
        _oauth_tokens.clear()
        _save_tokens()
    log.warning("All OAuth tokens revoked (%d tokens cleared)", count)
    return JSONResponse({"revoked": count})


# ---------------------------------------------------------------------------
# MCP rate limit + OAuth token middleware — applied only to /mcp path
# ---------------------------------------------------------------------------
class MCPAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/mcp"):
            return await call_next(request)

        ip = _get_ip(request)
        if not _check_rate_limit(ip):
            return PlainTextResponse("Too Many Requests", status_code=429)

        if not _validate_oauth_token(request):
            log.warning("Unauthorized /mcp from %s", ip)
            host = request.headers.get("host", "")
            resource_metadata = f"https://{host}/.well-known/oauth-protected-resource"
            return PlainTextResponse(
                "Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer realm="mcp", resource_metadata="{resource_metadata}"'},
            )

        # FastMCP's transport_security rejects external Host headers (DNS-rebinding
        # protection). Allowed list is ['localhost:*', '127.0.0.1:*'] — must include
        # the port. Rewrite after auth passes since the external hostname is legitimate.
        request.scope["headers"] = [
            (b"host", f"localhost:{PORT}".encode()) if k == b"host" else (k, v)
            for k, v in request.scope["headers"]
        ]

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
        event_ts   = event.get("timestamp", "")
        activity   = event.get("activity", {})
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
    distance_km  = round((activity.get("distance", 0) or 0) / 1000, 2)
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
    if not isinstance(activity_id, str) or (activity_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", activity_id)):
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
            "mcp_oauth": True,
            "token_expiry_days": _TOKEN_EXPIRY // 86400,
            "coach": bool(COACH_SECRET),
            "webhook": bool(WEBHOOK_SECRET),
        },
    })


# ---------------------------------------------------------------------------
# Lifespan — manage FastMCP session manager + shared httpx client
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: Starlette):
    _load_tokens()
    async with mcp_app.router.lifespan_context(app):
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

if not COACH_SECRET:
    log.error("COACH_SECRET not set — /mcp and /coach are UNAUTHENTICATED and open to the internet!")
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
        Route("/register", handle_register, methods=["POST"]),
        Route("/authorize", handle_authorize, methods=["GET", "POST"]),
        Route("/token", handle_token, methods=["POST"]),
        Route("/revoke", handle_revoke, methods=["POST"]),
        Route("/.well-known/oauth-authorization-server", handle_oauth_server_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", handle_oauth_resource_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp", handle_oauth_resource_metadata, methods=["GET"]),
        # Mount FastMCP last — it handles /mcp
        Mount("/", app=mcp_app),
    ],
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        ),
        Middleware(MCPAuthMiddleware),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    log.info("MCP endpoint:     http://0.0.0.0:%d/mcp  (OAuth: %s, token expiry: %d days)", PORT, "yes" if COACH_SECRET else "NO — set coach_secret!", _TOKEN_EXPIRY // 86400)
    log.info("Read-only mode:   %s (write tools %s)", READ_ONLY, "DISABLED" if READ_ONLY else "enabled")
    log.info("OAuth endpoints:  /authorize /token /register /.well-known/oauth-authorization-server")
    log.info("Webhook receiver: http://0.0.0.0:%d/webhook  (secret: %s)", PORT, "yes" if WEBHOOK_SECRET else "NO")
    log.info("Coach endpoint:   http://0.0.0.0:%d/coach  (auth: %s)", PORT, "yes" if COACH_SECRET else "NO")
    log.info("Health endpoint:  http://0.0.0.0:%d/health", PORT)
    log.info("Athlete ID: %s", ATHLETE_ID)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None)
