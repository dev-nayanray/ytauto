#!/bin/bash
# ── ytauto container entrypoint ────────────────────────────────────────────────
# Injects OAuth secrets from environment variables into files before starting
# the server. Set these in your container platform's environment / secrets UI.
#
#   TOKEN_JSON          — raw JSON content of token.json
#   CLIENT_SECRETS_JSON — raw JSON content of client_secrets.json
#
# Alternative: base64-encoded versions (safer for multiline JSON in some platforms)
#   TOKEN_JSON_B64          — base64(token.json)
#   CLIENT_SECRETS_JSON_B64 — base64(client_secrets.json)
#
# Generate base64 values locally with:
#   base64 -w 0 token.json
#   base64 -w 0 client_secrets.json
# ──────────────────────────────────────────────────────────────────────────────
set -e

echo "[entrypoint] ytauto starting…"

# ── token.json (YouTube + Drive + Sheets OAuth token) ──────────────────────────
if [ -n "$TOKEN_JSON_B64" ]; then
    echo "[entrypoint] Decoding TOKEN_JSON_B64 → token.json"
    echo "$TOKEN_JSON_B64" | base64 -d > /app/token.json
elif [ -n "$TOKEN_JSON" ]; then
    echo "[entrypoint] Writing TOKEN_JSON → token.json"
    printf '%s' "$TOKEN_JSON" > /app/token.json
fi

# ── client_secrets.json (Google OAuth app credentials) ────────────────────────
if [ -n "$CLIENT_SECRETS_JSON_B64" ]; then
    echo "[entrypoint] Decoding CLIENT_SECRETS_JSON_B64 → client_secrets.json"
    echo "$CLIENT_SECRETS_JSON_B64" | base64 -d > /app/client_secrets.json
elif [ -n "$CLIENT_SECRETS_JSON" ]; then
    echo "[entrypoint] Writing CLIENT_SECRETS_JSON → client_secrets.json"
    printf '%s' "$CLIENT_SECRETS_JSON" > /app/client_secrets.json
fi

# ── Verify critical env vars are present ───────────────────────────────────────
missing=""
for var in ANTHROPIC_API_KEY PEXELS_API_KEY; do
    if [ -z "${!var}" ]; then
        missing="$missing $var"
    fi
done
if [ -n "$missing" ]; then
    echo "[entrypoint] WARNING: missing env vars:$missing"
    echo "[entrypoint] Dashboard will start but pipeline may fail."
fi

echo "[entrypoint] Handing off to: $*"
exec "$@"
