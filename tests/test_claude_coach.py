"""
Unit tests for claude_coach.py — data-cleaning helpers, prompt builders,
tool-use extraction, apply_adjustments, and the main coaching flow.
"""
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, call

import httpx
import pytest

import claude_coach
from claude_coach import (
    _safe_str, _clean_activity, _clean_wellness, _clean_planned,
    _extract_tool_input, _build_system_prompt, _build_user_message,
    apply_adjustments, fetch_context, run_coaching_flow,
    today_iso, days_ago_iso, in_days_iso,
    ALLOWED_FIELDS, ACTIVITIES_DAYS, WELLNESS_DAYS, PLANNED_DAYS,
)


# ---------------------------------------------------------------------------
# _safe_str  (duplicate of the one in mcp_server — same contract)
# ---------------------------------------------------------------------------

class TestSafeStr:
    def test_valid(self):
        assert _safe_str("mykey") == "mykey"

    def test_null_returns_empty(self):
        assert _safe_str("null") == ""

    def test_none_value_returns_empty(self):
        assert _safe_str(None) == ""

    def test_strips_whitespace(self):
        assert _safe_str("  hello  ") == "hello"


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

class TestDateHelpers:
    def test_today_iso_format(self):
        datetime.strptime(today_iso(), "%Y-%m-%d")

    def test_days_ago_is_in_past(self):
        assert days_ago_iso(1) < today_iso()

    def test_in_days_is_in_future(self):
        assert in_days_iso(1) > today_iso()


# ---------------------------------------------------------------------------
# _clean_activity
# ---------------------------------------------------------------------------

class TestCleanActivity:
    SAMPLE = {
        "id": "act42",
        "start_date_local": "2024-03-10T06:45:00",
        "name": "Easy Sunday",
        "type": "Run",
        "moving_time": 5400,
        "distance": 15000,
        "icu_training_load": 65,
        "average_heartrate": 138,
        "average_watts": None,
        "icu_ctl": 50.3,
        "icu_atl": 42.1,
        "icu_tsb": 8.2,
        "perceived_exertion": 5,
    }

    def test_id_and_name(self):
        r = _clean_activity(self.SAMPLE)
        assert r["id"] == "act42"
        assert r["name"] == "Easy Sunday"

    def test_date_truncated(self):
        assert _clean_activity(self.SAMPLE)["date"] == "2024-03-10"

    def test_duration_in_minutes(self):
        assert _clean_activity(self.SAMPLE)["duration_min"] == 90.0

    def test_distance_in_km(self):
        assert _clean_activity(self.SAMPLE)["distance_km"] == 15.0

    def test_fitness_metrics(self):
        r = _clean_activity(self.SAMPLE)
        assert r["ctl"] == 50.3
        assert r["atl"] == 42.1
        assert r["tsb"] == 8.2

    def test_perceived_effort_mapped(self):
        assert _clean_activity(self.SAMPLE)["perceived_effort"] == 5

    def test_none_distance_is_zero(self):
        r = _clean_activity({"distance": None, "moving_time": 0})
        assert r["distance_km"] == 0.0


# ---------------------------------------------------------------------------
# _clean_wellness
# ---------------------------------------------------------------------------

class TestCleanWellness:
    def test_sleep_seconds_to_hours(self):
        w = {"id": "2024-03-10", "sleepSecs": 28800,
             "hrv": 65, "restingHR": 48, "weight": 70.0}
        r = _clean_wellness(w)
        assert r["sleep_hours"] == 8.0
        assert r["hrv"] == 65
        assert r["resting_hr"] == 48
        assert r["weight_kg"] == 70.0

    def test_no_sleep_data_is_none(self):
        r = _clean_wellness({"id": "2024-03-10", "sleepSecs": None})
        assert r["sleep_hours"] is None

    def test_date_comes_from_id_field(self):
        r = _clean_wellness({"id": "2024-03-10"})
        assert r["date"] == "2024-03-10"

    def test_partial_sleep_rounding(self):
        r = _clean_wellness({"id": "d", "sleepSecs": 25200})  # 7 h exactly
        assert r["sleep_hours"] == 7.0


