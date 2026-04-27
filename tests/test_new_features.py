"""Tests for get_best_efforts, setup_run_pace_zones, set_weekly_target, and auto-coach add action."""
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


# ---------------------------------------------------------------------------
# set_weekly_target
# ---------------------------------------------------------------------------

class TestSetWeeklyTarget:
    def _no_existing(self, mcp_server):
        """Patch icu_get to return an empty events list (no existing TARGET)."""
        return patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=[]))

    def _existing(self, mcp_server, event_id=42, sport="Run"):
        """Patch icu_get to return one existing TARGET event."""
        return patch.object(
            mcp_server,
            "icu_get",
            new=AsyncMock(return_value=[{"id": event_id, "category": "TARGET", "type": sport}]),
        )

    async def test_creates_target_event_when_none_exists(self):
        import mcp_server
        with self._no_existing(mcp_server), \
             patch.object(mcp_server, "icu_post", new=AsyncMock(return_value={"id": 77})) as mock_post:
            from mcp_server import set_weekly_target
            result = await set_weekly_target("2026-04-29", training_load=80, distance_km=50)
        assert result["status"] == "created"
        mock_post.assert_called_once()
        path, payload = mock_post.call_args[0]
        assert "events" in path
        assert payload["category"] == "TARGET"
        assert payload["icu_training_load"] == 80
        assert payload["distance"] == 50000

    async def test_updates_existing_target_event(self):
        """Second call for same week+sport should PUT, not POST a duplicate."""
        import mcp_server
        with self._existing(mcp_server, event_id=42), \
             patch.object(mcp_server, "icu_put", new=AsyncMock(return_value={"id": 42})) as mock_put, \
             patch.object(mcp_server, "icu_post", new=AsyncMock()) as mock_post:
            from mcp_server import set_weekly_target
            result = await set_weekly_target("2026-04-27", training_load=90)
        assert result["status"] == "updated"
        assert result["event_id"] == 42
        mock_put.assert_called_once()
        mock_post.assert_not_called()
        path, payload = mock_put.call_args[0]
        assert "events/42" in path
        assert payload["icu_training_load"] == 90

    async def test_different_sport_creates_new_event(self):
        """Existing Run TARGET does not block a new Swim TARGET for the same week."""
        import mcp_server
        with self._existing(mcp_server, event_id=42, sport="Run"), \
             patch.object(mcp_server, "icu_post", new=AsyncMock(return_value={"id": 99})) as mock_post:
            from mcp_server import set_weekly_target
            result = await set_weekly_target("2026-04-27", sport="Swim", training_load=30)
        assert result["status"] == "created"
        mock_post.assert_called_once()

    async def test_uses_monday_of_week(self):
        """Any date in the week should resolve to its Monday."""
        import mcp_server
        with self._no_existing(mcp_server), \
             patch.object(mcp_server, "icu_post", new=AsyncMock(return_value={"id": 1})) as mock_post:
            from mcp_server import set_weekly_target
            # 2026-04-29 is a Wednesday — Monday of that week is 2026-04-27
            await set_weekly_target("2026-04-29", training_load=70)
        _, payload = mock_post.call_args[0]
        assert payload["start_date_local"].startswith("2026-04-27")

    async def test_monday_input_unchanged(self):
        import mcp_server
        with self._no_existing(mcp_server), \
             patch.object(mcp_server, "icu_post", new=AsyncMock(return_value={"id": 1})) as mock_post:
            from mcp_server import set_weekly_target
            await set_weekly_target("2026-04-27", training_load=70)  # already Monday
        _, payload = mock_post.call_args[0]
        assert payload["start_date_local"].startswith("2026-04-27")

    async def test_duration_converted_to_seconds(self):
        import mcp_server
        with self._no_existing(mcp_server), \
             patch.object(mcp_server, "icu_post", new=AsyncMock(return_value={"id": 1})) as mock_post:
            from mcp_server import set_weekly_target
            await set_weekly_target("2026-04-27", duration_hours=6.5)
        _, payload = mock_post.call_args[0]
        assert payload["moving_time"] == 6.5 * 3600

    async def test_notes_become_description(self):
        import mcp_server
        with self._no_existing(mcp_server), \
             patch.object(mcp_server, "icu_post", new=AsyncMock(return_value={"id": 1})) as mock_post:
            from mcp_server import set_weekly_target
            await set_weekly_target("2026-04-27", training_load=60, notes="Base phase")
        _, payload = mock_post.call_args[0]
        assert payload["description"] == "Base phase"

    async def test_sport_defaults_to_run(self):
        import mcp_server
        with self._no_existing(mcp_server), \
             patch.object(mcp_server, "icu_post", new=AsyncMock(return_value={"id": 1})) as mock_post:
            from mcp_server import set_weekly_target
            await set_weekly_target("2026-04-27", training_load=60)
        _, payload = mock_post.call_args[0]
        assert payload["type"] == "Run"

    async def test_omitted_fields_not_in_payload(self):
        """Fields not provided should not appear in the posted payload."""
        import mcp_server
        with self._no_existing(mcp_server), \
             patch.object(mcp_server, "icu_post", new=AsyncMock(return_value={"id": 1})) as mock_post:
            from mcp_server import set_weekly_target
            await set_weekly_target("2026-04-27", training_load=60)
        _, payload = mock_post.call_args[0]
        assert "moving_time" not in payload
        assert "distance" not in payload
        assert "description" not in payload

    async def test_returns_week_starting_date(self):
        import mcp_server
        with self._no_existing(mcp_server), \
             patch.object(mcp_server, "icu_post", new=AsyncMock(return_value={"id": 5})):
            from mcp_server import set_weekly_target
            result = await set_weekly_target("2026-04-30", training_load=75)
        assert result["week_starting"] == "2026-04-27"  # Thursday → Monday

    async def test_get_failure_falls_back_to_create(self):
        """If the GET events call fails, fall back to creating a new event."""
        import mcp_server
        with patch.object(mcp_server, "icu_get", new=AsyncMock(side_effect=Exception("network error"))), \
             patch.object(mcp_server, "icu_post", new=AsyncMock(return_value={"id": 9})) as mock_post:
            from mcp_server import set_weekly_target
            result = await set_weekly_target("2026-04-27", training_load=60)
        assert result["status"] == "created"
        mock_post.assert_called_once()


