"""Central Anthropic client factory for host scripts.

Returns a client whose messages.create / messages.stream auto-inject the
Claude Code system-prompt prefix required for OAuth Max-tier rate limits.
Without this prefix, OAuth tokens (whether routed via OneCLI proxy or
passed directly) get demoted to a stricter throttle bucket that 429s on
Sonnet/Opus even at low load. See memory record
feedback_oauth_tier_requires_claude_code_prefix.md.

Usage:
    from claude_oauth import make_client
    client = make_client()                  # auto-resolves env tokens
    client = make_client(agent="velikov")   # via OneCLI proxy
    client = make_client(api_key=token)     # explicit
    client = make_client(auth_token=token)
"""

import os
from anthropic import Anthropic

CLAUDE_CODE_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


def inject_prefix(system):
    """Idempotently prepend the Claude Code prefix to the system prompt.
    Accepts None, str, or list of content blocks. Cache breakpoints stay
    valid as long as the prefix block sits before the cached block."""
    if system is None or system == "":
        return system
    if isinstance(system, str):
        if system.startswith(CLAUDE_CODE_PREFIX):
            return system
        return CLAUDE_CODE_PREFIX + "\n\n" + system
    if isinstance(system, list) and system:
        first = system[0]
        if isinstance(first, dict) and first.get("text", "").startswith(CLAUDE_CODE_PREFIX):
            return system
        return [{"type": "text", "text": CLAUDE_CODE_PREFIX}] + list(system)
    return system


def _wrap(client):
    orig_create = client.messages.create
    orig_stream = client.messages.stream

    def create(**kwargs):
        if "system" in kwargs:
            kwargs["system"] = inject_prefix(kwargs["system"])
        return orig_create(**kwargs)

    def stream(**kwargs):
        if "system" in kwargs:
            kwargs["system"] = inject_prefix(kwargs["system"])
        return orig_stream(**kwargs)

    client.messages.create = create
    client.messages.stream = stream
    return client


def make_client(*, agent=None, api_key=None, auth_token=None, http_client=None,
                default_headers=None):
    """Build an Anthropic client with Claude Code prefix auto-injection.

    If `agent` is given, configures the OneCLI proxy for that agent first
    and uses a placeholder auth_token so the proxy substitutes the real one.

    If neither api_key nor auth_token is given (and no agent), falls back
    to the CLAUDE_CODE_OAUTH_TOKEN env var, then ANTHROPIC_API_KEY.
    """
    if agent is not None:
        from onecli_proxy import apply_for_agent  # type: ignore
        apply_for_agent(agent)
        if auth_token is None and api_key is None:
            auth_token = "onecli-placeholder"

    if auth_token is None and api_key is None:
        auth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if not auth_token:
            api_key = os.environ.get("ANTHROPIC_API_KEY")

    kwargs = {}
    if auth_token is not None:
        kwargs["auth_token"] = auth_token
    if api_key is not None:
        kwargs["api_key"] = api_key
    if http_client is not None:
        kwargs["http_client"] = http_client
    if default_headers is not None:
        kwargs["default_headers"] = default_headers

    return _wrap(Anthropic(**kwargs))
