"""
Unit tests for mcp_server.py — pure helpers, auth, rate limiting,
replay protection, OAuth 2.1, and HTTP endpoints.
"""
import base64
import hashlib
import secrets
import time
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import mcp_server
from mcp_server import (
    _safe_int, _safe_str, _safe_eq,
    _check_header_token, _get_ip, _normalise_ip,
    _check_rate_limit, _prune_rate_buckets,
    _is_replay, _summarise_activity,
    _validate_oauth_token, _pkce_verify, _ts, _hash_token,
    _is_locked_out, _record_failure, _clear_failures,
    today_iso, days_ago_iso,
    RATE_BURST, app,
)
from starlette.requests import Request


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_request(headers: dict | None = None, client_host: str = "1.2.3.4") -> Request:
    req = MagicMock(spec=Request)
    req.headers = headers or {}
    req.client = MagicMock()
    req.client.host = client_host
    return req


def _make_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


@pytest.fixture(autouse=True)
def reset_oauth_state():
    mcp_server._oauth_clients.clear()
    mcp_server._oauth_codes.clear()
    mcp_server._oauth_tokens.clear()
    yield
    mcp_server._oauth_clients.clear()
    mcp_server._oauth_codes.clear()
    mcp_server._oauth_tokens.clear()


@pytest.fixture(autouse=True)
def reset_authorize_failures():
    mcp_server._authorize_failures.clear()
    yield
    mcp_server._authorize_failures.clear()


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def registered_client():
    """Pre-register an OAuth client and return its client_id."""
    client_id = "test_client_id_fixture"
    mcp_server._oauth_clients[client_id] = {
        "redirect_uris": ["https://claude.ai/oauth/callback"],
        "client_name": "Test Client",
    }
    return client_id


@pytest.fixture
def valid_token():
    """Insert a live OAuth token into the token store (keyed by hash) and return the plaintext token."""
    token = "test_valid_bearer_token"
    mcp_server._oauth_tokens[_hash_token(token)] = {
        "client_id": "test_client",
        "expires_at": _ts() + 3600,
    }
    return token


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
# _get_ip / _normalise_ip
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
        for i in range(RATE_BURST):
            assert _check_rate_limit(f"10.0.{i}.1") is True

    def test_ipv6_expanded_and_compressed_share_same_bucket(self):
        ip_compressed = _normalise_ip("::1")
        for _ in range(RATE_BURST):
            _check_rate_limit(ip_compressed)
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
        assert _is_replay("", ts) is False


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
# Login lockout helpers
# ---------------------------------------------------------------------------

class TestLoginLockout:
    def test_unknown_ip_not_locked(self):
        assert _is_locked_out("1.2.3.4") is False

    def test_below_threshold_not_locked(self):
        ip = "10.0.0.1"
        for _ in range(mcp_server._MAX_LOGIN_FAILURES - 1):
            _record_failure(ip)
        assert _is_locked_out(ip) is False

    def test_locked_after_max_failures(self):
        ip = "10.0.0.2"
        for _ in range(mcp_server._MAX_LOGIN_FAILURES):
            _record_failure(ip)
        assert _is_locked_out(ip) is True

    def test_clear_removes_lockout(self):
        ip = "10.0.0.3"
        for _ in range(mcp_server._MAX_LOGIN_FAILURES):
            _record_failure(ip)
        _clear_failures(ip)
        assert _is_locked_out(ip) is False

    def test_expired_lock_not_locked(self):
        ip = "10.0.0.4"
        mcp_server._authorize_failures[ip] = {"count": 10, "locked_until": _ts() - 1}
        assert _is_locked_out(ip) is False
        assert ip not in mcp_server._authorize_failures  # evicted

    def test_future_lock_is_locked(self):
        ip = "10.0.0.5"
        mcp_server._authorize_failures[ip] = {"count": 5, "locked_until": _ts() + 3600}
        assert _is_locked_out(ip) is True


# ---------------------------------------------------------------------------
# _pkce_verify
# ---------------------------------------------------------------------------

class TestPkceVerify:
    def test_correct_verifier_passes(self):
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = _make_code_challenge(verifier)
        assert _pkce_verify(verifier, challenge) is True

    def test_wrong_verifier_fails(self):
        verifier = "correct_verifier"
        challenge = _make_code_challenge(verifier)
        assert _pkce_verify("wrong_verifier", challenge) is False

    def test_empty_strings_fail(self):
        assert _pkce_verify("", "") is False


# ---------------------------------------------------------------------------
# _validate_oauth_token
# ---------------------------------------------------------------------------

