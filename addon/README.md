# Intervals.icu MCP Server

Connects your [intervals.icu](https://intervals.icu) training data to Claude AI via the Model Context Protocol (MCP). Ask Claude to review your training, check your recovery, or adjust your plan — it pulls your real fitness data on demand.

## Features

- **MCP tools for Claude.ai / Claude Desktop**
  - `review_training` — fetch 28 days of activities, 14 days of HRV/sleep/wellness, and CTL/ATL/TSB so Claude can coach you
  - `create_workout` — add a new planned workout to your calendar
  - `update_workout` — modify a planned workout directly in your calendar
  - `delete_workout` — remove a planned workout

- **Webhook receiver** — real-time events from intervals.icu push HA events and mobile notifications when a workout syncs or is analysed

- **`/coach` HTTP endpoint** — trigger an automated Claude coaching review from an HA automation

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
| `anthropic_api_key` | No | Needed for the `/coach` HTTP endpoint |
| `coach_secret` | No | Password for the built-in OAuth login form — gates access to `/mcp` |
| `token_expiry_days` | No | How long OAuth tokens stay valid in days (default: 180). POST `/revoke` with `X-Coach-Token` to invalidate all tokens immediately. |
| `claude_model` | No | Model for `/coach` (default: claude-sonnet-4-6) |
| `webhook_secret` | No | Must match the secret set in intervals.icu webhook settings |
| `ha_mobile_service` | No | HA notify service for push notifications (e.g. `notify.mobile_app_my_phone`) |

### 3. Connect to Claude.ai

Expose the addon externally (e.g. via Cloudflare Tunnel) and add it as a remote MCP server in Claude.ai settings:

```
https://your-tunnel-domain.com/mcp
```

Claude.ai will redirect you to `/authorize` — a login page hosted by the addon itself. Enter your `coach_secret`, and Claude.ai receives a token it uses for all `/mcp` requests. Tokens expire after 1 hour.

Then ask Claude: *"Review my training"* — it will call `review_training` and give you a coaching response using your real data.

### 4. Webhook (optional)

In intervals.icu: **Settings → Developer Settings → Webhooks**

- URL: `https://your-tunnel-domain.com/webhook`
- Set the same secret as `webhook_secret` in the addon config

## Automations

See the included `automations.yaml` for ready-made HA automations for mobile notifications and manual coaching triggers.

## Support

[GitHub](https://github.com/Swoop86/intervals-mcp)
