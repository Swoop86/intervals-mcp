"""
Unit tests for mcp_server.py — pure helpers, auth, rate limiting,
replay protection, and HTTP endpoints.
"""
import time
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import mcp_server
from mcp_server import (
    _safe_int, _safe_str, _safe_eq,
    _check_bearer, _check_header_token, _get_ip, _normalise_ip,
    _check_rate_limit, _prune_rate_buckets,
    _is_replay, _summarise_activity,
    _validate_cf_jwt,
    today_iso, days_ago_iso,
    RATE_BURST, app,
)
from starlette.requests import Request


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _mock_request(headers: dict | None = None, client_host: str = "1.2.3.4") -> Request:
    req = MagicMock(spec=Request)
    req.headers = headers or {}
    req.client = MagicMock()
    req.client.host = client_host
    return req


@pytest.fixture(autouse=True)
def reset_rate_buckets():
    mcp_server._rate_buckets.clear()
    yield
    mcp_server._rate_buckets.clear()


@pytest.fixture(autouse=True)
def reset_replay_cache():
    mcp_server._seen_event_ids.clear()
    yield
    mcp_server._seen_event_ids.clear()


@pytest.fixture
async def client():
    # raise_app_exceptions=False turns unhandled ASGI exceptions into 500 responses
    # instead of propagating — needed because FastMCP requires lifespan to be running,
    # which httpx.ASGITransport does not invoke.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# _safe_int
# ---------------------------------------------------------------------------

class TestSafeInt:
    def test_valid_number(self):
        assert _safe_int("42") == 42

    def test_null_string_returns_default(self):
        assert _safe_int("null") == 0

    def test_none_string_returns_default(self):
        assert _safe_int("none") == 0

    def test_empty_string_returns_default(self):
        assert _safe_int("") == 0

    def test_none_value_returns_default(self):
        assert _safe_int(None) == 0

    def test_custom_default(self):
        assert _safe_int("null", 99) == 99

    def test_invalid_string_returns_default(self):
        assert _safe_int("abc") == 0

    def test_whitespace_null(self):
        assert _safe_int("  NULL  ") == 0

    def test_zero(self):
        assert _safe_int("0") == 0


# ---------------------------------------------------------------------------
# _safe_str
# ---------------------------------------------------------------------------

class TestSafeStr:
    def test_valid_string(self):
        assert _safe_str("hello") == "hello"

    def test_null_string_returns_empty(self):
        assert _safe_str("null") == ""

    def test_none_string_returns_empty(self):
        assert _safe_str("none") == ""

    def test_none_value_returns_empty(self):
        assert _safe_str(None) == ""

    def test_empty_string_returns_empty(self):
        assert _safe_str("") == ""

    def test_strips_whitespace(self):
        assert _safe_str("  hello  ") == "hello"

    def test_null_uppercase(self):
        assert _safe_str("NULL") == ""


# ---------------------------------------------------------------------------
# _safe_eq
# ---------------------------------------------------------------------------

class TestSafeEq:
    def test_equal_strings(self):
        assert _safe_eq("secret", "secret") is True

    def test_different_strings(self):
        assert _safe_eq("secret", "wrong") is False

    def test_empty_strings_equal(self):
        assert _safe_eq("", "") is True

    def test_different_lengths(self):
        assert _safe_eq("abc", "abcd") is False

    def test_case_sensitive(self):
        assert _safe_eq("Secret", "secret") is False


# ---------------------------------------------------------------------------
# _check_bearer
# ---------------------------------------------------------------------------

class TestCheckBearer:
    def test_no_secret_always_passes(self):
        req = _mock_request({"authorization": "Bearer wrong"})
        assert _check_bearer(req, "") is True

    def test_valid_bearer_token(self):
        req = _mock_request({"authorization": "Bearer test_mcp_token"})
        assert _check_bearer(req, "test_mcp_token") is True

    def test_wrong_bearer_token(self):
        req = _mock_request({"authorization": "Bearer wrong"})
        assert _check_bearer(req, "test_mcp_token") is False

    def test_missing_authorization_header(self):
        req = _mock_request({})
        assert _check_bearer(req, "test_mcp_token") is False

    def test_non_bearer_scheme_rejected(self):
        req = _mock_request({"authorization": "Basic dGVzdA=="})
        assert _check_bearer(req, "test_mcp_token") is False