class TestValidateOauthToken:
    def test_no_coach_secret_allows_all(self):
        req = _mock_request({})
        with patch.object(mcp_server, "COACH_SECRET", ""):
            assert _validate_oauth_token(req) is True

    def test_missing_auth_header_rejected(self):
        req = _mock_request({})
        with patch.object(mcp_server, "COACH_SECRET", "test_coach_secret"):
            assert _validate_oauth_token(req) is False

    def test_non_bearer_scheme_rejected(self):
        req = _mock_request({"authorization": "Basic dGVzdA=="})
        with patch.object(mcp_server, "COACH_SECRET", "test_coach_secret"):
            assert _validate_oauth_token(req) is False

    def test_unknown_token_rejected(self):
        req = _mock_request({"authorization": "Bearer notavalidtoken"})
        with patch.object(mcp_server, "COACH_SECRET", "test_coach_secret"):
            assert _validate_oauth_token(req) is False

    def test_valid_token_accepted(self, valid_token):
        req = _mock_request({"authorization": f"Bearer {valid_token}"})
        with patch.object(mcp_server, "COACH_SECRET", "test_coach_secret"):
            assert _validate_oauth_token(req) is True

    def test_expired_token_rejected(self):
        token = "expired_token"
        mcp_server._oauth_tokens[_hash_token(token)] = {
            "client_id": "test",
            "expires_at": _ts() - 1,
        }
        req = _mock_request({"authorization": f"Bearer {token}"})
        with patch.object(mcp_server, "COACH_SECRET", "test_coach_secret"):
            assert _validate_oauth_token(req) is False
        assert _hash_token(token) not in mcp_server._oauth_tokens  # evicted

    def test_expired_token_evicted_from_store(self):
        token = "will_be_evicted"
        mcp_server._oauth_tokens[_hash_token(token)] = {
            "client_id": "test",
            "expires_at": _ts() - 100,
        }
        req = _mock_request({"authorization": f"Bearer {token}"})
        with patch.object(mcp_server, "COACH_SECRET", "test_coach_secret"):
            _validate_oauth_token(req)
        assert _hash_token(token) not in mcp_server._oauth_tokens


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

    async def test_auth_flags_present(self, client):
        auth = (await client.get("/health")).json()["auth"]
        assert auth["mcp_oauth"] is True
        assert auth["coach"] is True   # COACH_SECRET set in conftest
        assert auth["webhook"] is True  # WEBHOOK_SECRET set in conftest


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
            r2 = await client.post("/webhook", json=payload)
        assert r2.status_code == 200


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
# /mcp auth middleware (OAuth token)
# ---------------------------------------------------------------------------

class TestMcpAuthMiddleware:
    async def test_no_token_returns_401(self, client):
        r = await client.post("/mcp", content=b"{}", headers={"Content-Type": "application/json"})
        assert r.status_code == 401

    async def test_invalid_token_returns_401(self, client):
        r = await client.post(
            "/mcp",
            content=b"{}",
            headers={"Content-Type": "application/json", "Authorization": "Bearer bogus_token"},
        )
        assert r.status_code == 401

    async def test_valid_token_passes_middleware(self, client, valid_token):
        r = await client.post(
            "/mcp",
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {valid_token}",
            },
        )
        assert r.status_code != 401

    async def test_no_coach_secret_allows_all(self, client):
        with patch.object(mcp_server, "COACH_SECRET", ""):
            r = await client.post(
                "/mcp",
                content=b"{}",
                headers={"Content-Type": "application/json"},
            )
        assert r.status_code != 401

    async def test_401_includes_www_authenticate_header(self, client):
        r = await client.post("/mcp", content=b"{}", headers={"Content-Type": "application/json"})
        assert r.status_code == 401
        assert "www-authenticate" in r.headers
        assert "Bearer" in r.headers["www-authenticate"]
        assert "resource_metadata" in r.headers["www-authenticate"]


# ---------------------------------------------------------------------------
# /.well-known/oauth-authorization-server
# ---------------------------------------------------------------------------

class TestOAuthServerMetadata:
    async def test_returns_200(self, client):
        r = await client.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200

    async def test_required_fields_present(self, client):
        body = (await client.get("/.well-known/oauth-authorization-server")).json()
        assert "issuer" in body
        assert "authorization_endpoint" in body
        assert "token_endpoint" in body
        assert "registration_endpoint" in body

    async def test_endpoints_are_same_origin(self, client):
        body = (await client.get("/.well-known/oauth-authorization-server")).json()
        issuer = body["issuer"]
        assert body["authorization_endpoint"].startswith(issuer)
        assert body["token_endpoint"].startswith(issuer)
        assert body["registration_endpoint"].startswith(issuer)

    async def test_pkce_s256_supported(self, client):
        body = (await client.get("/.well-known/oauth-authorization-server")).json()
        assert "S256" in body.get("code_challenge_methods_supported", [])

    async def test_authorization_code_grant_supported(self, client):
        body = (await client.get("/.well-known/oauth-authorization-server")).json()
        assert "authorization_code" in body.get("grant_types_supported", [])

    async def test_cors_preflight_returns_allow_origin(self, client):
        r = await client.options(
            "/.well-known/oauth-authorization-server",
            headers={"Origin": "https://claude.ai", "Access-Control-Request-Method": "GET"},
        )
        assert r.status_code in (200, 204)
        assert "access-control-allow-origin" in r.headers


# ---------------------------------------------------------------------------
# /.well-known/oauth-protected-resource
# ---------------------------------------------------------------------------

class TestOAuthResourceMetadata:
    async def test_returns_200(self, client):
        r = await client.get("/.well-known/oauth-protected-resource")
        assert r.status_code == 200

    async def test_resource_and_auth_server_point_to_self(self, client):
        body = (await client.get("/.well-known/oauth-protected-resource")).json()
        assert "resource" in body
        assert "authorization_servers" in body
        # Both should be same host (self-contained auth server)
        resource = body["resource"]
        assert all(s == resource for s in body["authorization_servers"])

    async def test_authorization_servers_is_list(self, client):
        body = (await client.get("/.well-known/oauth-protected-resource")).json()
        assert isinstance(body["authorization_servers"], list)
        assert len(body["authorization_servers"]) == 1


# ---------------------------------------------------------------------------
# /register endpoint (RFC 7591 dynamic client registration)
# ---------------------------------------------------------------------------

