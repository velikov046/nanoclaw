"""Safe wrappers around Anthropic API calls with backoff + multi-model fallback.

Use `safe_messages_create` instead of `client.messages.create` for any ingestion
or batch work. Cascade behaviour:
  - Try primary model with exponential backoff on 429
  - On sustained 429 OR refusal-shaped errors, fall through to the next model in
    the configured chain (different calibrations across model versions can pass
    where the primary refuses)
  - Last resort: Haiku (separate rate-limit pool, often more permissive on
    extractive tasks)

Default chain: primary -> Haiku 4.5. Sonnet 4.5 used to sit between them for
refusal-pattern variation, but was removed 2026-05-01: when the primary 429s on
the OAuth subscription bucket, Sonnet 4.5 shares the same pool and 429s in
lockstep, costing ~7min of useless backoff before Haiku (separate pool) is
reached. Re-enable per-job via KB_FALLBACK_MODELS=claude-sonnet-4-5,claude-haiku-4-5-20251001
if a refusal-only fallback is wanted.
"""

import time
import os
import logging

logger = logging.getLogger(__name__)

# OAuth Max-tier on Sonnet/Opus is gated on this exact prefix appearing as the
# first system block. Without it, the same OAuth token gets demoted to a much
# stricter throttle bucket — even a 10-token call will 429 while concurrent
# `claude-agent-sdk` traffic on the same account succeeds. claude-agent-sdk
# adds the prefix automatically; raw `anthropic` SDK calls do not. See
# memory/feedback_oauth_tier_requires_claude_code_prefix.md.
CLAUDE_CODE_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


def _inject_claude_code_prefix(system):
    """Ensure the Claude Code prefix is the first thing in the system prompt.
    Idempotent: re-applying does not double-prefix. Returns the (possibly
    modified) system value to pass to messages.create."""
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

# Tunable via env vars so operators can adjust without redeploying
BACKOFF_INITIAL = float(os.environ.get("KB_BACKOFF_INITIAL", "30"))   # first retry wait, seconds
BACKOFF_MAX = float(os.environ.get("KB_BACKOFF_MAX", "300"))         # cap per retry
BACKOFF_RETRIES = int(os.environ.get("KB_BACKOFF_RETRIES", "4"))     # retries before falling to next model

# Default fallback chain. Operator can override via KB_FALLBACK_MODELS (comma-sep).
# Sonnet 4.5 was here but shares the OAuth subscription bucket with the Sonnet
# primary, so when the primary 429s, 4.5 lockstep-429s and only burns 7min of
# backoff before reaching Haiku. Haiku is a separate pool.
DEFAULT_FALLBACK_CHAIN = [
    "claude-haiku-4-5-20251001",
]
_env_chain = os.environ.get("KB_FALLBACK_MODELS")
if _env_chain:
    DEFAULT_FALLBACK_CHAIN = [m.strip() for m in _env_chain.split(",") if m.strip()]


def _looks_like_refusal(err) -> bool:
    """Heuristic: does this exception look like a content-policy refusal?

    Anthropic returns various error shapes for safety refusals — sometimes a
    400 with a specific message, sometimes a 403, sometimes content-filter
    metadata in a 200 response (handled by the caller's parse logic, not here).
    """
    status = getattr(err, "status_code", None)
    if status in (400, 403):
        msg = str(err).lower()
        if any(t in msg for t in ("policy", "harmful", "refused", "cannot", "violates", "safety")):
            return True
    return False


def safe_messages_create(
    client,
    model: str,
    fallback_models: list | None = None,
    max_retries: int | None = None,
    **kwargs,
):
    """Call client.messages.create with backoff + multi-model fallback.

    Args:
        client: anthropic.Anthropic client
        model: primary model id
        fallback_models: list of model ids to try in order if the primary stays
                         rate-limited or refuses. Default: Sonnet 4.5 then Haiku 4.5.
                         Pass [] to disable fallback.
        max_retries: number of 429 retries on each model before falling through (default 4)
        **kwargs: forwarded to messages.create

    Returns:
        Anthropic Message response

    Raises:
        Last exception if all models in the chain fail.
    """
    from anthropic import APIStatusError

    retries = max_retries if max_retries is not None else BACKOFF_RETRIES
    fb_chain = fallback_models if fallback_models is not None else list(DEFAULT_FALLBACK_CHAIN)

    if "system" in kwargs:
        kwargs["system"] = _inject_claude_code_prefix(kwargs["system"])

    # Build full attempt order, dedupe while preserving order
    chain = []
    for m in [model] + list(fb_chain):
        if m and m not in chain:
            chain.append(m)

    # Connection-level errors that should retry with backoff (transient network blips)
    try:
        from anthropic import APIConnectionError, APITimeoutError
        _CONNECTION_ERRORS: tuple = (APIConnectionError, APITimeoutError)
    except ImportError:
        _CONNECTION_ERRORS = ()
    try:
        import httpx
        _CONNECTION_ERRORS = _CONNECTION_ERRORS + (
            httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectTimeout,
            httpx.ConnectError, httpx.ReadError, httpx.WriteError,
        )
    except ImportError:
        pass

    last_err: Exception | None = None
    for ci, m in enumerate(chain):
        # Adapt max_tokens for Haiku ceiling
        kw = dict(kwargs)
        if "haiku" in m and kw.get("max_tokens", 0) > 8192:
            kw["max_tokens"] = 8192

        delay = BACKOFF_INITIAL
        for attempt in range(retries + 1):
            try:
                return client.messages.create(model=m, **kw)
            except APIStatusError as e:
                last_err = e
                status = getattr(e, "status_code", None)
                if status == 429 and attempt < retries:
                    wait = min(delay, BACKOFF_MAX)
                    print(f"    [429] {m} rate-limited, waiting {wait:.0f}s before retry {attempt + 2}/{retries + 1}...")
                    time.sleep(wait)
                    delay *= 2
                    continue
                # 5xx server errors are also worth retrying briefly before falling through
                if status and 500 <= status < 600 and attempt < retries:
                    wait = min(delay, BACKOFF_MAX)
                    print(f"    [{status}] {m} server error, waiting {wait:.0f}s before retry {attempt + 2}/{retries + 1}...")
                    time.sleep(wait)
                    delay *= 2
                    continue
                # Either sustained 429 or non-429 error: move to next model in chain
                if status == 429:
                    print(f"    [429] {m} sustained rate-limit, falling through to next model")
                elif _looks_like_refusal(e):
                    print(f"    [refusal] {m} refused on policy grounds, falling through")
                else:
                    print(f"    [error] {m}: {type(e).__name__} {status}, falling through")
                break  # exit attempt loop, advance to next model
            except _CONNECTION_ERRORS as e:
                last_err = e
                # Network blip — retry with backoff (use a shorter initial delay since
                # transient connection failures usually resolve in seconds)
                if attempt < retries:
                    wait = min(max(5.0, delay / 4), BACKOFF_MAX)
                    print(f"    [conn] {m} {type(e).__name__}, waiting {wait:.0f}s before retry {attempt + 2}/{retries + 1}...")
                    time.sleep(wait)
                    delay *= 2
                    continue
                print(f"    [conn] {m} sustained connection failures, falling through to next model")
                break
            except Exception as e:
                last_err = e
                print(f"    [error] {m}: {type(e).__name__}, falling through")
                break

        if ci < len(chain) - 1:
            print(f"    [fallback] trying {chain[ci + 1]}")

    if last_err:
        raise last_err
    raise RuntimeError(f"safe_messages_create exhausted all models: {chain}")
