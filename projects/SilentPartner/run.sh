#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
NANOCLAW_DIR="$(cd "$PROJECT_DIR/../.." && pwd)"
ENV_FILE="$NANOCLAW_DIR/.env"

# Load onecli credentials
ONECLI_URL=$(grep ONECLI_URL "$ENV_FILE" | cut -d= -f2)
ONECLI_API_KEY=$(grep ONECLI_API_KEY "$ENV_FILE" | cut -d= -f2)

# Get proxy config from onecli SDK
PROXY_CONFIG=$(ONECLI_URL="$ONECLI_URL" ONECLI_API_KEY="$ONECLI_API_KEY" node -e "
const { OneCLI } = require('$NANOCLAW_DIR/node_modules/@onecli-sh/sdk');
const onecli = new OneCLI({ url: process.env.ONECLI_URL, apiKey: process.env.ONECLI_API_KEY });
onecli.getContainerConfig().then(c => {
  process.stdout.write(JSON.stringify({ proxy: c.env.HTTPS_PROXY, ca: c.caCertificate }));
}).catch(e => { console.error(e.message); process.exit(1); });
")

PROXY_URL=$(echo "$PROXY_CONFIG" | python3 -c "import sys,json; print(json.load(sys.stdin)['proxy'])")
CA_CERT=$(echo "$PROXY_CONFIG" | python3 -c "import sys,json; print(json.load(sys.stdin)['ca'])")

CA_CERT_FILE=$(mktemp /tmp/onecli-ca-XXXXXX.pem)
printf '%s' "$CA_CERT" > "$CA_CERT_FILE"
trap "rm -f $CA_CERT_FILE" EXIT

TTY_FLAG=$([ -t 0 ] && echo "-it" || echo "")
docker run --rm $TTY_FLAG \
  --add-host host.docker.internal:host-gateway \
  -v "$PROJECT_DIR:/app" \
  -v "silentpartner-whisper:/root/.cache/huggingface" \
  -v "$CA_CERT_FILE:/tmp/onecli-ca.pem:ro" \
  -e "HTTPS_PROXY=$PROXY_URL" \
  -e "HTTP_PROXY=$PROXY_URL" \
  -e "NODE_EXTRA_CA_CERTS=/tmp/onecli-ca.pem" \
  -e "NODE_USE_ENV_PROXY=1" \
  -e "ANTHROPIC_API_KEY=placeholder" \
  silentpartner \
  python main.py "$@"