class TestRegisterEndpoint:
    async def test_valid_registration_returns_201(self, client):
        r = await client.post("/register", json={
            "redirect_uris": ["https://claude.ai/oauth/callback"],
            "client_name": "Claude.ai",
        })
        assert r.status_code == 201

    async def test_response_contains_client_id(self, client):
        r = await client.post("/register", json={
            "redirect_uris": ["https://claude.ai/oauth/callback"],
        })
        body = r.json()
        assert "client_id" in body
        assert isinstance(body["client_id"], str)
        assert len(body["client_id"]) > 0

    async def test_client_stored_in_state(self, client):
        r = await client.post("/register", json={
            "redirect_uris": ["https://example.com/cb"],
            "client_name": "Test",
        })
        client_id = r.json()["client_id"]
        assert client_id in mcp_server._oauth_clients
        assert "https://example.com/cb" in mcp_server._oauth_clients[client_id]["redirect_uris"]

    async def test_missing_redirect_uris_returns_400(self, client):
        r = await client.post("/register", json={"client_name": "NoRedirect"})
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_redirect_uri"

    async def test_empty_redirect_uris_returns_400(self, client):
        r = await client.post("/register", json={"redirect_uris": []})
        assert r.status_code == 400

    async def test_invalid_json_returns_400(self, client):
        r = await client.post("/register", content=b"not json", headers={"Content-Type": "application/json"})
        assert r.status_code == 400

    async def test_multiple_redirect_uris_accepted(self, client):
        uris = ["https://a.example.com/cb", "https://b.example.com/cb"]
        r = await client.post("/register", json={"redirect_uris": uris})
        assert r.status_code == 201
        assert r.json()["redirect_uris"] == uris

    async def test_each_registration_gets_unique_client_id(self, client):
        r1 = await client.post("/register", json={"redirect_uris": ["https://a.com/cb"]})
        r2 = await client.post("/register", json={"redirect_uris": ["https://b.com/cb"]})
        assert r1.json()["client_id"] != r2.json()["client_id"]

    async def test_registration_cap_returns_503(self, client):
        with patch.object(mcp_server, "_MAX_CLIENTS", 2):
            await client.post("/register", json={"redirect_uris": ["https://a.com/cb"]})
            await client.post("/register", json={"redirect_uris": ["https://b.com/cb"]})
            r = await client.post("/register", json={"redirect_uris": ["https://c.com/cb"]})
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# /authorize endpoint
# ---------------------------------------------------------------------------