# ---------------------------------------------------------------------------
# get_progress — aerobic efficiency factor
# ---------------------------------------------------------------------------

def _easy_run(month_prefix, dist_m, moving_time_s, avg_hr):
    """Easy run suitable for EF calculation (120–155 bpm, >3km)."""
    return {
        "id": f"i{abs(hash(month_prefix+str(dist_m))) % 100000}",
        "type": "Run",
        "start_date_local": f"{month_prefix}-10T07:00:00",
        "distance": dist_m,
        "moving_time": moving_time_s,
        "average_heartrate": avg_hr,
        "icu_training_load": 50,
        "icu_ctl": 45,
    }


class TestGetProgressAerobicEF:
    def _wellness(self, month_prefix):
        return [{"id": f"{month_prefix}-10", "hrv": 60, "restingHR": 52, "sleepSecs": 27000}]

    async def test_aerobic_ef_present_in_monthly_summary(self):
        import mcp_server
        acts = [_easy_run("2026-01", 8000, 2560, 140)]  # 3.125 m/s, EF = 3.125/140
        wells = self._wellness("2026-01")
        with patch.object(mcp_server, "icu_get", new=AsyncMock(side_effect=[acts, wells])):
            from mcp_server import get_progress
            result = await get_progress(months=1)
        month = result["monthly_summaries"][0]
        assert month["aerobic_ef"] is not None
        expected_ef = round((8000 / 2560) / 140, 5)
        assert abs(month["aerobic_ef"] - expected_ef) < 0.00001

    async def test_aerobic_ef_improves_with_fitness(self):
        """Second month EF should be higher when same HR gives faster pace."""
        import mcp_server
        acts_jan = [_easy_run("2026-01", 8000, 2560, 140)]  # slower
        acts_feb = [_easy_run("2026-02", 8000, 2400, 140)]  # faster at same HR
        acts = acts_jan + acts_feb
        wells = self._wellness("2026-01") + self._wellness("2026-02")
        with patch.object(mcp_server, "icu_get", new=AsyncMock(side_effect=[acts, wells])):
            from mcp_server import get_progress
            result = await get_progress(months=2)
        months = result["monthly_summaries"]
        assert months[1]["aerobic_ef"] > months[0]["aerobic_ef"]

    async def test_ef_trend_in_summary(self):
        """trend block should include aerobic_ef_start, ef_end, ef_change_pct."""
        import mcp_server
        acts = [
            _easy_run("2026-01", 8000, 2560, 140),
            _easy_run("2026-02", 8000, 2400, 140),
        ]
        wells = self._wellness("2026-01") + self._wellness("2026-02")
        with patch.object(mcp_server, "icu_get", new=AsyncMock(side_effect=[acts, wells])):
            from mcp_server import get_progress
            result = await get_progress(months=2)
        trend = result["trend"]
        assert trend["aerobic_ef_start"] is not None
        assert trend["aerobic_ef_end"] > trend["aerobic_ef_start"]
        assert trend["aerobic_ef_change_pct"] > 0

    async def test_no_ef_when_hr_missing(self):
        """Activities without avg HR should not crash and should return None EF."""
        import mcp_server
        act = _easy_run("2026-01", 8000, 2560, 140)
        act_no_hr = {**act, "average_heartrate": None}
        with patch.object(mcp_server, "icu_get", new=AsyncMock(side_effect=[[act_no_hr], []])):
            from mcp_server import get_progress
            result = await get_progress(months=1)
        assert result["monthly_summaries"][0]["aerobic_ef"] is None

    async def test_ef_excludes_short_runs(self):
        """Runs under 3km should not contribute to EF calculation."""
        import mcp_server
        short = {**_easy_run("2026-01", 2000, 700, 140)}  # only 2km
        with patch.object(mcp_server, "icu_get", new=AsyncMock(side_effect=[[short], []])):
            from mcp_server import get_progress
            result = await get_progress(months=1)
        assert result["monthly_summaries"][0]["aerobic_ef"] is None


