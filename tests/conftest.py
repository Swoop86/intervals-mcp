"""
Set required environment variables before any module imports.
mcp_server.py raises SystemExit(1) at import time if ATHLETE_ID/API_KEY are missing.
"""
import os
import sys

os.environ.setdefault("INTERVALS_ATHLETE_ID", "i999999")
os.environ.setdefault("INTERVALS_API_KEY", "test_api_key")
os.environ.setdefault("COACH_SECRET", "test_coach_secret")
os.environ.setdefault("INTERVALS_WEBHOOK_SECRET", "test_webhook_secret")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
os.environ.setdefault("INTERVALS_PORT", "8765")
os.environ.setdefault("HA_TOKEN", "")
os.environ.setdefault("CLAUDE_MODEL", "claude-sonnet-4-6")
os.environ.setdefault("TOKEN_EXPIRY_DAYS", "1")

# Add addon/ so test files can import mcp_server / claude_coach directly
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addon"))