class TestAuthorizeEndpoint:
    async def test_get_unknown_client_returns_400(self, client):
        r = await client.get("/authorize?client_id=unknown&redirect_uri=https://x.com/cb")
        assert r.status_code == 400

    async def test_get_known_client_returns_login_form(self, client, registered_client):
        r = await client.get(
            f"/authorize?client_id={registered_client}"
            f"&redirect_uri=https://claude.ai/oauth/callback"
            f"&response_type=code&state=abc",
        )
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "password" in r.text.lower()

    async def test_get_unregistered_redirect_uri_returns_400(self, client, registered_client):
        r = await client.get(
            f"/authorize?client_id={registered_client}"
            f"&redirect_uri=https://evil.com/steal",
        )
        assert r.status_code == 400

    async def test_post_correct_password_redirects_with_code(self, client, registered_client):
        r = await client.post(
            f"/authorize?client_id={registered_client}"
            f"&redirect_uri=https://claude.ai/oauth/callback"
            f"&response_type=code&state=xyz",
            data={"password": "test_coach_secret"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        loc = r.headers["location"]
        assert "code=" in loc
        assert "state=xyz" in loc
        assert loc.startswith("https://claude.ai/oauth/callback")

    async def test_post_wrong_password_returns_401_with_form(self, client, registered_client):
        r = await client.post(
            f"/authorize?client_id={registered_client}"
            f"&redirect_uri=https://claude.ai/oauth/callback",
            data={"password": "wrong_password"},
            follow_redirects=False,
        )
        assert r.status_code == 401
        assert "text/html" in r.headers["content-type"]

    async def test_post_no_coach_secret_returns_503(self, client, registered_client):
        with patch.object(mcp_server, "COACH_SECRET", ""):
            r = await client.post(
                f"/authorize?client_id={registered_client}"
                f"&redirect_uri=https://claude.ai/oauth/callback",
                data={"password": "anything"},
                follow_redirects=False,
            )
        assert r.status_code == 503

    async def test_post_stores_code_in_state(self, client, registered_client):
        r = await client.post(
            f"/authorize?client_id={registered_client}"
            f"&redirect_uri=https://claude.ai/oauth/callback",
            data={"password": "test_coach_secret"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        # Extract code from location header
        loc = r.headers["location"]
        code = dict(p.split("=", 1) for p in loc.split("?", 1)[1].split("&"))["code"]
        assert code in mcp_server._oauth_codes

    async def test_post_with_pkce_stores_challenge(self, client, registered_client):
        verifier = "my_code_verifier_abcdef1234567890"
        challenge = _make_code_challenge(verifier)
        r = await client.post(
            f"/authorize?client_id={registered_client}"
            f"&redirect_uri=https://claude.ai/oauth/callback"
            f"&code_challenge={challenge}&code_challenge_method=S256",
            data={"password": "test_coach_secret"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        loc = r.headers["location"]
        code = dict(p.split("=", 1) for p in loc.split("?", 1)[1].split("&"))["code"]
        assert mcp_server._oauth_codes[code]["code_challenge"] == challenge

    async def test_lockout_after_max_failures(self, client, registered_client):
        url = f"/authorize?client_id={registered_client}&redirect_uri=https://claude.ai/oauth/callback"
        with patch("mcp_server._get_ip", return_value="1.2.3.4"):
            for _ in range(mcp_server._MAX_LOGIN_FAILURES):
                await client.post(url, data={"password": "wrong"}, follow_redirects=False)
            r = await client.post(url, data={"password": "wrong"}, follow_redirects=False)
        assert r.status_code == 429

    async def test_locked_ip_blocked_even_with_correct_password(self, client, registered_client):
        url = f"/authorize?client_id={registered_client}&redirect_uri=https://claude.ai/oauth/callback"
        with patch("mcp_server._get_ip", return_value="1.2.3.4"):
            for _ in range(mcp_server._MAX_LOGIN_FAILURES):
                await client.post(url, data={"password": "wrong"}, follow_redirects=False)
            r = await client.post(url, data={"password": "test_coach_secret"}, follow_redirects=False)
        assert r.status_code == 429

    async def test_successful_login_clears_failure_count(self, client, registered_client):
        url = f"/authorize?client_id={registered_client}&redirect_uri=https://claude.ai/oauth/callback"
        with patch("mcp_server._get_ip", return_value="1.2.3.4"):
            for _ in range(mcp_server._MAX_LOGIN_FAILURES - 1):
                await client.post(url, data={"password": "wrong"}, follow_redirects=False)
            await client.post(url, data={"password": "test_coach_secret"}, follow_redirects=False)
        assert "1.2.3.4" not in mcp_server._authorize_failures

    async def test_no_state_param_omitted_from_redirect(self, client, registered_client):
        r = await client.post(
            f"/authorize?client_id={registered_client}"
            f"&redirect_uri=https://claude.ai/oauth/callback",
            data={"password": "test_coach_secret"},
            follow_redirects=False,
        )
        loc = r.headers["location"]
        assert "state=" not in loc


# ---------------------------------------------------------------------------
# /token endpoint
# ---------------------------------------------------------------------------

class TestTokenEndpoint:
    def _insert_code(self, client_id: str, redirect_uri: str, challenge: str = "") -> str:
        code = "test_auth_code_12345"
        mcp_server._oauth_codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "expires_at": _ts() + 600,
        }
        return code

    async def test_valid_exchange_returns_token(self, client, registered_client):
        code = self._insert_code(registered_client, "https://claude.ai/oauth/callback")
        r = await client.post("/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/oauth/callback",
            "client_id": registered_client,
        })
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == mcp_server._TOKEN_EXPIRY

    async def test_token_stored_in_state(self, client, registered_client):
        code = self._insert_code(registered_client, "https://claude.ai/oauth/callback")
        r = await client.post("/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/oauth/callback",
            "client_id": registered_client,
        })
        token = r.json()["access_token"]
        assert _hash_token(token) in mcp_server._oauth_tokens

    async def test_code_is_single_use(self, client, registered_client):
        code = self._insert_code(registered_client, "https://claude.ai/oauth/callback")
        await client.post("/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/oauth/callback",
            "client_id": registered_client,
        })
        # Second use of same code must fail
        r2 = await client.post("/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/oauth/callback",
            "client_id": registered_client,
        })
        assert r2.status_code == 400
        assert r2.json()["error"] == "invalid_grant"

    async def test_unknown_code_returns_invalid_grant(self, client, registered_client):
        r = await client.post("/token", data={
            "grant_type": "authorization_code",
            "code": "nonexistent",
            "redirect_uri": "https://claude.ai/oauth/callback",
            "client_id": registered_client,
        })
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    async def test_wrong_client_id_returns_invalid_client(self, client, registered_client):
        code = self._insert_code(registered_client, "https://claude.ai/oauth/callback")
        r = await client.post("/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/oauth/callback",
            "client_id": "wrong_client",
        })
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_client"

    async def test_wrong_redirect_uri_returns_invalid_grant(self, client, registered_client):
        code = self._insert_code(registered_client, "https://claude.ai/oauth/callback")
        r = await client.post("/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://evil.com/steal",
            "client_id": registered_client,
        })
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    async def test_unsupported_grant_type_returns_error(self, client, registered_client):
        r = await client.post("/token", data={
            "grant_type": "client_credentials",
            "client_id": registered_client,
        })
        assert r.status_code == 400
        assert r.json()["error"] == "unsupported_grant_type"

    async def test_expired_code_returns_invalid_grant(self, client, registered_client):
        code = "expired_code"
        mcp_server._oauth_codes[code] = {
            "client_id": registered_client,
            "redirect_uri": "https://claude.ai/oauth/callback",
            "code_challenge": "",
            "code_challenge_method": "S256",
            "expires_at": _ts() - 1,
        }
        r = await client.post("/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/oauth/callback",
            "client_id": registered_client,
        })
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    async def test_pkce_correct_verifier_accepted(self, client, registered_client):
        verifier = "my_code_verifier_abcdef1234567890"
        challenge = _make_code_challenge(verifier)
        code = self._insert_code(registered_client, "https://claude.ai/oauth/callback", challenge)
        r = await client.post("/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/oauth/callback",
            "client_id": registered_client,
            "code_verifier": verifier,
        })
        assert r.status_code == 200
        assert "access_token" in r.json()

    async def test_pkce_wrong_verifier_rejected(self, client, registered_client):
        verifier = "correct_verifier_value_xyz"
        challenge = _make_code_challenge(verifier)
        code = self._insert_code(registered_client, "https://claude.ai/oauth/callback", challenge)
        r = await client.post("/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/oauth/callback",
            "client_id": registered_client,
            "code_verifier": "wrong_verifier",
        })
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    async def test_pkce_missing_verifier_rejected(self, client, registered_client):
        challenge = _make_code_challenge("some_verifier")
        code = self._insert_code(registered_client, "https://claude.ai/oauth/callback", challenge)
        r = await client.post("/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/oauth/callback",
            "client_id": registered_client,
        })
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"


# ---------------------------------------------------------------------------
# /revoke endpoint
# ---------------------------------------------------------------------------