# ---------------------------------------------------------------------------
# _check_header_token
# ---------------------------------------------------------------------------

class TestCheckHeaderToken:
    def test_no_secret_always_passes(self):
        req = _mock_request({"x-coach-token": "anything"})
        assert _check_header_token(req, "X-Coach-Token", "") is True

    def test_valid_header_token(self):
        req = _mock_request({"x-coach-token": "mysecret"})
        assert _check_header_token(req, "X-Coach-Token", "mysecret") is True

    def test_wrong_header_token(self):
        req = _mock_request({"x-coach-token": "wrong"})
        assert _check_header_token(req, "X-Coach-Token", "mysecret") is False

    def test_missing_header_fails(self):
        req = _mock_request({})
        assert _check_header_token(req, "X-Coach-Token", "mysecret") is False


# ---------------------------------------------------------------------------
# _get_ip
# ---------------------------------------------------------------------------

class TestNormaliseIp:
    def test_ipv4_unchanged(self):
        assert _normalise_ip("1.2.3.4") == "1.2.3.4"

    def test_ipv6_already_canonical(self):
        assert _normalise_ip("2606:4700::1") == "2606:4700::1"

    def test_ipv6_expanded_compressed(self):
        assert _normalise_ip("2606:4700:0000:0000:0000:0000:0000:0001") == "2606:4700::1"

    def test_ipv6_loopback_variants_equal(self):
        assert _normalise_ip("0:0:0:0:0:0:0:1") == _normalise_ip("::1")

    def test_brackets_stripped(self):
        assert _normalise_ip("[::1]") == "::1"

    def test_invalid_string_returned_as_is(self):
        assert _normalise_ip("unknown") == "unknown"


class TestGetIp:
    def test_cloudflare_header_wins(self):
        req = _mock_request({"cf-connecting-ip": "1.1.1.1", "x-forwarded-for": "2.2.2.2"})
        assert _get_ip(req) == "1.1.1.1"

    def test_x_forwarded_for_first_entry(self):
        req = _mock_request({"x-forwarded-for": "3.3.3.3, 4.4.4.4"})
        assert _get_ip(req) == "3.3.3.3"

    def test_client_host_fallback(self):
        req = _mock_request({}, client_host="5.5.5.5")
        assert _get_ip(req) == "5.5.5.5"

    def test_ipv6_cf_header_canonical(self):
        req = _mock_request({"cf-connecting-ip": "2606:4700::1"})
        assert _get_ip(req) == "2606:4700::1"

    def test_ipv6_cf_header_expanded_compressed(self):
        req = _mock_request({"cf-connecting-ip": "2606:4700:0000:0000:0000:0000:0000:0001"})
        assert _get_ip(req) == "2606:4700::1"

    def test_ipv6_xff_brackets_stripped(self):
        req = _mock_request({"x-forwarded-for": "[::1], 10.0.0.1"})
        assert _get_ip(req) == "::1"

    def test_ipv6_xff_bare(self):
        req = _mock_request({"x-forwarded-for": "::1, 10.0.0.1"})
        assert _get_ip(req) == "::1"


# ---------------------------------------------------------------------------
# _check_rate_limit
# ---------------------------------------------------------------------------

class TestCheckRateLimit:
    def test_allows_up_to_burst_limit(self):
        for _ in range(RATE_BURST):
            assert _check_rate_limit("10.0.0.1") is True

    def test_rejects_when_burst_exhausted(self):
        ip = "10.0.0.2"
        for _ in range(RATE_BURST):
            _check_rate_limit(ip)
        assert _check_rate_limit(ip) is False

    def test_different_ips_are_independent(self):
        for i in range(RATE_BURST):
            _check_rate_limit(f"10.0.{i}.1")
        # Each IP used only 1 token — all should still have tokens
        for i in range(RATE_BURST):
            assert _check_rate_limit(f"10.0.{i}.1") is True

    def test_ipv6_expanded_and_compressed_share_same_bucket(self):
        # Exhaust the bucket via the compressed form
        ip_compressed = _normalise_ip("::1")
        for _ in range(RATE_BURST):
            _check_rate_limit(ip_compressed)
        # Expanded form normalises to the same key — bucket must already be exhausted
        ip_expanded = _normalise_ip("0:0:0:0:0:0:0:1")
        assert ip_compressed == ip_expanded
        assert _check_rate_limit(ip_expanded) is False