# ---------------------------------------------------------------------------
# get_training_distribution
# ---------------------------------------------------------------------------

def _te_activity(date, duration_s, label, sport="Run"):
    return {
        "id": f"i{abs(hash(date+label)) % 100000}",
        "type": sport,
        "start_date_local": f"{date}T08:00:00",
        "moving_time": duration_s,
        "training_effect_label": label,
        "icu_training_load": 50,
    }


class TestGetTrainingDistribution:
    async def test_polarized_week_classified_correctly(self):
        import mcp_server
        # 80% low, 5% moderate, 15% high → Polarized
        acts = [
            _te_activity("2026-04-21", 4800, "Base"),      # 80 min low
            _te_activity("2026-04-22",  300, "Tempo"),     #  5 min moderate
            _te_activity("2026-04-23",  900, "VO2Max"),    # 15 min high
        ]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=acts)):
            from mcp_server import get_training_distribution
            result = await get_training_distribution(weeks=1)
        assert result["weekly"][0]["distribution"] == "Polarized"

    async def test_threshold_week_classified_correctly(self):
        import mcp_server
        # 30% low, 50% moderate (Tempo), 20% high → Threshold (high not > moderate, so not HIIT)
        acts = [
            _te_activity("2026-04-21", 3600, "Base"),    # 30 min low
            _te_activity("2026-04-22", 6000, "Tempo"),   # 50 min moderate
            _te_activity("2026-04-23", 2400, "VO2Max"),  # 20 min high
        ]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=acts)):
            from mcp_server import get_training_distribution
            result = await get_training_distribution(weeks=1)
        assert result["weekly"][0]["distribution"] == "Threshold"

    async def test_pyramidal_week_classified_correctly(self):
        import mcp_server
        # 60% low, 30% moderate, 10% high → Pyramidal
        acts = [
            _te_activity("2026-04-21", 3600, "Base"),
            _te_activity("2026-04-22", 1800, "Tempo"),
            _te_activity("2026-04-23",  600, "Threshold"),
        ]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=acts)):
            from mcp_server import get_training_distribution
            result = await get_training_distribution(weeks=1)
        assert result["weekly"][0]["distribution"] == "Pyramidal"

    async def test_consecutive_hard_weeks_counted(self):
        import mcp_server
        # 3 threshold weeks → overreaching_flag True
        acts = [
            _te_activity("2026-04-07", 3600, "Threshold"),
            _te_activity("2026-04-08", 3600, "Threshold"),
            _te_activity("2026-04-14", 3600, "Threshold"),
            _te_activity("2026-04-15", 3600, "Threshold"),
            _te_activity("2026-04-21", 3600, "Threshold"),
            _te_activity("2026-04-22", 3600, "Threshold"),
        ]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=acts)):
            from mcp_server import get_training_distribution
            result = await get_training_distribution(weeks=3)
        assert result["overreaching_flag"] is True
        assert result["consecutive_hard_weeks"] >= 3

    async def test_no_overreaching_flag_for_easy_week(self):
        import mcp_server
        acts = [_te_activity("2026-04-21", 3600, "Base")]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=acts)):
            from mcp_server import get_training_distribution
            result = await get_training_distribution(weeks=1)
        assert result["overreaching_flag"] is False

    async def test_zone_pct_sums_to_100(self):
        import mcp_server
        acts = [
            _te_activity("2026-04-21", 3000, "Base"),
            _te_activity("2026-04-22", 1000, "Tempo"),
            _te_activity("2026-04-23",  500, "VO2Max"),
        ]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=acts)):
            from mcp_server import get_training_distribution
            result = await get_training_distribution(weeks=1)
        z = result["weekly"][0]["zone_pct"]
        total = z["low_pct"] + z["moderate_pct"] + z["high_pct"]
        assert abs(total - 100.0) < 0.5


