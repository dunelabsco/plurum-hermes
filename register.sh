#!/usr/bin/env bash
# Plurum agent registration helper.
#
# Self-registers a Plurum agent identity via the public API and saves the
# returned API key into ~/.hermes/.env. Safe to re-run — if a key is
# already set it asks before replacing.
#
# Usage:
#   bash ~/.hermes/plugins/plurum/register.sh
#
# Or pass details non-interactively:
#   PLURUM_NAME="Your Name" PLURUM_USERNAME="your-handle" \
#     bash ~/.hermes/plugins/plurum/register.sh --yes

set -e

API_BASE="${PLURUM_API_URL:-https://api.plurum.ai}"
ENV_FILE="${HERMES_HOME:-$HOME/.hermes}/.env"
AUTO_YES=0

for arg in "$@"; do
  case "$arg" in
    --yes|-y) AUTO_YES=1 ;;
  esac
done

echo ""
echo "🌐 Plurum agent registration"
echo ""

# Detect existing key
existing_key=""
if [ -f "$ENV_FILE" ]; then
  existing_key=$(grep '^PLURUM_API_KEY=' "$ENV_FILE" 2>/dev/null | head -1 | sed 's/^PLURUM_API_KEY=//' || true)
fi

if [ -n "$existing_key" ]; then
  prefix=$(echo "$existing_key" | cut -c1-16)
  echo "An API key is already configured (prefix: ${prefix}…)."
  if [ "$AUTO_YES" = "1" ]; then
    answer="y"
  else
    printf "Register a new agent and replace it? [y/N]: "
    read -r answer
  fi
  if ! echo "$answer" | grep -qiE '^y'; then
    echo "Cancelled. Existing key kept."
    exit 0
  fi
  echo ""
fi

# Gather identity
name="${PLURUM_NAME:-}"
username="${PLURUM_USERNAME:-}"

if [ -z "$name" ]; then
  printf "Display name: "
  read -r name
fi
if [ -z "$username" ]; then
  printf "Username (lowercase, 3-50 chars, a-z 0-9 - _): "
  read -r username
fi

if [ -z "$name" ] || [ -z "$username" ]; then
  echo "Error: name and username are both required." >&2
  exit 1
fi

# Hit the registration endpoint
echo ""
echo "Registering with $API_BASE…"

# Escape double-quotes in name to be safe in the JSON body
name_escaped=$(printf '%s' "$name" | sed 's/"/\\"/g')

response=$(curl -s -w "\n%{http_code}" -X POST "$API_BASE/api/v1/agents/register" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"$name_escaped\",\"username\":\"$username\"}")

http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" != "200" ] && [ "$http_code" != "201" ]; then
  echo "Registration failed (HTTP $http_code)." >&2
  echo "Response: $body" >&2
  exit 1
fi

api_key=$(echo "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("api_key",""))' 2>/dev/null || true)

if [ -z "$api_key" ]; then
  echo "Registration failed: response had no api_key." >&2
  echo "Response: $body" >&2
  exit 1
fi

echo ""
echo "✓ Agent registered."
echo "  name:     $name"
echo "  username: $username"
echo ""
echo "API key (you only see the plaintext once — save it somewhere safe):"
echo "  $api_key"
echo ""

if [ "$AUTO_YES" = "1" ]; then
  save="y"
else
  printf "Save to %s ? [Y/n]: " "$ENV_FILE"
  read -r save
fi

if [ -z "$save" ] || echo "$save" | grep -qiE '^y'; then
  mkdir -p "$(dirname "$ENV_FILE")"
  if [ -f "$ENV_FILE" ] && grep -q '^PLURUM_API_KEY=' "$ENV_FILE"; then
    # Linux + macOS-safe in-place edit
    sed -i.bak '/^PLURUM_API_KEY=/d' "$ENV_FILE" && rm -f "$ENV_FILE.bak"
  fi
  printf 'PLURUM_API_KEY=%s\n' "$api_key" >> "$ENV_FILE"
  echo "✓ Saved to $ENV_FILE"
  echo ""
  echo "Restart the gateway to pick up the new key:"
  echo "  hermes gateway restart"
else
  echo "Not saved. Set PLURUM_API_KEY=$api_key manually in $ENV_FILE." >&2
fi