class TestRevokeEndpoint:
    async def test_missing_auth_returns_401(self, client):
        r = await client.post("/revoke")
        assert r.status_code == 401

    async def test_wrong_auth_returns_401(self, client):
        r = await client.post("/revoke", headers={"X-Coach-Token": "WRONG"})
        assert r.status_code == 401

    async def test_clears_all_tokens(self, client, valid_token):
        assert _hash_token(valid_token) in mcp_server._oauth_tokens
        r = await client.post("/revoke", headers={"X-Coach-Token": "test_coach_secret"})
        assert r.status_code == 200
        assert r.json()["revoked"] == 1
        assert _hash_token(valid_token) not in mcp_server._oauth_tokens

    async def test_returns_count_of_revoked_tokens(self, client):
        mcp_server._oauth_tokens[_hash_token("tok1")] = {"client_id": "c1", "expires_at": _ts() + 3600}
        mcp_server._oauth_tokens[_hash_token("tok2")] = {"client_id": "c2", "expires_at": _ts() + 3600}
        r = await client.post("/revoke", headers={"X-Coach-Token": "test_coach_secret"})
        assert r.json()["revoked"] == 2
        assert len(mcp_server._oauth_tokens) == 0

    async def test_empty_store_returns_zero(self, client):
        r = await client.post("/revoke", headers={"X-Coach-Token": "test_coach_secret"})
        assert r.status_code == 200
        assert r.json()["revoked"] == 0


# ---------------------------------------------------------------------------
# Full OAuth flow integration test
# ---------------------------------------------------------------------------

class TestOAuthFullFlow:
    async def test_register_authorize_token_mcp(self, client):
        # 1. Register client
        reg = await client.post("/register", json={
            "redirect_uris": ["https://claude.ai/oauth/callback"],
            "client_name": "Claude.ai",
        })
        assert reg.status_code == 201
        client_id = reg.json()["client_id"]

        # 2. PKCE setup
        verifier = secrets.token_urlsafe(32)
        challenge = _make_code_challenge(verifier)

        # 3. Authorize — get login form
        auth_get = await client.get(
            f"/authorize?client_id={client_id}"
            f"&redirect_uri=https://claude.ai/oauth/callback"
            f"&response_type=code&state=flow_test"
            f"&code_challenge={challenge}&code_challenge_method=S256",
        )
        assert auth_get.status_code == 200

        # 4. Authorize — submit password
        auth_post = await client.post(
            f"/authorize?client_id={client_id}"
            f"&redirect_uri=https://claude.ai/oauth/callback"
            f"&response_type=code&state=flow_test"
            f"&code_challenge={challenge}&code_challenge_method=S256",
            data={"password": "test_coach_secret"},
            follow_redirects=False,
        )
        assert auth_post.status_code == 302
        loc = auth_post.headers["location"]
        params = dict(p.split("=", 1) for p in loc.split("?", 1)[1].split("&"))
        code = params["code"]
        assert params["state"] == "flow_test"

        # 5. Exchange code for token
        tok = await client.post("/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/oauth/callback",
            "client_id": client_id,
            "code_verifier": verifier,
        })
        assert tok.status_code == 200
        access_token = tok.json()["access_token"]

        # 6. Use token to hit /mcp
        mcp_r = await client.post(
            "/mcp",
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
        )
        assert mcp_r.status_code != 401


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
        # review_training adds athlete_profile and race_goal to context
        assert result["athlete_id"] == "i999999"
        assert "athlete_profile" in result
        assert "race_goal" in result

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

    async def test_date_normalised_to_datetime(self):
        import mcp_server

        captured_payload: dict = {}

        async def fake_put(path: str, payload: dict):
            captured_payload.update(payload)
            return {}

        with patch.object(mcp_server, "icu_put", new=fake_put):
            from mcp_server import update_workout
            await update_workout(event_id=1, date="2026-05-10")

        assert captured_payload["start_date_local"] == "2026-05-10T00:00:00"


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


class TestCreatePlanTool:
    async def test_calls_bulk_endpoint(self):
        import mcp_server

        workouts = [
            {"start_date_local": "2026-04-25", "name": "Easy Run", "type": "Run", "description": "30min easy"},
            {"start_date_local": "2026-04-26", "name": "Rest", "type": "Run", "description": "Rest day"},
        ]
        post_response = [{"id": 1}, {"id": 2}]

        with patch.object(mcp_server, "icu_post", new=AsyncMock(return_value=post_response)) as mock_post:
            from mcp_server import create_plan
            result = await create_plan(workouts)

        mock_post.assert_called_once()
        call_path = mock_post.call_args[0][0]
        assert "events/bulk" in call_path
        sent = mock_post.call_args[0][1]
        assert len(sent) == len(workouts)
        for w, s in zip(workouts, sent):
            for k, v in w.items():
                if k == "start_date_local":
                    assert s[k].startswith(v)
                else:
                    assert s[k] == v
            assert s["category"] == "WORKOUT"
            assert "T" in s["start_date_local"]
        assert result == post_response

    async def test_passes_workout_doc(self):
        import mcp_server

        workout_doc = {
            "description": "test", "duration": 3600, "ftp": 280,
            "target": "POWER",
            "steps": [{"reps": 1, "steps": [{"duration": 3600, "power": {"start": 65, "end": 75, "units": "%ftp"}}]}],
        }
        workouts = [{"start_date_local": "2026-04-25", "name": "Ride", "type": "Ride",
                     "description": "Aerobic", "workout_doc": workout_doc}]

        with patch.object(mcp_server, "icu_post", new=AsyncMock(return_value=[{"id": 1}])) as mock_post:
            from mcp_server import create_plan
            await create_plan(workouts)

        sent_payload = mock_post.call_args[0][1]
        assert sent_payload[0]["workout_doc"] == workout_doc

    async def test_distance_km_converted_to_metres(self):
        import mcp_server

        workouts = [{"start_date_local": "2026-04-25", "name": "Easy Run",
                     "type": "Run", "description": "Easy", "distance_km": 6.0}]

        with patch.object(mcp_server, "icu_post", new=AsyncMock(return_value=[{"id": 1}])) as mock_post:
            from mcp_server import create_plan
            await create_plan(workouts)

        sent = mock_post.call_args[0][1][0]
        assert sent["distance"] == 6000
        assert "distance_km" not in sent

    async def test_date_normalised_to_datetime(self):
        import mcp_server

        workouts = [{"start_date_local": "2026-05-01", "name": "Run",
                     "type": "Run", "description": "Easy"}]

        with patch.object(mcp_server, "icu_post", new=AsyncMock(return_value=[{"id": 1}])) as mock_post:
            from mcp_server import create_plan
            await create_plan(workouts)

        sent = mock_post.call_args[0][1][0]
        assert sent["start_date_local"] == "2026-05-01T00:00:00"

    async def test_full_datetime_unchanged(self):
        import mcp_server

        workouts = [{"start_date_local": "2026-05-01T06:30:00", "name": "Run",
                     "type": "Run", "description": "Morning run"}]

        with patch.object(mcp_server, "icu_post", new=AsyncMock(return_value=[{"id": 1}])) as mock_post:
            from mcp_server import create_plan
            await create_plan(workouts)

        sent = mock_post.call_args[0][1][0]
        assert sent["start_date_local"] == "2026-05-01T06:30:00"


