#!/bin/bash
set -e

# Trust the onecli CA cert so httpx can verify the MITM proxy
if [ -f /tmp/onecli-ca.pem ]; then
  python3 -c "import certifi; open(certifi.where(), 'a').write(open('/tmp/onecli-ca.pem').read())"
fi

exec "$@"
