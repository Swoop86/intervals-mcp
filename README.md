# Intervals.icu MCP Server

A Home Assistant addon that connects your [intervals.icu](https://intervals.icu) training data to Claude AI via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). Ask Claude to review your training, build periodized plans around a race goal, and schedule workouts around the weather — all using your real fitness data.

## What it does

```
Claude.ai ──MCP──► /mcp ──► intervals.icu API
                              └─► Activities, HRV, sleep, wellness
                              └─► CTL / ATL / TSB fitness metrics
                              └─► Planned workouts / calendar
                              └─► Athlete profile + race goal (local)
                   /mcp ──► Open-Meteo (weather forecast, no API key)

intervals.icu ──webhook──► /webhook ──► HA events + mobile notifications
HA automation ──HTTP──► /coach ──► Claude ──► calendar adjustments + HA push
```

## MCP tools available to Claude

| Tool | What it does |
|------|-------------|
| `review_training` | Fetch 28 days of activities, 14 days of wellness (HRV/sleep/resting HR), and CTL/ATL/TSB — the full coaching context |
| `get_activities` | Recent completed workouts with TSS, HR, pace/power |
| `get_wellness` | HRV, resting HR, sleep, weight, mood |
| `get_fitness` | CTL/ATL/TSB history |
| `get_athlete` | FTP, LTHR, weight, sport zones |
| `get_planned_workouts` | Upcoming calendar events |
| `get_activity_detail` | Full detail for a specific activity |
| `get_activity_intervals` | Lap/interval breakdown for pacing analysis |
| `get_profile` | Your stored coaching profile (paces, limiters, notes) |
| `get_weather` | 7-day forecast for your location via Open-Meteo |
| `update_profile` | Save your training preferences to the profile |
| `set_race_goal` | Set a target event → enables periodized coaching mode |
| `clear_race_goal` | Remove the goal → back to general improvement mode |
| `create_workout` | Add a planned workout to the calendar |
| `update_workout` | Modify a planned workout (reschedule, adjust load) |
| `delete_workout` | Remove a planned workout |
| `create_plan` | Bulk-create a training plan (syncs structured workouts to Garmin) |

Write tools (`create_workout`, `update_workout`, `delete_workout`, `create_plan`, `update_profile`, `set_race_goal`, `clear_race_goal`) are disabled when `read_only: true`.

---

## Installation

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add: `https://github.com/Swoop86/intervals-mcp`
3. Install **Intervals.icu MCP Server**
4. Set `athlete_id` and `api_key` in the addon configuration
5. Start the addon

---

## Connecting to Claude.ai

Expose the addon externally (e.g. via [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)) and add it as a remote MCP server in Claude.ai settings:

```
https://your-tunnel-domain.com/mcp
```

Claude.ai will initiate an OAuth 2.1 login flow. It redirects to `/authorize` — a login page hosted by the addon itself. Enter your `coach_secret`, and Claude.ai receives a bearer token valid for `token_expiry_days` days (default 180). After that you'll be prompted to log in again.

> **Cloudflare Tunnel tip:** If you have geo-based security rules, make sure requests from Anthropic's servers (US-based) are not blocked, or the connection will fail silently.

---

## Setting up your profile

The athlete profile lets Claude personalise coaching to your actual ability and preferences. Set it up once and it persists across conversations.

**Example conversation:**

> **You:** Set up my profile — I'm 34, running about 40km/week, easy pace around 6:00/km, threshold around 4:45/km. I sometimes get left knee pain on long downhills. I train Mon/Tue/Thu/Sat/Sun, rest Wed/Fri. I'm based in Oslo, Norway.
>
> **Claude:** *calls `update_profile` with your details*
>
> Done! I've saved your profile. I can see your location is Oslo, so I can check the weather before suggesting outdoor sessions.

Check what's stored at any time:

> **You:** What does my profile say?
>
> **Claude:** *calls `get_profile`*

---

## Race goal / event prep mode

Without a race goal, coaching stays in **general improvement mode** — building aerobic fitness and consistency with no deadline pressure.

When you have a specific event, tell Claude and it switches to **periodized event prep**:

> **You:** I've entered the Bergen City Marathon on 15 September. I want to run sub-4 hours. It's my first marathon — I just want to finish strong.
>
> **Claude:** *calls `set_race_goal`*
>
> Race goal set! That's 20 weeks away, so you're in the **Base phase** right now. Here's how your training will be structured:
>
> - **Base (now → ~week 12):** High volume, mostly easy running. Build your long run to 30km. Keep intensity low.
> - **Build (weeks 8–16):** Add tempo runs and marathon-pace sessions. Introduce progression runs.
> - **Peak (weeks 4–8):** Race-specific long runs with the final 10km at goal pace. Your highest-TSS weeks.
> - **Taper (final 3 weeks):** Cut volume ~30% while keeping intensity. Arrive fresh.
>
> Every coaching review from now on will be framed around this goal and your current phase.

Once the race is done:

> **You:** I finished the marathon! Clear my race goal.
>
> **Claude:** *calls `clear_race_goal`* — back to general improvement mode.

---

## Weather-aware scheduling

Once your location is set in the profile, Claude can check the forecast before scheduling or moving workouts.

**Example — moving a long run:**

> **You:** Can you review my plan for this week?
>
> **Claude:** *calls `review_training` and `get_weather`*
>
> Your long run is scheduled for Saturday but I'm seeing 18mm of rain forecast with 60km/h gusts. Sunday looks clear and 14°C — perfect for a long effort. Want me to move it to Sunday?
>
> **You:** Yes please.
>
> **Claude:** *calls `update_workout`* — Done, moved to Sunday.

**Example — asking about rain gear:**

> **You:** Should I do my tempo run tomorrow?
>
> **Claude:** *calls `get_weather`*
>
> Tomorrow shows light rain (3mm, 70% chance) and 8°C. The effort itself is fine — do you have rain gear and are you comfortable running in light rain? If not, I can swap it with Wednesday's easy run, which looks dry.

**Example — building a plan around the forecast:**

> **You:** Build me a 5-day training block starting Monday.
>
> **Claude:** *calls `get_weather` and `review_training`*
>
> Monday looks good — I'll put your tempo session there. Tuesday has heavy rain so I'll keep that as strength/cross-training. Wednesday–Thursday are clear, ideal for easy runs. Friday looks fine for your long run since you're fresh. Creating the plan now…
>
> **Claude:** *calls `create_plan` with 5 workouts*

---

## Automated coaching (webhook + /coach)

### Webhook — triggers on every workout

When a workout syncs to intervals.icu, a webhook fires → Claude reviews it automatically → adjustments are applied to upcoming sessions → you get an HA notification.

**Setup in intervals.icu:** Settings → Developer Settings → Webhooks
- URL: `https://your-tunnel-domain.com/webhook`
- Set the same value as `webhook_secret` in the addon config

### /coach — trigger manually from HA

POST to `/coach` with `X-Coach-Token: <coach_secret>` to trigger a coaching review on demand — useful in HA automations (e.g. morning briefing).

```yaml
# automations.yaml example
- alias: Morning training briefing
  trigger:
    - platform: time
      at: "07:30:00"
  action:
    - service: rest_command.coaching_review
```

```yaml
# configuration.yaml
rest_command:
  coaching_review:
    url: https://your-tunnel-domain.com/coach
    method: POST
    headers:
      X-Coach-Token: !secret coach_secret
```

---

## Garmin structured workouts

`create_workout` and `create_plan` both support a `workout_doc` field that sends interval-by-interval targets to Garmin Connect (and other connected devices).

When Claude builds a plan with `workout_doc`, the workout appears on your Garmin with step-by-step prompts — pace alerts, HR zones, rep countdowns. Just tell Claude you want structured workouts:

> **You:** Build me a 4-week build block with structured workouts for Garmin. I have a threshold of 4:45/km.

---

## Security

- **Set `coach_secret`** — without it, anyone who reaches your addon URL can query your training data and trigger coaching.
- Rate limiting: 5 requests/s per IP (burst 10). Login lockout: 5 failed attempts → 1 hour block.
- OAuth tokens are stored as SHA-256 hashes — the raw token is never written to disk.
- Athlete profile and race goal are stored as **Fernet-encrypted JSON** at `/data/`, keyed from your `coach_secret`. If `coach_secret` is not set, they're stored as plain JSON.
- Use `read_only: true` to prevent Claude from modifying your training calendar (recommended for shared or public setups).

---

## Configuration

| Option | Required | Description |
|--------|----------|-------------|
| `athlete_id` | Yes | Your intervals.icu athlete ID (from the URL when logged in) |
| `api_key` | Yes | intervals.icu API key (Settings → API) |
| `port` | No | Port to listen on (default: 8765) |
| `coach_secret` | No | Password for the OAuth login form — gates access to `/mcp` and `/coach`. **Strongly recommended.** |
| `token_expiry_days` | No | How long OAuth tokens stay valid (default: 180). POST `/revoke` with `X-Coach-Token` to invalidate all tokens immediately. |
| `anthropic_api_key` | No | Anthropic API key — required for the `/coach` HTTP endpoint |
| `claude_model` | No | Claude model for `/coach` (default: `claude-sonnet-4-6`) |
| `webhook_secret` | No | Must match the secret in intervals.icu webhook settings |
| `ha_mobile_service` | No | HA notify service for mobile push (e.g. `notify.mobile_app_my_phone`) |
| `read_only` | No | Set to `true` to disable all write tools. Default: `false`. |

---

## Development

```bash
uv run --with pytest --with pytest-asyncio --with httpx --with starlette --with mcp --with cryptography \
    python -m pytest tests/ -v
```

---

## License

MIT — see [LICENSE](LICENSE). Coaching metrics (RI, ACWR) derived from the [Section 11 framework](https://github.com/CrankAddict/section-11).