# ---------------------------------------------------------------------------
# /.well-known/oauth-protected-resource/mcp path alias
# ---------------------------------------------------------------------------

class TestOAuthResourceMetadataMcpAlias:
    async def test_mcp_path_alias_returns_200(self, client):
        r = await client.get("/.well-known/oauth-protected-resource/mcp")
        assert r.status_code == 200

    async def test_mcp_alias_same_body_as_base_path(self, client):
        base = (await client.get("/.well-known/oauth-protected-resource")).json()
        alias = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert base == alias


# ---------------------------------------------------------------------------
# READ_ONLY mode
# ---------------------------------------------------------------------------

class TestReadOnlyMode:
    def test_read_only_false_in_tests(self):
        import mcp_server
        assert mcp_server.READ_ONLY is False

    def test_write_tools_present_when_read_only_false(self):
        import mcp_server
        assert hasattr(mcp_server, "create_workout")
        assert hasattr(mcp_server, "update_workout")
        assert hasattr(mcp_server, "delete_workout")
        assert hasattr(mcp_server, "create_plan")


# ---------------------------------------------------------------------------
# Athlete profile + race goal — get_profile, update_profile, set_race_goal,
# clear_race_goal
# ---------------------------------------------------------------------------

@pytest.fixture
def profile_paths(tmp_path):
    """Redirect profile/goal file paths to a temporary directory for isolation."""
    profile = str(tmp_path / "athlete_profile.json")
    goal = str(tmp_path / "athlete_goal.json")
    with patch.object(mcp_server, "_PROFILE_PATH", profile), \
         patch.object(mcp_server, "_GOAL_PATH", goal):
        yield {"profile": profile, "goal": goal}


class TestGetProfile:
    async def test_returns_default_when_no_file(self, profile_paths):
        from mcp_server import get_profile
        result = await get_profile()
        assert result["sport"] == "running"
        assert "notes" in result

    async def test_returns_stored_profile(self, profile_paths):
        mcp_server._write_json_file(profile_paths["profile"], {"sport": "cycling", "age": 35, "notes": "test"})
        from mcp_server import get_profile
        result = await get_profile()
        assert result["sport"] == "cycling"
        assert result["age"] == 35


class TestUpdateProfile:
    async def test_merges_fields(self, profile_paths):
        from mcp_server import update_profile
        result = await update_profile({"sport": "cycling", "age": 30})
        assert result["sport"] == "cycling"
        assert result["age"] == 30

    async def test_preserves_unset_fields(self, profile_paths):
        from mcp_server import update_profile
        await update_profile({"sport": "cycling"})
        result = await update_profile({"age": 25})
        assert result["sport"] == "cycling"
        assert result["age"] == 25

    async def test_rejects_unknown_fields(self, profile_paths):
        from mcp_server import update_profile
        result = await update_profile({"malicious_field": "evil", "sport": "running"})
        assert "malicious_field" not in result
        assert result["sport"] == "running"

    async def test_returns_error_on_empty_updates(self, profile_paths):
        from mcp_server import update_profile
        result = await update_profile({})
        assert "error" in result

    async def test_writes_to_file(self, profile_paths):
        import json
        from mcp_server import update_profile
        await update_profile({"sport": "triathlon"})
        # File should exist and be readable (plain JSON in test since no encryption key override)
        raw = open(profile_paths["profile"], "rb").read()
        # In tests COACH_SECRET is set, so file may be encrypted; just verify it exists and is non-empty
        assert len(raw) > 0


class TestSetRaceGoal:
    async def test_stores_goal_and_returns_it(self, profile_paths):
        from mcp_server import set_race_goal
        result = await set_race_goal(
            event_name="Oslo 10K",
            event_date="2027-01-01",
            distance_km=10.0,
            target_time="sub-50:00",
        )
        assert result["event_name"] == "Oslo 10K"
        assert result["distance_km"] == 10.0
        assert result["target_time"] == "sub-50:00"
        assert "current_phase" in result
        assert "weeks_to_race" in result

    async def test_base_phase_more_than_16_weeks(self, profile_paths):
        from mcp_server import set_race_goal
        from datetime import date, timedelta
        far_date = (date.today() + timedelta(weeks=20)).isoformat()
        result = await set_race_goal("Future Race", far_date, 42.2)
        assert result["current_phase"] == "base"

    async def test_taper_phase_2_to_4_weeks(self, profile_paths):
        from mcp_server import set_race_goal
        from datetime import date, timedelta
        soon = (date.today() + timedelta(weeks=3)).isoformat()
        result = await set_race_goal("Near Race", soon, 21.1)
        assert result["current_phase"] == "taper"

    async def test_race_week_within_1_week(self, profile_paths):
        from mcp_server import set_race_goal
        from datetime import date, timedelta
        very_soon = (date.today() + timedelta(days=5)).isoformat()
        result = await set_race_goal("This Weekend", very_soon, 5.0)
        assert result["current_phase"] == "race_week"

    async def test_past_date_returns_error(self, profile_paths):
        from mcp_server import set_race_goal
        result = await set_race_goal("Old Race", "2020-01-01", 10.0)
        assert "error" in result

    async def test_invalid_date_format_returns_error(self, profile_paths):
        from mcp_server import set_race_goal
        result = await set_race_goal("Bad Date Race", "not-a-date", 10.0)
        assert "error" in result