# ---------------------------------------------------------------------------
# compare_season
# ---------------------------------------------------------------------------

class TestCompareSeason:
    def _make_acts(self, start_date_prefix, count=3):
        return [
            {
                "id": f"i{i}",
                "type": "Run",
                "start_date_local": f"{start_date_prefix}-{10+i:02d}T08:00:00",
                "distance": 8000,
                "moving_time": 2700,
                "average_heartrate": 142,
                "icu_training_load": 55,
                "icu_ctl": 50,
                "training_effect_label": "Base",
            }
            for i in range(count)
        ]

    def _make_wells(self, start_date_prefix, count=5):
        return [
            {"id": f"{start_date_prefix}-{10+i:02d}", "hrv": 65, "restingHR": 50}
            for i in range(count)
        ]

    async def test_returns_both_periods(self):
        import mcp_server
        now_acts = self._make_acts("2026-04")
        lyr_acts = self._make_acts("2025-04")
        now_well = self._make_wells("2026-04")
        lyr_well = self._make_wells("2025-04")
        with patch.object(mcp_server, "icu_get", new=AsyncMock(
            side_effect=[now_acts, lyr_acts, now_well, lyr_well]
        )):
            from mcp_server import compare_season
            result = await compare_season(weeks=4)
        assert "current_period" in result
        assert "year_ago_period" in result
        assert result["current_period"]["activity_count"] == 3
        assert result["year_ago_period"]["activity_count"] == 3

    async def test_changes_pct_computed(self):
        import mcp_server
        # Now: 3 runs × 8km = 24km; Last year: 3 runs × 6km = 18km → +33%
        now_acts = self._make_acts("2026-04")
        lyr_acts = [{**a, "distance": 6000} for a in self._make_acts("2025-04")]
        with patch.object(mcp_server, "icu_get", new=AsyncMock(
            side_effect=[now_acts, lyr_acts, [], []]
        )):
            from mcp_server import compare_season
            result = await compare_season(weeks=4)
        pct = result["changes_pct"]["total_km"]
        assert pct is not None
        assert pct > 0  # more km this year

    async def test_empty_year_ago_graceful(self):
        """If there were no activities last year, should not crash."""
        import mcp_server
        now_acts = self._make_acts("2026-04")
        with patch.object(mcp_server, "icu_get", new=AsyncMock(
            side_effect=[now_acts, [], [], []]
        )):
            from mcp_server import compare_season
            result = await compare_season(weeks=4)
        assert result["year_ago_period"]["total_km"] == 0
        assert result["changes_pct"]["total_km"] is None  # can't divide by zero

    async def test_window_weeks_clamped(self):
        import mcp_server
        with patch.object(mcp_server, "icu_get", new=AsyncMock(return_value=[])):
            from mcp_server import compare_season
            result = await compare_season(weeks=99)
        assert result["window_weeks"] == 12
