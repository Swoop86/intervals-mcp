# Intervals.icu MCP Server

A Home Assistant addon that connects your [intervals.icu](https://intervals.icu) training data to Claude AI via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). Ask Claude to review your training, and it pulls your real fitness data — activities, HRV, sleep, CTL/ATL/TSB — to give you a personalised coaching response.

## What it does

- **MCP endpoint** — expose your training data to Claude.ai or Claude Desktop as MCP tools
- **`review_training`** — Claude fetches 28 days of activities, 14 days of wellness, and your fitness metrics on demand
- **`update_workout`** — Claude can reschedule or modify planned workouts directly in your calendar
- **`delete_workout`** — Claude can remove planned workouts when rest is needed
- **Webhook receiver** — receive real-time events from intervals.icu (activity uploaded, analyzed, calendar updated) and push them to Home Assistant as events and mobile notifications
- **`/coach` endpoint** — trigger an automated coaching review via HTTP (e.g. from an HA automation)

## How it works

```
Claude.ai ──MCP──► /mcp ──► intervals.icu API
                              └─► HRV / sleep / wellness
                              └─► Activities / CTL / ATL / TSB
                              └─► Planned workouts

intervals.icu ──webhook──► /webhook ──► HA events + mobile push
```

When you ask Claude "review my training", it calls `review_training`, gets your data, and responds using the Recovery Index (RI) and ACWR load metrics to assess readiness and suggest adjustments.

## Installation

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add: `https://github.com/Swoop86/intervals-mcp`
3. Install **Intervals.icu MCP Server**
4. Set `athlete_id` and `api_key` in the addon configuration
5. Start the addon

## Connecting to Claude.ai

Expose the addon via [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) and add it as a remote MCP server in Claude.ai settings:

```
https://your-tunnel-domain.com/mcp
```

Set `cf_team_domain` and `cf_access_aud` in the addon config to protect the endpoint with Cloudflare Access JWT validation.

## Configuration

| Option | Required | Description |
|--------|----------|-------------|
| `athlete_id` | Yes | Your intervals.icu athlete ID (found in the URL when logged in) |
| `api_key` | Yes | intervals.icu API key (Settings → API) |
| `port` | No | Port to listen on (default: 8765) |
| `anthropic_api_key` | No | Anthropic API key — required for the `/coach` HTTP endpoint |
| `coach_secret` | No | Token to protect the `/coach` endpoint |
| `claude_model` | No | Claude model for `/coach` (default: claude-sonnet-4-6) |
| `webhook_secret` | No | Secret configured in intervals.icu webhook settings |
| `ha_mobile_service` | No | HA notify service for mobile push (e.g. `notify.mobile_app_my_phone`) |
| `cf_team_domain` | No | Cloudflare Access team domain (e.g. `yourteam.cloudflareaccess.com`) |
| `cf_access_aud` | No | Cloudflare Access application AUD for JWT validation |

## Development

```bash
# Run tests
uv run --with pytest --with pytest-asyncio --with httpx --with starlette --with mcp \
    python -m pytest tests/ -v
```

## License

MIT — see [LICENSE](LICENSE). Coaching metrics (RI, ACWR) derived from the [Section 11 framework](https://github.com/CrankAddict/section-11).