class TestClearRaceGoal:
    async def test_clears_existing_goal(self, profile_paths):
        import os
        mcp_server._write_json_file(profile_paths["goal"], {"event_name": "Test Race"})
        from mcp_server import clear_race_goal
        result = await clear_race_goal()
        assert result["status"] == "cleared"
        assert not os.path.exists(profile_paths["goal"])

    async def test_no_op_when_no_goal_exists(self, profile_paths):
        from mcp_server import clear_race_goal
        result = await clear_race_goal()
        assert result["status"] == "cleared"


# ---------------------------------------------------------------------------
# get_weather tool
# ---------------------------------------------------------------------------

_FAKE_GEO = {"results": [{"name": "Oslo", "country": "Norway", "latitude": 59.9127, "longitude": 10.7461}]}
_FAKE_FORECAST = {
    "daily": {
        "time": ["2026-04-24", "2026-04-25", "2026-04-26"],
        "weather_code": [61, 3, 0],
        "precipitation_sum": [5.2, 0.0, 0.0],
        "precipitation_probability_max": [80, 20, 5],
        "temperature_2m_max": [12.0, 14.0, 16.0],
        "temperature_2m_min": [6.0, 7.0, 8.0],
        "wind_speed_10m_max": [20.0, 15.0, 10.0],
    }
}


class TestSetCoachingStyle:
    async def test_preset_polarized(self, profile_paths):
        from mcp_server import set_coaching_style
        result = await set_coaching_style("polarized")
        assert result["coaching_methodology"] == "Polarized (80/20)"
        assert "80%" in result["coaching_description"]

    async def test_preset_maffetone(self, profile_paths):
        from mcp_server import set_coaching_style
        result = await set_coaching_style("maffetone")
        assert result["coaching_methodology"] == "Maffetone / MAF"
        assert "MAF" in result["coaching_description"]

    async def test_preset_jack_daniels(self, profile_paths):
        from mcp_server import set_coaching_style
        result = await set_coaching_style("jack_daniels")
        assert result["coaching_methodology"] == "Jack Daniels VDOT"

    async def test_preset_norwegian(self, profile_paths):
        from mcp_server import set_coaching_style
        result = await set_coaching_style("norwegian")
        assert "Norwegian" in result["coaching_methodology"]

    async def test_preset_pyramidal(self, profile_paths):
        from mcp_server import set_coaching_style
        result = await set_coaching_style("pyramidal")
        assert "Pyramidal" in result["coaching_methodology"]

    async def test_custom_requires_description(self, profile_paths):
        from mcp_server import set_coaching_style
        result = await set_coaching_style("custom")
        assert "error" in result

    async def test_custom_with_description(self, profile_paths):
        from mcp_server import set_coaching_style
        result = await set_coaching_style("custom", custom_description="High volume, low intensity base first.")
        assert result["coaching_methodology"] == "Custom"
        assert "High volume" in result["coaching_description"]

    async def test_unknown_methodology_returns_error(self, profile_paths):
        from mcp_server import set_coaching_style
        result = await set_coaching_style("zatsiorsky")
        assert "error" in result

    async def test_persists_to_profile(self, profile_paths):
        from mcp_server import set_coaching_style, get_profile
        await set_coaching_style("polarized")
        profile = await get_profile()
        assert profile["coaching_methodology"] == "Polarized (80/20)"
        assert "80%" in profile["coaching_description"]

    async def test_overwrites_previous_style(self, profile_paths):
        from mcp_server import set_coaching_style, get_profile
        await set_coaching_style("polarized")
        await set_coaching_style("pyramidal")
        profile = await get_profile()
        assert profile["coaching_methodology"] == "Pyramidal"