# ---------------------------------------------------------------------------
# _clean_planned
# ---------------------------------------------------------------------------

class TestCleanPlanned:
    def test_basic_fields(self):
        e = {
            "id": 201,
            "start_date_local": "2024-03-12T00:00:00",
            "name": "Tempo Intervals",
            "type": "Run",
            "description": "5×1km at threshold",
            "icu_training_load": 70,
            "moving_time": 3600,
        }
        r = _clean_planned(e)
        assert r["id"] == 201
        assert r["date"] == "2024-03-12"
        assert r["name"] == "Tempo Intervals"
        assert r["target_tss"] == 70
        assert r["duration_min"] == 60.0

    def test_no_moving_time_is_none(self):
        r = _clean_planned({"id": 1, "start_date_local": "2024-03-12"})
        assert r["duration_min"] is None


# ---------------------------------------------------------------------------
# _extract_tool_input
# ---------------------------------------------------------------------------

class TestExtractToolInput:
    def test_finds_correct_tool_block(self):
        data = {
            "content": [
                {"type": "text", "text": "thinking..."},
                {
                    "type": "tool_use",
                    "name": "submit_coaching_review",
                    "input": {
                        "analysis": "Great run!",
                        "alert_level": "green",
                        "adjustments": [],
                    },
                },
            ],
            "stop_reason": "tool_use",
        }
        r = _extract_tool_input(data)
        assert r["analysis"] == "Great run!"
        assert r["alert_level"] == "green"

    def test_raises_when_tool_not_called(self):
        data = {"content": [{"type": "text", "text": "no tool"}], "stop_reason": "end_turn"}
        with pytest.raises(ValueError, match="submit_coaching_review"):
            _extract_tool_input(data)

    def test_raises_on_empty_content(self):
        with pytest.raises(ValueError):
            _extract_tool_input({"content": [], "stop_reason": "end_turn"})

    def test_ignores_other_tool_blocks(self):
        data = {
            "content": [
                {"type": "tool_use", "name": "other_tool", "input": {"x": 1}},
                {"type": "tool_use", "name": "submit_coaching_review",
                 "input": {"analysis": "ok", "alert_level": "green", "adjustments": []}},
            ],
            "stop_reason": "tool_use",
        }
        r = _extract_tool_input(data)
        assert r["analysis"] == "ok"


# ---------------------------------------------------------------------------
# _build_system_prompt
# ---------------------------------------------------------------------------

class TestBuildSystemPrompt:
    PROFILE = "# Test Profile\n- Goal: sub-2:00 half marathon"

    def test_contains_athlete_profile(self):
        result = _build_system_prompt(self.PROFILE)
        assert "sub-2:00 half marathon" in result

    def test_contains_tsb_threshold(self):
        assert "TSB < -30" in _build_system_prompt(self.PROFILE)

    def test_contains_tool_invocation_instruction(self):
        assert "submit_coaching_review" in _build_system_prompt(self.PROFILE)

    def test_contains_hrv_rule(self):
        assert "HRV" in _build_system_prompt(self.PROFILE)


# ---------------------------------------------------------------------------
# _build_user_message
# ---------------------------------------------------------------------------

class TestBuildUserMessage:
    CONTEXT = {
        "latest_activity": {"name": "Morning Run", "tss": 55},
        "current_fitness": {"ctl": 45.0, "atl": 38.0, "tsb": 7.0},
        "wellness": [{"date": "2024-03-10", "hrv": 65}],
        "recent_activities": [],
        "planned_workouts": [],
    }

    def test_contains_section_headers(self):
        msg = _build_user_message(self.CONTEXT)
        for header in ("LATEST WORKOUT", "CURRENT FITNESS", "WELLNESS", "PLANNED SESSIONS"):
            assert header in msg

    def test_embeds_activity_name(self):
        assert "Morning Run" in _build_user_message(self.CONTEXT)

    def test_days_counts_mentioned(self):
        msg = _build_user_message(self.CONTEXT)
        assert str(WELLNESS_DAYS) in msg
        assert str(PLANNED_DAYS) in msg


