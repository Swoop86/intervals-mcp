"""Tests for get_best_efforts, setup_run_pace_zones, and auto-coach add action."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _run_activity(name, dist_m, moving_time_s, sport="Run", date="2026-03-01"):
    return {
        "id": f"i{abs(hash(name)) % 100000}",
        "name": name,
        "type": sport,
        "distance": dist_m,
        "moving_time": moving_time_s,
        "start_date_local": f"{date}T08:00:00",
    }


# ---------------------------------------------------------------------------
# get_best_efforts
# ---------------------------------------------------------------------------

class TestGetBestEfforts:
    async def test_finds_best_5km(self):
        import mcp_server
        activities = [
            _run_activity("Easy 5k",  5000, 1500, date="2026-01-10"),  # 5:00/km
            _run_activity("Race 5k",  5000, 1350, date="2026-02-01"),  # 4:30/km — best
            _run_activity("Long run", 15000, 5400),                     # outside bracket
        ]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=activities)):
            from mcp_server import get_best_efforts
            result = await get_best_efforts(months=6)
        assert "5km" in result["best_efforts"]
        best = result["best_efforts"]["5km"]
        assert best["pace_min_per_km"] == pytest.approx(4.5, rel=0.01)
        assert best["activity_name"] == "Race 5k"

    async def test_excludes_out_of_bracket(self):
        import mcp_server
        activities = [_run_activity("Short run", 3000, 900)]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=activities)):
            from mcp_server import get_best_efforts
            result = await get_best_efforts()
        assert "5km" not in result["best_efforts"]

    async def test_non_run_types_excluded(self):
        import mcp_server
        activities = [_run_activity("Bike ride", 5000, 900, sport="Ride")]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=activities)):
            from mcp_server import get_best_efforts
            result = await get_best_efforts()
        assert "5km" not in result["best_efforts"]

    async def test_months_clamped_to_24(self):
        import mcp_server
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=[])):
            from mcp_server import get_best_efforts
            result = await get_best_efforts(months=99)
        assert result["period_months"] == 24

    async def test_half_marathon_bracket(self):
        import mcp_server
        activities = [_run_activity("HM race", 21100, 6600)]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=activities)):
            from mcp_server import get_best_efforts
            result = await get_best_efforts()
        assert "Half Marathon" in result["best_efforts"]

    def test_format_duration_sub_hour(self):
        from mcp_server import _format_duration
        assert _format_duration(330) == "5:30"
        assert _format_duration(3599) == "59:59"

    def test_format_duration_over_hour(self):
        from mcp_server import _format_duration
        assert _format_duration(3661) == "1:01:01"
        assert _format_duration(7200) == "2:00:00"

    async def test_treadmill_included(self):
        import mcp_server
        activities = [_run_activity("Treadmill 5k", 5000, 1500, sport="Treadmill")]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=activities)):
            from mcp_server import get_best_efforts
            result = await get_best_efforts()
        assert "5km" in result["best_efforts"]

    async def test_returns_fastest_not_most_recent(self):
        import mcp_server
        activities = [
            _run_activity("Old fast 5k", 5000, 1200, date="2025-06-01"),  # 4:00/km
            _run_activity("New slow 5k", 5000, 1500, date="2026-03-01"),  # 5:00/km
        ]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=activities)):
            from mcp_server import get_best_efforts
            result = await get_best_efforts()
        assert result["best_efforts"]["5km"]["activity_name"] == "Old fast 5k"


# ---------------------------------------------------------------------------
# setup_run_pace_zones
# ---------------------------------------------------------------------------

_ATHLETE_NO_ZONES = {
    "sportSettings": [{"activity_type": "Run", "threshold_pace": 3.333333}]
}
_ATHLETE_WITH_ZONES = {
    "sportSettings": [{
        "activity_type": "Run",
        "threshold_pace": 3.333333,
        "pace_zones": [2.593, 2.867, 3.1, 3.333, 3.6, 3.833],
    }]
}


class TestSetupRunPaceZones:
    async def test_returns_existing_zones_without_writing(self):
        """Default (force=False): if zones exist, return them and do NOT call icu_put."""
        import mcp_server
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=_ATHLETE_WITH_ZONES)), \
             patch.object(mcp_server, "icu_put", new=AsyncMock(return_value={})) as mock_put:
            from mcp_server import setup_run_pace_zones
            result = await setup_run_pace_zones()
        assert result["status"] == "already_configured"
        assert "current_zones" in result
        mock_put.assert_not_called()

    async def test_writes_when_no_existing_zones(self):
        import mcp_server
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=_ATHLETE_NO_ZONES)), \
             patch.object(mcp_server, "icu_put", new=AsyncMock(return_value={})) as mock_put:
            from mcp_server import setup_run_pace_zones
            result = await setup_run_pace_zones()
        assert result["status"] == "written"
        mock_put.assert_called_once()

    async def test_force_overwrites_existing_zones(self):
        import mcp_server
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=_ATHLETE_WITH_ZONES)), \
             patch.object(mcp_server, "icu_put", new=AsyncMock(return_value={})) as mock_put:
            from mcp_server import setup_run_pace_zones
            result = await setup_run_pace_zones(force=True)
        assert result["status"] == "written"
        mock_put.assert_called_once()

    async def test_writes_6_zone_values(self):
        import mcp_server
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=_ATHLETE_NO_ZONES)), \
             patch.object(mcp_server, "icu_put", new=AsyncMock(return_value={})) as mock_put:
            from mcp_server import setup_run_pace_zones
            result = await setup_run_pace_zones()
        assert len(result["zones_written"]) == 6
        _, payload = mock_put.call_args[0]
        assert len(payload["pace_zones"]) == 6

    async def test_z4_equals_threshold(self):
        import mcp_server
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=_ATHLETE_NO_ZONES)), \
             patch.object(mcp_server, "icu_put", new=AsyncMock(return_value={})):
            from mcp_server import setup_run_pace_zones
            result = await setup_run_pace_zones()
        assert result["zones_written"]["Z4"]["pct_threshold"] == 100

    async def test_zone_percentages_increase(self):
        import mcp_server
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=_ATHLETE_NO_ZONES)), \
             patch.object(mcp_server, "icu_put", new=AsyncMock(return_value={})):
            from mcp_server import setup_run_pace_zones
            result = await setup_run_pace_zones()
        pcts = [result["zones_written"][f"Z{i+1}"]["pct_threshold"] for i in range(6)]
        assert pcts == sorted(pcts)

    async def test_garmin_zone_boundaries(self):
        """Z1/Z2 boundary should be 78%, Z2/Z3 should be 86% (matches Garmin model)."""
        import mcp_server
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=_ATHLETE_NO_ZONES)), \
             patch.object(mcp_server, "icu_put", new=AsyncMock(return_value={})):
            from mcp_server import setup_run_pace_zones
            result = await setup_run_pace_zones()
        assert result["zones_written"]["Z1"]["pct_threshold"] == 78
        assert result["zones_written"]["Z2"]["pct_threshold"] == 86
        assert result["zones_written"]["Z3"]["pct_threshold"] == 93

    async def test_error_when_no_threshold_and_none_in_settings(self):
        import mcp_server
        athlete_data = {"sportSettings": [{"activity_type": "Run"}]}
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=athlete_data)):
            from mcp_server import setup_run_pace_zones
            result = await setup_run_pace_zones()
        assert "error" in result

    async def test_reads_threshold_from_athlete(self):
        import mcp_server
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=_ATHLETE_NO_ZONES)), \
             patch.object(mcp_server, "icu_put", new=AsyncMock(return_value={})):
            from mcp_server import setup_run_pace_zones
            result = await setup_run_pace_zones()
        assert result["status"] == "written"
        assert "5:00" in result["threshold_pace"]

    async def test_zone_speeds_increase_in_ms(self):
        import mcp_server
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=_ATHLETE_NO_ZONES)), \
             patch.object(mcp_server, "icu_put", new=AsyncMock(return_value={})) as mock_put:
            from mcp_server import setup_run_pace_zones
            await setup_run_pace_zones()
        _, payload = mock_put.call_args[0]
        speeds = payload["pace_zones"]
        assert speeds == sorted(speeds)
        assert speeds[-1] > speeds[0]


# ---------------------------------------------------------------------------
# Auto-coach add action
# ---------------------------------------------------------------------------

class TestCoachAddAction:
    def _ok_response(self, status=200, body=None):
        resp = MagicMock()
        resp.status_code = status
        resp.is_error = False
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=body or {})
        return resp

    async def test_add_creates_event(self):
        import claude_coach

        async def _mock_post(url, **kwargs):
            return self._ok_response(201, {"id": 999})

        mock_client = MagicMock()
        mock_client.post = _mock_post
        adj = [{
            "action": "add",
            "reason": "Adding recovery run after hard effort",
            "new_workout": {
                "start_date_local": "2026-04-28",
                "name": "Recovery Run",
                "type": "Run",
                "description": "30m 65-72% LTHR",
                "moving_time": 1800,
            },
        }]
        applied = await claude_coach.apply_adjustments(mock_client, adj, [])
        assert len(applied) == 1
        assert "Added" in applied[0]
        assert "Recovery Run" in applied[0]

    async def test_add_normalises_date(self):
        import claude_coach
        posted_payload = {}

        async def _mock_post(url, **kwargs):
            posted_payload.update(kwargs.get("json", {}))
            return self._ok_response(201, {"id": 42})

        mock_client = MagicMock()
        mock_client.post = _mock_post
        adj = [{
            "action": "add",
            "reason": "test",
            "new_workout": {
                "start_date_local": "2026-04-28",
                "name": "Easy Run",
                "type": "Run",
                "description": "30m easy",
            },
        }]
        await claude_coach.apply_adjustments(mock_client, adj, [])
        assert "T" in posted_payload["start_date_local"]

    async def test_add_strips_unknown_fields(self):
        import claude_coach
        posted_payload = {}

        async def _mock_post(url, **kwargs):
            posted_payload.update(kwargs.get("json", {}))
            return self._ok_response(201, {"id": 1})

        mock_client = MagicMock()
        mock_client.post = _mock_post
        adj = [{
            "action": "add",
            "reason": "test",
            "new_workout": {
                "start_date_local": "2026-04-28",
                "name": "Test",
                "type": "Run",
                "description": "30m easy",
                "unknown_field": "should_be_stripped",
            },
        }]
        await claude_coach.apply_adjustments(mock_client, adj, [])
        assert "unknown_field" not in posted_payload

    async def test_modify_still_works(self):
        import claude_coach

        async def _mock_put(url, **kwargs):
            return self._ok_response(200, {})

        mock_client = MagicMock()
        mock_client.put = _mock_put
        planned = [{"id": 101}]
        adj = [{
            "event_id": 101,
            "action": "modify",
            "reason": "reduce load",
            "changes": {"description": "20m 65-72% LTHR"},
        }]
        applied = await claude_coach.apply_adjustments(mock_client, adj, planned)
        assert len(applied) == 1
        assert "Modified" in applied[0]

    async def test_remove_still_works(self):
        import claude_coach

        async def _mock_delete(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 204
            resp.is_error = False
            resp.raise_for_status = MagicMock()
            return resp

        mock_client = MagicMock()
        mock_client.delete = _mock_delete
        planned = [{"id": 202}]
        adj = [{"event_id": 202, "action": "remove", "reason": "rest day"}]
        applied = await claude_coach.apply_adjustments(mock_client, adj, planned)
        assert len(applied) == 1
        assert "Removed" in applied[0]