# ---------------------------------------------------------------------------
# _prune_rate_buckets
# ---------------------------------------------------------------------------

class TestPruneRateBuckets:
    def test_removes_stale_bucket(self):
        ip = "stale.host"
        mcp_server._rate_buckets[ip] = {"tokens": 5.0, "last": time.monotonic() - 4000}
        _prune_rate_buckets()
        assert ip not in mcp_server._rate_buckets

    def test_keeps_fresh_bucket(self):
        ip = "fresh.host"
        mcp_server._rate_buckets[ip] = {"tokens": 5.0, "last": time.monotonic()}
        _prune_rate_buckets()
        assert ip in mcp_server._rate_buckets


# ---------------------------------------------------------------------------
# _is_replay
# ---------------------------------------------------------------------------

class TestIsReplay:
    def test_fresh_event_passes(self):
        ts = datetime.now(timezone.utc).isoformat()
        assert _is_replay("event_a", ts) is False

    def test_duplicate_event_rejected(self):
        ts = datetime.now(timezone.utc).isoformat()
        _is_replay("event_b", ts)
        assert _is_replay("event_b", ts) is True

    def test_stale_timestamp_rejected(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
        assert _is_replay("event_c", old_ts) is True

    def test_future_timestamp_rejected(self):
        future_ts = (datetime.now(timezone.utc) + timedelta(seconds=400)).isoformat()
        assert _is_replay("event_d", future_ts) is True

    def test_empty_event_id_never_cached(self):
        ts = datetime.now(timezone.utc).isoformat()
        assert _is_replay("", ts) is False
        assert _is_replay("", ts) is False  # no cache entry for empty id


# ---------------------------------------------------------------------------
# _summarise_activity
# ---------------------------------------------------------------------------

class TestSummariseActivity:
    SAMPLE = {
        "id": "abc123",
        "start_date_local": "2024-01-15T07:30:00",
        "name": "Morning Run",
        "type": "Run",
        "moving_time": 3600,
        "distance": 10000,
        "icu_training_load": 55,
        "average_heartrate": 145,
        "icu_average_speed": 2.78,
        "average_watts": None,
        "total_elevation_gain": 50,
        "icu_ctl": 45.2,
        "icu_atl": 38.1,
        "icu_tsb": 7.1,
    }

    def test_basic_field_extraction(self):
        r = _summarise_activity(self.SAMPLE)
        assert r["id"] == "abc123"
        assert r["name"] == "Morning Run"
        assert r["type"] == "Run"
        assert r["tss"] == 55
        assert r["avg_hr"] == 145
        assert r["ctl"] == 45.2

    def test_date_truncated_to_iso_date(self):
        r = _summarise_activity(self.SAMPLE)
        assert r["date"] == "2024-01-15"

    def test_duration_converted_to_minutes(self):
        r = _summarise_activity(self.SAMPLE)
        assert r["duration_min"] == 60.0

    def test_distance_converted_to_km(self):
        r = _summarise_activity(self.SAMPLE)
        assert r["distance_km"] == 10.0

    def test_none_distance_becomes_zero(self):
        r = _summarise_activity({"distance": None, "moving_time": 0})
        assert r["distance_km"] == 0.0

    def test_duration_rounding(self):
        r = _summarise_activity({"moving_time": 3661})
        assert r["duration_min"] == 61.0


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

class TestDateHelpers:
    def test_today_iso_is_valid_date(self):
        datetime.strptime(today_iso(), "%Y-%m-%d")

    def test_days_ago_iso_is_in_the_past(self):
        assert days_ago_iso(7) < today_iso()

    def test_days_ago_iso_format(self):
        datetime.strptime(days_ago_iso(30), "%Y-%m-%d")


# ---------------------------------------------------------------------------
# /health endpoint
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    async def test_returns_200(self, client):
        r = await client.get("/health")
        assert r.status_code == 200

    async def test_response_body_structure(self, client):
        body = (await client.get("/health")).json()
        assert body["status"] == "ok"
        assert "time" in body
        assert "auth" in body

    async def test_auth_flags_reflect_config(self, client):
        auth = (await client.get("/health")).json()["auth"]
        assert auth["mcp_cf_access"] is False  # CF_ACCESS_AUD="" in tests
        assert auth["coach"] is True
        assert auth["webhook"] is True
        assert auth["webhook_header"] is False  # INTERVALS_WEBHOOK_HEADER_SECRET=""


# ---------------------------------------------------------------------------
# /webhook endpoint
# ---------------------------------------------------------------------------

class TestWebhookEndpoint:
    async def test_body_too_large_returns_413(self, client):
        r = await client.post("/webhook", content=b"x" * (mcp_server.MAX_BODY_BYTES + 1))
        assert r.status_code == 413

    async def test_bad_json_returns_400(self, client):
        r = await client.post("/webhook", content=b"not json at all")
        assert r.status_code == 400

    async def test_wrong_payload_secret_silent_reject(self, client):
        r = await client.post("/webhook", json={"secret": "WRONG", "events": []})
        assert r.status_code == 200
        assert r.text == "OK"

    async def test_valid_empty_events(self, client):
        r = await client.post(
            "/webhook", json={"secret": "test_webhook_secret", "events": []}
        )
        assert r.status_code == 200

    async def test_stale_timestamp_silently_dropped(self, client):
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
        payload = {
            "secret": "test_webhook_secret",
            "events": [{"type": "ACTIVITY_UPLOADED", "timestamp": old_ts,
                         "activity": {"id": "a1"}}],
        }
        r = await client.post("/webhook", json=payload)
        assert r.status_code == 200

    async def test_activity_uploaded_event_returns_ok(self, client):
        ts = datetime.now(timezone.utc).isoformat()
        payload = {
            "secret": "test_webhook_secret",
            "events": [{
                "type": "ACTIVITY_UPLOADED", "timestamp": ts,
                "activity": {"id": "upload_1", "name": "Run", "type": "Run",
                             "moving_time": 2700, "distance": 8000,
                             "icu_training_load": 40},
            }],
        }
        with patch.object(mcp_server, "ha_notify", new=AsyncMock()), \
             patch.object(mcp_server, "ha_fire_event", new=AsyncMock()):
            r = await client.post("/webhook", json=payload)
        assert r.status_code == 200

    async def test_wrong_header_secret_silent_reject(self, client):
        with patch.object(mcp_server, "WEBHOOK_HEADER_SECRET", "expected_secret"):
            r = await client.post(
                "/webhook",
                json={"secret": "test_webhook_secret", "events": []},
                headers={"X-Webhook-Auth": "wrong_header_secret"},
            )
        assert r.status_code == 200
        assert r.text == "OK"

    async def test_correct_header_secret_proceeds(self, client):
        with patch.object(mcp_server, "WEBHOOK_HEADER_SECRET", "correct_header"):
            r = await client.post(
                "/webhook",
                json={"secret": "test_webhook_secret", "events": []},
                headers={"X-Webhook-Auth": "correct_header"},
            )
        assert r.status_code == 200

    async def test_replay_protection_drops_duplicate(self, client):
        ts = datetime.now(timezone.utc).isoformat()
        payload = {
            "secret": "test_webhook_secret",
            "events": [{
                "type": "ACTIVITY_UPLOADED", "timestamp": ts,
                "activity": {"id": "dup_act"},
            }],
        }
        with patch.object(mcp_server, "ha_notify", new=AsyncMock()), \
             patch.object(mcp_server, "ha_fire_event", new=AsyncMock()):
            await client.post("/webhook", json=payload)
            # Second identical post — same event_id:type:timestamp key
            r2 = await client.post("/webhook", json=payload)
        assert r2.status_code == 200  # Still 200, just dropped silently


# ---------------------------------------------------------------------------
# /coach endpoint
# ---------------------------------------------------------------------------

class TestCoachEndpoint:
    async def test_missing_auth_returns_401(self, client):
        r = await client.post("/coach", json={"activity_id": "abc"})
        assert r.status_code == 401

    async def test_wrong_auth_returns_401(self, client):
        r = await client.post(
            "/coach", json={"activity_id": "abc"},
            headers={"X-Coach-Token": "WRONG"},
        )
        assert r.status_code == 401

    async def test_body_too_large_returns_413(self, client):
        r = await client.post(
            "/coach",
            content=b"x" * (mcp_server.MAX_BODY_BYTES + 1),
            headers={"X-Coach-Token": "test_coach_secret"},
        )
        assert r.status_code == 413

    async def test_activity_id_too_long_returns_400(self, client):
        r = await client.post(
            "/coach",
            json={"activity_id": "x" * 51},
            headers={"X-Coach-Token": "test_coach_secret"},
        )
        assert r.status_code == 400

    async def test_non_string_activity_id_returns_400(self, client):
        r = await client.post(
            "/coach",
            json={"activity_id": 12345},
            headers={"X-Coach-Token": "test_coach_secret"},
        )
        assert r.status_code == 400

    async def test_valid_request_calls_coaching_flow(self, client):
        mock_result = {"status": "ok", "alert_level": "green", "adjustments_applied": 0}
        import claude_coach
        with patch.object(claude_coach, "run_coaching_flow", new=AsyncMock(return_value=mock_result)):
            r = await client.post(
                "/coach",
                json={"activity_id": "act123"},
                headers={"X-Coach-Token": "test_coach_secret"},
            )
        assert r.status_code == 200
        assert r.json()["alert_level"] == "green"

    async def test_empty_body_uses_empty_activity_id(self, client):
        mock_result = {"status": "ok", "alert_level": "green", "adjustments_applied": 0}
        import claude_coach
        with patch.object(claude_coach, "run_coaching_flow", new=AsyncMock(return_value=mock_result)):
            r = await client.post(
                "/coach",
                content=b"",
                headers={"X-Coach-Token": "test_coach_secret"},
            )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# CF Access JWT validation
# ---------------------------------------------------------------------------

class TestValidateCfJwt:
    def test_no_aud_configured_skips_check(self):
        # CF_ACCESS_AUD is "" in tests — validation is bypassed
        assert _validate_cf_jwt("") is True
        assert _validate_cf_jwt("any_token") is True

    def test_with_aud_empty_token_rejected(self):
        with patch.object(mcp_server, "CF_ACCESS_AUD", "test-aud"):
            assert _validate_cf_jwt("") is False

    def test_with_aud_no_jwks_loaded_rejected(self):
        with patch.object(mcp_server, "CF_ACCESS_AUD", "test-aud"), \
             patch.object(mcp_server, "_cf_jwks", []):
            assert _validate_cf_jwt("some.jwt.token") is False

    def test_expired_jwt_rejected(self):
        import jwt as pyjwt
        fake_key = {"kty": "RSA", "n": "x", "e": "AQAB"}
        with patch.object(mcp_server, "CF_ACCESS_AUD", "test-aud"), \
             patch.object(mcp_server, "_cf_jwks", [fake_key]), \
             patch("mcp_server.pyjwt.algorithms.RSAAlgorithm.from_jwk", return_value=MagicMock()), \
             patch("mcp_server.pyjwt.decode", side_effect=pyjwt.ExpiredSignatureError("expired")):
            assert _validate_cf_jwt("expired.jwt.token") is False

    def test_wrong_audience_rejected(self):
        import jwt as pyjwt
        fake_key = {"kty": "RSA"}
        with patch.object(mcp_server, "CF_ACCESS_AUD", "test-aud"), \
             patch.object(mcp_server, "_cf_jwks", [fake_key]), \
             patch("mcp_server.pyjwt.algorithms.RSAAlgorithm.from_jwk", return_value=MagicMock()), \
             patch("mcp_server.pyjwt.decode", side_effect=pyjwt.InvalidAudienceError("wrong aud")):
            assert _validate_cf_jwt("wrong.aud.token") is False

    def test_valid_jwt_accepted(self):
        fake_key = {"kty": "RSA"}
        with patch.object(mcp_server, "CF_ACCESS_AUD", "test-aud"), \
             patch.object(mcp_server, "_cf_jwks", [fake_key]), \
             patch("mcp_server.pyjwt.algorithms.RSAAlgorithm.from_jwk", return_value=MagicMock()), \
             patch("mcp_server.pyjwt.decode", return_value={"sub": "user@example.com"}):
            assert _validate_cf_jwt("valid.jwt.token") is True


# ---------------------------------------------------------------------------
# /mcp auth middleware (CF Access JWT — disabled when CF_ACCESS_AUD="")
# ---------------------------------------------------------------------------

class TestMcpAuthMiddleware:
    async def test_no_cf_aud_configured_passes_all(self, client):
        # CF_ACCESS_AUD="" in tests → JWT check skipped → FastMCP handles it
        r = await client.post(
            "/mcp",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code != 401  # middleware passes; FastMCP may return other codes

    async def test_cf_aud_set_missing_jwt_returns_401(self, client):
        with patch.object(mcp_server, "CF_ACCESS_AUD", "test-aud"), \
             patch.object(mcp_server, "_cf_jwks", [{"kty": "RSA"}]):
            r = await client.post("/mcp", json={})
        assert r.status_code == 401

    async def test_cf_aud_set_valid_jwt_passes_middleware(self, client):
        with patch.object(mcp_server, "CF_ACCESS_AUD", "test-aud"), \
             patch.object(mcp_server, "_cf_jwks", [{"kty": "RSA"}]), \
             patch("mcp_server.pyjwt.algorithms.RSAAlgorithm.from_jwk", return_value=MagicMock()), \
             patch("mcp_server.pyjwt.decode", return_value={"sub": "me@example.com"}):
            r = await client.post(
                "/mcp",
                content=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "CF-Access-Jwt-Assertion": "valid.jwt.token",
                },
            )
        assert r.status_code != 401


# ---------------------------------------------------------------------------
# MCP tools — review_training, update_workout, delete_workout
# ---------------------------------------------------------------------------

class TestReviewTrainingTool:
    async def test_returns_context_dict(self):
        fake_context = {
            "athlete_id": "i999999",
            "recent_activities": [],
            "wellness": {},
        }
        with patch("claude_coach.fetch_context", new=AsyncMock(return_value=fake_context)):
            from mcp_server import review_training
            result = await review_training()
        assert result == fake_context

    async def test_passes_activity_id(self):
        fake_context = {"athlete_id": "i999999", "activity": {"id": "act123"}}
        captured = {}

        async def mock_fetch(client, activity_id=""):
            captured["activity_id"] = activity_id
            return fake_context

        with patch("claude_coach.fetch_context", new=mock_fetch):
            from mcp_server import review_training
            await review_training(activity_id="act123")

        assert captured["activity_id"] == "act123"

    async def test_empty_activity_id_default(self):
        captured = {}

        async def mock_fetch(client, activity_id=""):
            captured["activity_id"] = activity_id
            return {}

        with patch("claude_coach.fetch_context", new=mock_fetch):
            from mcp_server import review_training
            await review_training()

        assert captured["activity_id"] == ""


class TestUpdateWorkoutTool:
    async def test_calls_icu_put_with_correct_path(self):
        import mcp_server
        put_response = {"id": 99, "name": "Easy Run"}

        with patch.object(mcp_server, "icu_put", new=AsyncMock(return_value=put_response)) as mock_put:
            from mcp_server import update_workout
            result = await update_workout(
                event_id=99,
                name="Easy Run",
                description="Recovery jog",
                moving_time=3600,
                target_tss=40,
                date="2026-04-21",
            )

        mock_put.assert_called_once()
        call_path = mock_put.call_args[0][0]
        assert "events/99" in call_path
        assert result == put_response

    async def test_no_fields_returns_error(self):
        import mcp_server
        from mcp_server import update_workout
        result = await update_workout(event_id=1)
        assert "error" in result

    async def test_optional_fields_omit_none(self):
        import mcp_server

        captured_payload: dict = {}

        async def fake_put(path: str, payload: dict):
            captured_payload.update(payload)
            return {}

        with patch.object(mcp_server, "icu_put", new=fake_put):
            from mcp_server import update_workout
            await update_workout(event_id=1, name="Run")

        assert "name" in captured_payload
        assert "description" not in captured_payload
        assert "moving_time" not in captured_payload


class TestDeleteWorkoutTool:
    async def test_calls_icu_delete_with_correct_path(self):
        import mcp_server

        with patch.object(mcp_server, "icu_delete", new=AsyncMock()) as mock_del:
            from mcp_server import delete_workout
            result = await delete_workout(event_id=42)

        mock_del.assert_called_once()
        call_path = mock_del.call_args[0][0]
        assert "events/42" in call_path
        assert result == {"status": "deleted", "event_id": 42}