class TestGetProgress:
    def _make_activity(self, date: str, distance_m: float = 8000, tss: float = 50,
                       ctl: float = 55.0, hr: float | None = 140) -> dict:
        return {
            "start_date_local": date,
            "type": "Run",
            "distance": distance_m,
            "moving_time": 2700,
            "icu_training_load": tss,
            "icu_ctl": ctl,
            "average_heartrate": hr,
        }

    def _make_wellness(self, date: str, hrv: float = 60.0, rhr: float = 52.0,
                       sleep_secs: int = 25200) -> dict:
        return {"id": date, "hrv": hrv, "restingHR": rhr, "sleepSecs": sleep_secs}

    async def test_returns_monthly_summaries(self):
        acts = [
            self._make_activity("2026-02-10", ctl=48.0),
            self._make_activity("2026-02-20", ctl=51.0),
            self._make_activity("2026-03-10", ctl=54.0),
            self._make_activity("2026-03-25", ctl=57.0),
        ]
        wells = [
            self._make_wellness("2026-02-10"),
            self._make_wellness("2026-03-10", hrv=65.0),
        ]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(side_effect=[acts, wells])):
            from mcp_server import get_progress
            result = await get_progress(months=2)
        assert len(result["monthly_summaries"]) == 2
        assert result["monthly_summaries"][0]["month"] == "2026-02"
        assert result["monthly_summaries"][1]["month"] == "2026-03"

    async def test_trend_ctl_change(self):
        acts = [
            self._make_activity("2026-02-10", ctl=40.0),
            self._make_activity("2026-03-25", ctl=55.0),
        ]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(side_effect=[acts, []])):
            from mcp_server import get_progress
            result = await get_progress(months=2)
        assert result["trend"]["ctl_start"] == 40.0
        assert result["trend"]["ctl_end"] == 55.0
        assert result["trend"]["ctl_change"] == 15.0

    async def test_months_clamped_to_12(self):
        captured = {}

        async def _fake_get(path, params=None):
            captured["params"] = params or {}
            return []

        with patch.object(mcp_server, "icu_get", new=_fake_get):
            from mcp_server import get_progress
            await get_progress(months=99)
        # Should have requested max 12 months
        assert captured["params"].get("oldest") is not None

    async def test_empty_data_returns_structure(self):
        with patch.object(mcp_server, "icu_get", new=AsyncMock(side_effect=[[], []])):
            from mcp_server import get_progress
            result = await get_progress(months=3)
        assert "monthly_summaries" in result
        assert "trend" in result
        assert result["trend"]["ctl_change"] is None

    async def test_wellness_averages_computed(self):
        acts = [self._make_activity("2026-03-10", ctl=50.0)]
        wells = [
            self._make_wellness("2026-03-05", hrv=58.0, rhr=51.0, sleep_secs=25200),
            self._make_wellness("2026-03-12", hrv=62.0, rhr=49.0, sleep_secs=28800),
        ]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(side_effect=[acts, wells])):
            from mcp_server import get_progress
            result = await get_progress(months=1)
        summary = result["monthly_summaries"][0]
        assert summary["avg_hrv"] == 60.0
        assert summary["avg_resting_hr"] == 50.0

    async def test_easy_pace_computed_for_low_hr_runs(self):
        # 8km in 40min at 140bpm → 5.0 min/km
        acts = [{"start_date_local": "2026-03-10", "type": "Run",
                 "distance": 8000, "moving_time": 2400, "icu_training_load": 50,
                 "icu_ctl": 50.0, "average_heartrate": 140}]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(side_effect=[acts, []])):
            from mcp_server import get_progress
            result = await get_progress(months=1)
        summary = result["monthly_summaries"][0]
        assert summary["avg_easy_pace_min_per_km"] == 5.0

    async def test_high_hr_run_excluded_from_easy_pace(self):
        # 160bpm is above threshold → should not count as easy
        acts = [{"start_date_local": "2026-03-10", "type": "Run",
                 "distance": 8000, "moving_time": 2400, "icu_training_load": 70,
                 "icu_ctl": 52.0, "average_heartrate": 165}]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(side_effect=[acts, []])):
            from mcp_server import get_progress
            result = await get_progress(months=1)
        summary = result["monthly_summaries"][0]
        assert summary["avg_easy_pace_min_per_km"] is None


class TestGetWeather:
    def _mock_http(self, geo_data=None, forecast_data=None):
        """Return a mock that intercepts Open-Meteo requests."""
        geo_data = geo_data or _FAKE_GEO
        forecast_data = forecast_data or _FAKE_FORECAST

        async def _mock_get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "geocoding-api" in url:
                resp.json = MagicMock(return_value=geo_data)
            else:
                resp.json = MagicMock(return_value=forecast_data)
            return resp

        client = MagicMock()
        client.get = _mock_get
        return client

    async def test_no_location_returns_error(self, profile_paths):
        from mcp_server import get_weather
        result = await get_weather()
        assert "error" in result

    async def test_returns_forecast_for_named_city(self, profile_paths):
        import mcp_server as ms
        ms._write_json_file(profile_paths["profile"], {"sport": "running", "location": "Oslo, Norway"})
        mock_client = self._mock_http()
        with patch.object(ms, "http", return_value=mock_client):
            from mcp_server import get_weather
            result = await get_weather(days=3)
        assert result["location"] == "Oslo, Norway"
        assert len(result["forecast"]) == 3
        assert result["forecast"][0]["date"] == "2026-04-24"
        assert result["forecast"][0]["conditions"] == "Rain"
        assert result["forecast"][0]["precipitation_mm"] == 5.2

    async def test_latlon_skips_geocoding(self, profile_paths):
        import mcp_server as ms
        ms._write_json_file(profile_paths["profile"], {"sport": "running", "location": "59.9127,10.7461"})
        geo_calls = []

        async def _mock_get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "geocoding-api" in url:
                geo_calls.append(url)
                resp.json = MagicMock(return_value=_FAKE_GEO)
            else:
                resp.json = MagicMock(return_value=_FAKE_FORECAST)
            return resp

        mock_client = MagicMock()
        mock_client.get = _mock_get
        with patch.object(ms, "http", return_value=mock_client):
            from mcp_server import get_weather
            await get_weather()
        assert len(geo_calls) == 0

    async def test_unknown_city_returns_error(self, profile_paths):
        import mcp_server as ms
        ms._write_json_file(profile_paths["profile"], {"sport": "running", "location": "NowhereVille"})
        mock_client = self._mock_http(geo_data={"results": []})
        with patch.object(ms, "http", return_value=mock_client):
            from mcp_server import get_weather
            result = await get_weather()
        assert "error" in result

    async def test_forecast_days_clamped_to_14(self, profile_paths):
        import mcp_server as ms
        ms._write_json_file(profile_paths["profile"], {"sport": "running", "location": "Oslo, Norway"})
        captured_params = {}

        async def _mock_get(url, **kwargs):
            if "open-meteo.com/v1/forecast" in url:
                captured_params.update(kwargs.get("params", {}))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "geocoding" in url:
                resp.json = MagicMock(return_value=_FAKE_GEO)
            else:
                resp.json = MagicMock(return_value=_FAKE_FORECAST)
            return resp

        mock_client = MagicMock()
        mock_client.get = _mock_get
        with patch.object(ms, "http", return_value=mock_client):
            from mcp_server import get_weather
            await get_weather(days=99)
        assert captured_params.get("forecast_days") == 14
