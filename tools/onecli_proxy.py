"""onecli_proxy.py — host-side OneCLI proxy setup for direct API tools.

Hits OneCLI's /api/container-config endpoint with the user's API key and a
target agent identifier, then applies the returned proxy env vars and CA cert
to the current process so subsequent HTTP calls (Anthropic SDK, ElevenLabs
requests, etc.) route through the OneCLI gateway. The gateway injects the
real per-agent credentials; this script never sees or stores them.

Usage from another script:

    from onecli_proxy import apply_for_agent
    apply_for_agent("stella")
    # subsequent anthropic.Anthropic(api_key="placeholder") calls now route
    # through the proxy and pick up Stella's real Anthropic credential.

Reads ONECLI_URL and ONECLI_API_KEY from environment or the repo .env file.
"""

import os
import tempfile
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATHS = [
    r"\\wsl.localhost\Ubuntu\home\aurellian\nanoclaw\.env",
    str(REPO_ROOT / ".env"),
]


def _env(key):
    val = os.environ.get(key)
    if val:
        return val
    for path in ENV_PATHS:
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(key + "="):
                        return line.split("=", 1)[1]
        except FileNotFoundError:
            continue
    return None


def apply_for_agent(agent_identifier=None):
    """Fetch OneCLI container-config for the given agent identifier and apply
    it to os.environ. Subsequent HTTP calls in this process route through the
    OneCLI proxy with credentials injected. Returns the path to the CA cert.

    Pass `agent_identifier=None` (or omit) to use the Default Agent."""
    onecli_url = _env("ONECLI_URL")
    onecli_api_key = _env("ONECLI_API_KEY")
    if not onecli_url or not onecli_api_key:
        raise RuntimeError(
            "OneCLI not configured: ONECLI_URL or ONECLI_API_KEY missing from env / .env"
        )

    params = {"agent": agent_identifier} if agent_identifier else {}
    resp = requests.get(
        f"{onecli_url.rstrip('/')}/api/container-config",
        params=params,
        headers={"Authorization": f"Bearer {onecli_api_key}"},
        timeout=15,
    )
    resp.raise_for_status()
    config = resp.json()

    ca_path = Path(tempfile.gettempdir()) / "onecli-host-ca.pem"
    ca_path.write_text(config["caCertificate"])

    for k, v in config["env"].items():
        if k.lower() in ("https_proxy", "http_proxy"):
            v = v.replace("host.docker.internal", "127.0.0.1")
        os.environ[k] = v

    os.environ["SSL_CERT_FILE"] = str(ca_path)
    os.environ["REQUESTS_CA_BUNDLE"] = str(ca_path)
    return str(ca_path)
