#!/usr/bin/with-contenv bashio

# Read all options from HA addon config
ATHLETE_ID=$(bashio::config 'athlete_id')
API_KEY=$(bashio::config 'api_key')
PORT=$(bashio::config 'port')
WEBHOOK_SECRET=$(bashio::config 'webhook_secret')
WEBHOOK_HEADER_SECRET=$(bashio::config 'webhook_header_secret')
ANTHROPIC_API_KEY=$(bashio::config 'anthropic_api_key')
COACH_SECRET=$(bashio::config 'coach_secret')
CLAUDE_MODEL=$(bashio::config 'claude_model')
HA_MOBILE_SERVICE=$(bashio::config 'ha_mobile_service')
CF_TEAM_DOMAIN=$(bashio::config 'cf_team_domain')
CF_ACCESS_AUD=$(bashio::config 'cf_access_aud')

# Fail fast on missing required config
if [ -z "$ATHLETE_ID" ] || [ -z "$API_KEY" ]; then
  bashio::log.fatal "athlete_id and api_key are required"
  exit 1
fi

# Export all for Python
export INTERVALS_ATHLETE_ID="$ATHLETE_ID"
export INTERVALS_API_KEY="$API_KEY"
export INTERVALS_PORT="$PORT"
export INTERVALS_WEBHOOK_SECRET="$WEBHOOK_SECRET"
export INTERVALS_WEBHOOK_HEADER_SECRET="$WEBHOOK_HEADER_SECRET"
export ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"
export COACH_SECRET="$COACH_SECRET"
export CLAUDE_MODEL="$CLAUDE_MODEL"
export HA_MOBILE_SERVICE="$HA_MOBILE_SERVICE"
export CF_TEAM_DOMAIN="$CF_TEAM_DOMAIN"
export CF_ACCESS_AUD="$CF_ACCESS_AUD"
export HA_TOKEN="${SUPERVISOR_TOKEN}"

# Security warnings
if [ -z "$CF_ACCESS_AUD" ]; then
  bashio::log.warning "CF_ACCESS_AUD not set — /mcp endpoint has no Cloudflare Access validation!"
fi
if [ -z "$COACH_SECRET" ]; then
  bashio::log.warning "COACH_SECRET not set — /coach endpoint has no authentication!"
fi
if [ -z "$WEBHOOK_SECRET" ]; then
  bashio::log.warning "WEBHOOK_SECRET not set — /webhook accepts unsigned payloads!"
fi
if [ -z "$ANTHROPIC_API_KEY" ]; then
  bashio::log.warning "ANTHROPIC_API_KEY not set — /coach endpoint cannot run coaching"
fi
if [ -z "$HA_MOBILE_SERVICE" ]; then
  bashio::log.info "ha_mobile_service not set — mobile push notifications disabled"
fi

bashio::log.info "Model: $CLAUDE_MODEL"
if [ -n "$CF_TEAM_DOMAIN" ] && [ "$CF_TEAM_DOMAIN" != "null" ]; then
  bashio::log.info "CF Access domain: $CF_TEAM_DOMAIN"
fi
bashio::log.info "Starting Intervals.icu MCP Server on port $PORT..."
exec python3 -u /app/mcp_server.py
