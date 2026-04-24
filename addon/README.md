# Intervals.icu MCP Server

Connects your [intervals.icu](https://intervals.icu) training data to Claude AI via the Model Context Protocol (MCP). Ask Claude to review your training, prepare for a race, and schedule sessions around the weather — it reads and writes your real data.

## Features

- **MCP endpoint** — expose your training data to Claude.ai or Claude Desktop
- **Athlete profile** — store paces, limiters, location, and notes so Claude knows you
- **Race goal / event prep** — set a target event and coaching shifts to periodized Base → Build → Peak → Taper mode
- **Weather-aware scheduling** — Claude fetches the forecast for your location and can move sessions to better days
- **Structured workouts** — create Garmin-ready interval sessions from Claude
- **Webhook receiver** — real-time events from intervals.icu push HA notifications when a workout syncs
- **`/coach` endpoint** — trigger an automated Claude coaching review from an HA automation

## Setup

### 1. intervals.icu

- Find your **Athlete ID** in the URL when logged in: `intervals.icu/athletes/iXXXXXX`
- Create an **API key**: Settings → API Access

### 2. Addon configuration

| Option | Required | Description |
|--------|----------|-------------|
| `athlete_id` | Yes | Your athlete ID (e.g. `i123456`) |
| `api_key` | Yes | intervals.icu API key |
| `port` | No | Port (default: 8765) |
| `coach_secret` | No | Password for the OAuth login form — gates access to `/mcp`. **Strongly recommended.** |
| `token_expiry_days` | No | OAuth token lifetime in days (default: 180). POST `/revoke` with `X-Coach-Token` to clear all tokens immediately. |
| `anthropic_api_key` | No | Required for the `/coach` HTTP endpoint |
| `claude_model` | No | Model for `/coach` (default: `claude-sonnet-4-6`) |
| `webhook_secret` | No | Must match the secret in intervals.icu webhook settings |
| `ha_mobile_service` | No | HA notify service for push notifications (e.g. `notify.mobile_app_my_phone`) |
| `read_only` | No | `true` to disable write tools. Default: `false`. |

### 3. Connect to Claude.ai

Expose the addon externally (e.g. via Cloudflare Tunnel) and add it as a remote MCP server in Claude.ai settings:

```
https://your-tunnel-domain.com/mcp
```

Claude.ai starts an OAuth login flow and redirects to `/authorize`. Enter your `coach_secret`, and Claude.ai receives a bearer token valid for `token_expiry_days` days.

> **Cloudflare Tunnel:** If you have geo-based security rules, ensure requests from US-based servers (Anthropic) are not blocked — they will fail silently otherwise.

### 4. Set up your profile

Tell Claude about yourself once and it personalises all future coaching:

> *"Set up my profile — I'm 34, running ~40km/week, easy pace 6:00/km, threshold 4:45/km, training Mon/Tue/Thu/Sat/Sun, based in Oslo, Norway."*

Claude calls `update_profile`. Your location enables weather lookups.

### 5. Set a race goal (optional)

> *"I've entered the Bergen City Marathon on 15 September. Sub-4 hours, first marathon."*

Claude calls `set_race_goal` and automatically determines your current training phase (Base/Build/Peak/Taper/Race week) based on weeks remaining. Every coaching review from that point is framed around the goal. When the race is done: *"Clear my race goal"*.

### 6. Set a coaching methodology (optional)

Without a methodology, Claude applies general endurance principles. Choose a preset for a consistent coaching philosophy across every conversation:

> *"Set my coaching style to polarized."*

| Preset | Description |
|--------|-------------|
| `polarized` | 80% easy (Z1), 20% hard (Z3). Avoid threshold. Fartlek + VO2max intervals. |
| `maffetone` | Train below MAF HR (180 − age) to build aerobic base. No intensity until base is solid. |
| `jack_daniels` | VDOT-based paces from recent race time. Five intensity zones. |
| `norwegian` | Two controlled threshold sessions/week at ~75–80% HRmax. High volume, all else easy. |
| `pyramidal` | ~70% easy, ~20% threshold, ~10% hard. Traditional approach for recreational runners. |
| `custom` | Define your own training philosophy in free text. |

The selected methodology is stored in the profile and applied to every coaching review, including automated `/coach` reviews.

### 7. Weather-aware planning

Once your location is set, Claude can check the forecast before scheduling:

> *"Can you check the weather and move my long run to the best day this week?"*

Claude calls `get_weather` (Open-Meteo, no API key needed) and `get_planned_workouts`, then reschedules accordingly. It will also ask about rain gear for borderline conditions rather than just cancelling a session.

### 8. Webhook (optional)

In intervals.icu: **Settings → Developer Settings → Webhooks**

- URL: `https://your-tunnel-domain.com/webhook`
- Secret: same value as `webhook_secret` in the addon config

After each workout syncs, Claude reviews it automatically and pushes an HA notification with analysis and any plan adjustments.

## Security

- **`coach_secret` is strongly recommended** when the addon is reachable from the internet.
- Rate limiting: 5 req/s per IP (burst 10). Login lockout: 5 failed attempts → 1 hour block.
- OAuth tokens are stored as SHA-256 hashes — the raw token is never persisted.
- Athlete profile and race goal are stored as **Fernet-encrypted JSON**, keyed from `coach_secret`.
- Use `read_only: true` to prevent any calendar modifications.

## Support

[GitHub](https://github.com/Swoop86/intervals-mcp)