# ---------------------------------------------------------------------------
# apply_adjustments
# ---------------------------------------------------------------------------

def _mock_response(status=200, json_data=None):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_data or {})
    return resp


class TestApplyAdjustments:
    @pytest.fixture
    def mock_client(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.put = AsyncMock(return_value=_mock_response(json_data={"id": 123}))
        client.delete = AsyncMock(return_value=_mock_response())
        return client

    async def test_unknown_event_id_skipped(self, mock_client):
        adjustments = [{"event_id": 999, "action": "modify", "reason": "test",
                        "changes": {"name": "New Name"}}]
        planned = [{"id": 1, "name": "Other Event"}]
        result = await apply_adjustments(mock_client, adjustments, planned)
        assert result == []
        mock_client.put.assert_not_called()

    async def test_modify_sends_allowed_fields_only(self, mock_client):
        with patch("claude_coach.icu_put", new=AsyncMock(return_value={})) as mock_put:
            adjustments = [{
                "event_id": 10,
                "action": "modify",
                "reason": "reduce volume",
                "changes": {
                    "name": "Easy Run",
                    "moving_time": 2700,
                    "pace": "5:30/km",        # NOT in ALLOWED_FIELDS
                    "secret_field": "oops",    # NOT in ALLOWED_FIELDS
                },
            }]
            planned = [{"id": 10, "name": "Hard Intervals"}]
            result = await apply_adjustments(mock_client, adjustments, planned)

        assert len(result) == 1
        assert "Modified" in result[0]
        sent = mock_put.call_args[0][2]  # positional: client, path, payload
        assert "name" in sent
        assert "moving_time" in sent
        assert "pace" not in sent
        assert "secret_field" not in sent

    async def test_modify_with_only_disallowed_fields_skipped(self, mock_client):
        with patch("claude_coach.icu_put", new=AsyncMock()) as mock_put:
            adjustments = [{"event_id": 10, "action": "modify", "reason": "x",
                            "changes": {"pace": "5:30", "notes": "ignored"}}]
            planned = [{"id": 10}]
            result = await apply_adjustments(mock_client, adjustments, planned)
        assert result == []
        mock_put.assert_not_called()

    async def test_remove_action_calls_delete(self, mock_client):
        with patch("claude_coach.icu_delete", new=AsyncMock()) as mock_del:
            adjustments = [{"event_id": 20, "action": "remove", "reason": "rest day"}]
            planned = [{"id": 20, "name": "Threshold Run"}]
            result = await apply_adjustments(mock_client, adjustments, planned)
        assert len(result) == 1
        assert "Removed" in result[0]
        mock_del.assert_called_once()

    async def test_api_exception_captured_in_result(self, mock_client):
        with patch("claude_coach.icu_put", new=AsyncMock(side_effect=Exception("network error"))):
            adjustments = [{"event_id": 30, "action": "modify", "reason": "x",
                            "changes": {"name": "Short Run"}}]
            planned = [{"id": 30}]
            result = await apply_adjustments(mock_client, adjustments, planned)
        assert len(result) == 1
        assert "Failed" in result[0] or "⚠️" in result[0]

    async def test_all_allowed_fields_accepted(self, mock_client):
        with patch("claude_coach.icu_put", new=AsyncMock(return_value={})) as mock_put:
            changes = {f: "val" for f in ALLOWED_FIELDS}
            adjustments = [{"event_id": 50, "action": "modify",
                            "reason": "full update", "changes": changes}]
            planned = [{"id": 50}]
            await apply_adjustments(mock_client, adjustments, planned)
        sent = mock_put.call_args[0][2]
        assert set(sent.keys()) == ALLOWED_FIELDS


# ---------------------------------------------------------------------------
# fetch_context
# ---------------------------------------------------------------------------

class TestFetchContext:
    ACTIVITIES = [{
        "id": "act1",
        "start_date_local": "2024-01-15T07:00:00",
        "name": "Run",
        "type": "Run",
        "moving_time": 3600,
        "distance": 10000,
        "icu_training_load": 55,
        "icu_ctl": 45.0,
        "icu_atl": 38.0,
        "icu_tsb": 7.0,
    }]
    WELLNESS = [{"id": "2024-01-15", "hrv": 65, "restingHR": 48,
                 "sleepSecs": 25200, "weight": 70.0}]
    PLANNED = [{"id": 201, "start_date_local": "2024-01-18T00:00:00",
                "name": "Tempo", "type": "Run", "description": "hard",
                "icu_training_load": 70, "moving_time": 3600}]
    ATHLETE = {"id": "i123", "ftp": 280, "lthr": 162, "weight": 70.0,
               "sportSettings": [{"activity_type": "Run", "threshold_pace": 3.509,
                                   "lthr": 162, "max_heart_rate": 190}]}

    async def test_returns_correct_structure(self):
        mock_client = AsyncMock()
        with patch("claude_coach.icu_get",
                   new=AsyncMock(side_effect=[self.ACTIVITIES, self.WELLNESS,
                                              self.PLANNED, self.ATHLETE])):
            result = await fetch_context(mock_client, "act1")

        assert result["latest_activity"]["id"] == "act1"
        assert result["latest_activity"]["duration_min"] == 60.0
        assert result["wellness"][0]["hrv"] == 65
        assert result["wellness"][0]["sleep_hours"] == 7.0
        assert result["planned_workouts"][0]["name"] == "Tempo"
        assert result["current_fitness"]["ctl"] == 45.0
        assert result["current_fitness"]["atl"] == 38.0
        assert result["current_fitness"]["tsb"] == 7.0

    async def test_includes_athlete_zones(self):
        mock_client = AsyncMock()
        with patch("claude_coach.icu_get",
                   new=AsyncMock(side_effect=[self.ACTIVITIES, self.WELLNESS,
                                              self.PLANNED, self.ATHLETE])):
            result = await fetch_context(mock_client, "act1")

        assert "athlete_zones" in result
        assert result["athlete_zones"]["running_threshold_pace_min_per_km"] == pytest.approx(4.75, abs=0.05)
        assert result["athlete_zones"]["run_lthr_bpm"] == 162
        assert result["athlete_zones"]["run_max_hr_bpm"] == 190

    async def test_uses_most_recent_activity_when_id_not_found(self):
        activities = [
            {"id": "old", "start_date_local": "2024-01-14T07:00:00",
             "moving_time": 1800, "distance": 5000},
            {"id": "new", "start_date_local": "2024-01-15T07:00:00",
             "moving_time": 3600, "distance": 10000},
        ]
        mock_client = AsyncMock()
        with patch("claude_coach.icu_get",
                   new=AsyncMock(side_effect=[activities, [], [], {}])):
            result = await fetch_context(mock_client, "nonexistent_id")

        assert result["latest_activity"]["id"] == "new"

    async def test_filters_note_type_from_planned(self):
        planned_with_note = [
            {"id": 1, "start_date_local": "2024-01-18T00:00:00",
             "name": "Workout", "type": "Run"},
            {"id": 2, "start_date_local": "2024-01-19T00:00:00",
             "name": "Race notes", "type": "Note"},
        ]
        mock_client = AsyncMock()
        with patch("claude_coach.icu_get",
                   new=AsyncMock(side_effect=[[], [], planned_with_note, {}])):
            result = await fetch_context(mock_client, "")

        assert len(result["planned_workouts"]) == 1
        assert result["planned_workouts"][0]["name"] == "Workout"


# ---------------------------------------------------------------------------
# run_coaching_flow
# ---------------------------------------------------------------------------

class TestRunCoachingFlow:
    CONTEXT = {
        "latest_activity": {"id": "act1", "name": "Morning Run"},
        "current_fitness": {"ctl": 45.0, "atl": 38.0, "tsb": 7.0},
        "wellness": [],
        "recent_activities": [],
        "planned_workouts": [{"id": 201, "name": "Tempo", "date": "2024-01-18"}],
    }
    CLAUDE_RESULT = {
        "analysis": "Solid effort. Keep building.",
        "alert_level": "green",
        "alert_reason": "Good form",
        "adjustments": [],
    }

    async def test_success_returns_status_ok(self):
        mock_client = AsyncMock()
        with patch("claude_coach.fetch_context", new=AsyncMock(return_value=self.CONTEXT)), \
             patch("claude_coach.call_claude", new=AsyncMock(return_value=self.CLAUDE_RESULT)), \
             patch("claude_coach.ha_notify", new=AsyncMock()), \
             patch("claude_coach.ha_mobile_notify", new=AsyncMock()):
            result = await run_coaching_flow("act1", http_client=mock_client)

        assert result["status"] == "ok"
        assert result["alert_level"] == "green"
        assert result["adjustments_applied"] == 0

    async def test_adjustments_are_applied_and_counted(self):
        context_with_plan = {**self.CONTEXT}
        claude_with_adj = {
            **self.CLAUDE_RESULT,
            "adjustments": [
                {"event_id": 201, "action": "modify", "reason": "reduce load",
                 "changes": {"name": "Easy Run", "moving_time": 1800}},
            ],
        }
        mock_client = AsyncMock()
        with patch("claude_coach.fetch_context", new=AsyncMock(return_value=context_with_plan)), \
             patch("claude_coach.call_claude", new=AsyncMock(return_value=claude_with_adj)), \
             patch("claude_coach.apply_adjustments",
                   new=AsyncMock(return_value=["✏️ Modified: Easy Run — reduce load"])) as mock_adj, \
             patch("claude_coach.ha_notify", new=AsyncMock()), \
             patch("claude_coach.ha_mobile_notify", new=AsyncMock()):
            result = await run_coaching_flow("act1", http_client=mock_client)

        assert result["adjustments_applied"] == 1
        mock_adj.assert_called_once()

    async def test_alert_level_red_propagated(self):
        red_result = {**self.CLAUDE_RESULT, "alert_level": "red",
                      "alert_reason": "Overreaching detected"}
        mock_client = AsyncMock()
        with patch("claude_coach.fetch_context", new=AsyncMock(return_value=self.CONTEXT)), \
             patch("claude_coach.call_claude", new=AsyncMock(return_value=red_result)), \
             patch("claude_coach.ha_notify", new=AsyncMock()), \
             patch("claude_coach.ha_mobile_notify", new=AsyncMock()):
            result = await run_coaching_flow("act1", http_client=mock_client)

        assert result["alert_level"] == "red"

    async def test_ha_notify_called_with_title(self):
        mock_client = AsyncMock()
        ha_mock = AsyncMock()
        with patch("claude_coach.fetch_context", new=AsyncMock(return_value=self.CONTEXT)), \
             patch("claude_coach.call_claude", new=AsyncMock(return_value=self.CLAUDE_RESULT)), \
             patch("claude_coach.ha_notify", new=ha_mock), \
             patch("claude_coach.ha_mobile_notify", new=AsyncMock()):
            await run_coaching_flow("act1", http_client=mock_client)

        ha_mock.assert_called_once()
        title_arg = ha_mock.call_args[0][1]  # ha_notify(client, title, message)
        assert "Morning Run" in title_arg
        assert "✅" in title_arg  # green alert emoji
