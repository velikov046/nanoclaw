import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path


# Trailing advisory/tips headers emitted by the analysis prompts.
# extract_advice() takes everything from the first line matching one of these
# through end-of-text — that's the only part TTS speaks.
ADVICE_HEADERS = (
    "LIBERAL ADVISORY",
    "FASCIST ADVISORY",
    "GOOD ADVISORY",
    "EVIL ADVISORY",
    "ST RECOMMENDATIONS",
    "TACTICAL ADVICE",
)
_ADVICE_RE = re.compile(
    r"^.*\b(?:" + "|".join(ADVICE_HEADERS) + r")\b.*$",
    re.IGNORECASE | re.MULTILINE,
)


def extract_advice(analysis: str) -> str:
    """Return only the trailing advisory section. Empty string if none found
    (e.g. spectator role, or full-debate mode where no advice is requested)."""
    m = _ADVICE_RE.search(analysis)
    if not m:
        return ""
    return analysis[m.start():].strip()


def strip_markdown_for_tts(text: str) -> str:
    """Strip markdown decoration so the TTS engine doesn't read it literally."""
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)         # **bold**
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)  # *italic*
    text = re.sub(r"`([^`]+)`", r"\1", text)               # `code`
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)  # ## headers
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)  # bullets
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)  # 1. ordered lists
    return text


def _vad_tail_silent(pcm, sample_rate: int = 16000,
                     tail_ms: int = 600, frame_ms: int = 30,
                     aggressiveness: int = 2) -> bool:
    """True if the last `tail_ms` of pcm contain no voiced frames.
    Lazy-imports webrtcvad so the rest of the app still runs without it."""
    try:
        import webrtcvad
    except ImportError:
        return False
    import numpy as np
    if pcm.size < int(sample_rate * tail_ms / 1000):
        return False
    tail = pcm[-int(sample_rate * tail_ms / 1000):]
    pcm16 = (np.clip(tail, -1, 1) * 32767).astype("<i2").tobytes()
    vad = webrtcvad.Vad(aggressiveness)
    frame_bytes = int(sample_rate * frame_ms / 1000) * 2
    for i in range(0, len(pcm16) - frame_bytes + 1, frame_bytes):
        if vad.is_speech(pcm16[i:i+frame_bytes], sample_rate):
            return False
    return True


def _is_redundant(new_text: str, recent: list[str], threshold: float = 0.72) -> bool:
    """SequenceMatcher dedup against the last N fast outputs."""
    import difflib
    norm = " ".join(new_text.lower().split())
    for prev in recent:
        if difflib.SequenceMatcher(
            None, norm, " ".join(prev.lower().split())
        ).ratio() > threshold:
            return True
    return False


def _transcript_tail_seconds(accumulated: list[dict], seconds: int) -> str:
    """Format the tail of the transcript covering the last `seconds` of audio."""
    if not accumulated:
        return ""
    cutoff = accumulated[-1]["end"] - seconds
    tail = [s for s in accumulated if s["end"] >= cutoff]
    return format_transcript(tail)


def extract_audio(input_path: str) -> str:
    ext = Path(input_path).suffix.lower()
    if ext == '.wav':
        return input_path
    out = str(Path(input_path).with_suffix('.wav'))
    print(f"Extracting audio to {out}...")
    subprocess.run(
        ['ffmpeg', '-i', input_path, '-ar', '16000', '-ac', '1', '-y', out],
        check=True, capture_output=True
    )
    return out


def transcribe(audio_path: str) -> list[dict]:
    from faster_whisper import WhisperModel

    print("Loading Whisper (medium)...")
    model = WhisperModel("medium", device="cpu", compute_type="int8")

    print("Transcribing...")
    segments, _ = model.transcribe(audio_path, beam_size=5)

    return [
        {
            "start":   round(seg.start, 1),
            "end":     round(seg.end, 1),
            "speaker": None,
            "text":    seg.text.strip(),
        }
        for seg in segments
    ]


def format_transcript(transcript: list[dict]) -> str:
    lines = []
    current_speaker = None
    buffer = []

    for seg in transcript:
        speaker = seg.get("speaker") or f"[{seg['start']:.0f}s]"
        label = f"[{speaker}]" if not speaker.startswith("[") else speaker

        if speaker != current_speaker:
            if buffer:
                prefix = f"[{current_speaker}]" if not current_speaker.startswith("[") else current_speaker
                lines.append(f"{prefix}: {' '.join(buffer)}")
            current_speaker = speaker
            buffer = [seg["text"]]
        else:
            buffer.append(seg["text"])

    if buffer:
        prefix = f"[{current_speaker}]" if not current_speaker.startswith("[") else current_speaker
        lines.append(f"{prefix}: {' '.join(buffer)}")

    return "\n".join(lines)


def slice_transcript(transcript: list[dict], portion: float) -> list[dict]:
    if not transcript:
        return transcript
    max_time = max(s["end"] for s in transcript)
    cutoff = max_time * portion
    return [s for s in transcript if s["start"] < cutoff]


def analyze(transcript_text: str, players: list, role: str, partial: bool = False,
            game: str = "secret_hitler", topic: str = "",
            mode: str = "", script: str = "",
            two_pass: bool = False, priors: dict | None = None,
            state_path: str = "output/botc_state.json") -> str:
    import importlib
    import shutil

    mod = importlib.import_module(f"games.{game}")

    # Two-pass path: any game whose module exposes get_extraction_prompt + get_reasoning_prompt.
    if two_pass and hasattr(mod, "get_extraction_prompt") and hasattr(mod, "get_reasoning_prompt"):
        # Build the system prompt with whatever signature this game uses.
        if game == "debate":
            system = mod.get_system_prompt(players, topic)
        elif game == "blood_on_the_clocktower":
            system = mod.get_system_prompt(players, mode, role, script)
        else:
            system = mod.get_system_prompt(players, role)
        return _analyze_two_pass(mod, system, transcript_text, priors, state_path)

    if game == "debate":
        system = mod.get_system_prompt(players, topic)
        analysis_prompt = mod.get_analysis_prompt(partial=partial)
    elif game == "blood_on_the_clocktower":
        system = mod.get_system_prompt(players, mode, role, script)
        analysis_prompt = mod.get_analysis_prompt(mode, role, partial=partial)
    else:
        system = mod.get_system_prompt(players, role)
        analysis_prompt = mod.get_analysis_prompt(role, partial=partial)

    prompt = f"{system}\n\nTRANSCRIPT:\n{transcript_text}\n\n{analysis_prompt}"

    claude_bin = shutil.which("claude") or "claude"
    print("Sending to Claude for analysis...")
    result = subprocess.run(
        [claude_bin, "--print", "--model", "claude-sonnet-4-6"],
        input=prompt, capture_output=True, text=True, check=True,
    )
    return result.stdout


_anthropic_client = None
_anthropic_token_loaded_at = 0.0


def _get_anthropic_client():
    """Lazy-load an Anthropic SDK client using the OAuth token from the
    `claude` CLI's credentials file (no API key required). Returns None if
    the token can't be read, in which case callers fall back to the
    `claude --print` subprocess.

    The OAuth token is cached for 30 minutes; if the credentials file is
    rotated underneath us we'll pick up the new token on the next refresh.
    Direct SDK calls are ~6x faster than the subprocess (1.4s vs 9s for a
    trivial Haiku call) because they skip Claude Code's tool-loading and
    process-startup overhead."""
    global _anthropic_client, _anthropic_token_loaded_at
    import json as _json
    import time as _time
    if _anthropic_client is not None and (_time.time() - _anthropic_token_loaded_at) < 1800:
        return _anthropic_client
    try:
        from anthropic import Anthropic
        creds_path = os.path.expanduser("~/.claude/.credentials.json")
        with open(creds_path) as f:
            creds = _json.load(f)
        token = (creds.get("claudeAiOauth") or {}).get("accessToken")
        if not token:
            return None
        _anthropic_client = Anthropic(
            auth_token=token,
            default_headers={"anthropic-beta": "oauth-2025-04-20"},
        )
        _anthropic_token_loaded_at = _time.time()
        return _anthropic_client
    except Exception as e:
        print(f"\n[anthropic] OAuth token load failed: {e}; falling back to subprocess.")
        _anthropic_client = None
        return None


def _run_claude(prompt: str, model: str = "claude-sonnet-4-6",
                system: str = "", max_tokens: int = 4096) -> str:
    """Run a Claude API call. Prefers direct Anthropic SDK with OAuth token
    (fast); falls back to `claude --print` subprocess if SDK setup fails.

    `system` is passed separately when given, with cache_control breakpoint
    set so identical system prompts on subsequent calls hit the prompt cache
    (further latency reduction on every fast tick after the first)."""
    client = _get_anthropic_client()
    if client is not None:
        try:
            kwargs = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                kwargs["system"] = [
                    {
                        "type": "text",
                        "text": "You are Claude Code, Anthropic's official CLI for Claude.",
                    },
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    },
                ]
            else:
                kwargs["system"] = "You are Claude Code, Anthropic's official CLI for Claude."
            resp = client.messages.create(**kwargs)
            # Extract text from the first content block
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    return block.text
            return ""
        except Exception as e:
            print(f"\n[anthropic SDK] call failed ({type(e).__name__}: {e}); falling back to subprocess.")
            # fall through to subprocess
    import shutil
    claude_bin = shutil.which("claude") or "claude"
    full = f"{system}\n\n{prompt}" if system else prompt
    result = subprocess.run(
        [claude_bin, "--print", "--model", model],
        input=full, capture_output=True, text=True, check=True,
    )
    return result.stdout


def _analyze_two_pass(mod, system: str, transcript_text: str,
                      priors: dict | None, state_path: str) -> str:
    """Game-agnostic two-pass analysis. Each game module supplies:
       - EMPTY_STATE: the starting-state dict
       - get_extraction_prompt(prior_state, priors): pass-1 instructions
       - get_reasoning_prompt(state, priors): pass-2 instructions
       - update_state_from_extraction(prior, text): JSON parser + fallback

       Pass 1 extracts/updates structured state (carry forward, resolve predictions).
       Pass 2 renders a human-readable report from that state with new WATCH FOR
       predictions feeding the next cycle's pass 1.
    """
    # Load prior state (or start fresh)
    prior_state = dict(getattr(mod, "EMPTY_STATE", {}))
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                prior_state = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[two-pass] could not read {state_path}: {e}; starting fresh")

    # Pass 1: extract structured state
    print("Pass 1/2: extracting structured state...")
    extraction_prompt = mod.get_extraction_prompt(prior_state, priors)
    pass1_user = f"TRANSCRIPT:\n{transcript_text}\n\n{extraction_prompt}"
    pass1_out = _run_claude(pass1_user, system=system)

    new_state = mod.update_state_from_extraction(prior_state, pass1_out)

    # Persist new state (atomic-ish via temp file)
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(new_state, f, indent=2)
    os.replace(tmp, state_path)
    versioned = state_path.replace(
        ".json", f"_{new_state.get('analysis_count', 0):02d}.json"
    )
    with open(versioned, "w", encoding="utf-8") as f:
        json.dump(new_state, f, indent=2)
    print(f"[two-pass] state saved: {state_path} (+ {versioned})")

    if new_state.get("_extraction_error"):
        print(f"[two-pass] WARNING: pass-1 extraction error: {new_state['_extraction_error']}")
        # Fall through and let pass 2 still run on whatever we have

    # Pass 2: render report from state
    print("Pass 2/2: rendering report from state...")
    reasoning_prompt = mod.get_reasoning_prompt(new_state, priors)
    return _run_claude(reasoning_prompt, system=system)


def _two_cadence_loop(game: str, accumulated: list, lock: threading.Lock,
                      push_fn, silence_state: dict,
                      players: list, role: str, topic: str, mode: str,
                      push_jid: str | None, state_path: str,
                      fast_interval: int, slow_interval: int,
                      fast_window: int, slow_window: int,
                      silence_trigger: bool, priors: dict | None,
                      source_lang: str = "", target_lang: str = "en") -> None:
    """Game-agnostic two-cadence loop. Used by --game conversation,
       --game mastermind, and --game translation. All modules expose the
       same interface: get_system_prompt, get_fast_prompt, bridge_from_state,
       plus the two-pass extraction/reasoning pair.

       Slow pass writes state + HTML + (optional) Telegram.
       Fast pass goes to phone TTS only — by design, slow synthesis is too
       long to read aloud during a live event."""
    import importlib
    import time as _time

    mod = importlib.import_module(f"games.{game}")
    default_mode = {
        "conversation": "general",
        "mastermind":   "broadcast",
        "translation":  "listening",
    }.get(game, "general")
    try:
        system = mod.get_system_prompt(players, mode=mode or default_mode,
                                       role=role, topic=topic,
                                       source_lang=source_lang,
                                       target_lang=target_lang)
    except TypeError:
        # Other games' get_system_prompt has no language kwargs; fall back.
        system = mod.get_system_prompt(players, mode=mode or default_mode,
                                       role=role, topic=topic)

    recent_fast: list[str] = []
    last_fast_at = [0.0]   # mutable for closure
    last_translated_offset = [0.0]  # translation: max(end) of segments already
                                     # fed to Haiku — only NEW segments past this
                                     # offset go in the next window. Replaces the
                                     # sliding fast_window for translation only;
                                     # other games keep the sliding window because
                                     # they re-evaluate state continuously.
    fast_count = [0]
    slow_count = [0]
    stop = threading.Event()
    # User corrections injected via `c <text>` — folded into priors for slow
    # pass and surfaced in bridge for fast pass. Authoritative: override
    # anything in cached state if conflicting.
    corrections: list[str] = []
    force_slow = threading.Event()

    fast_log_path = f"output/{game}_fast.log"
    slow_path_prefix = f"output/{game}_slow"

    def _read_bridge() -> dict:
        bridge = {}
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    bridge = mod.bridge_from_state(json.load(f))
            except (OSError, json.JSONDecodeError):
                bridge = {}
        if corrections:
            bridge["user_corrections"] = list(corrections)
        # Recent fast outputs feed back to the next fast tick so the model
        # can see what it has already produced and avoid re-translating /
        # re-stating overlapping window content. Game modules that don't
        # care about this (conversation, mastermind) simply ignore the key.
        if recent_fast:
            bridge["recent_fast"] = list(recent_fast)
        return bridge

    def fast_tick():
        chunk_event = silence_state.get("chunk_event")
        while not stop.is_set():
            # Prefer event-based wakeup (a chunk just transcribed) over
            # polling. Fall back to fast_interval timeout as a backstop so
            # the loop still fires during long silences if anything is in
            # the buffer.
            triggered_by_chunk = False
            if chunk_event is not None:
                triggered_by_chunk = chunk_event.wait(timeout=fast_interval)
                chunk_event.clear()
            else:
                _time.sleep(0.5)
            if stop.is_set():
                return
            now = _time.time()
            should_fire = triggered_by_chunk or (now - last_fast_at[0]) >= fast_interval
            if silence_trigger and silence_state.get("silent_tail"):
                should_fire = True
                silence_state["silent_tail"] = False
            if not should_fire:
                continue
            with lock:
                if game == "translation":
                    # Time-offset dedup: only feed Haiku segments past the
                    # last translated offset. Eliminates window-overlap as
                    # a source of paraphrase repetition at the structural
                    # level rather than relying on prompt-side instructions.
                    relevant_segs = [s for s in accumulated
                                     if s["start"] >= last_translated_offset[0]]
                    window_text = format_transcript(relevant_segs) if relevant_segs else ""
                    next_offset = relevant_segs[-1]["end"] if relevant_segs else None
                else:
                    window_text = _transcript_tail_seconds(accumulated, fast_window)
                    next_offset = None
            if not window_text.strip():
                continue
            bridge = _read_bridge()
            try:
                fast_prompt = mod.get_fast_prompt(window_text, bridge, role,
                                                  mode=mode or default_mode)
            except TypeError:
                # conversation.py's get_fast_prompt has no mode kwarg; fall back
                fast_prompt = mod.get_fast_prompt(window_text, bridge, role)
            last_fast_at[0] = now
            try:
                # Pass system+prompt split so the SDK path can cache the
                # system block (constant across most fast ticks).
                out = _run_claude(fast_prompt, model="claude-haiku-4-5",
                                  system=system, max_tokens=1024).strip()
            except subprocess.CalledProcessError as e:
                print(f"\n[fast] claude failed: {e}")
                _time.sleep(2)
                continue
            # Advance translation offset regardless of output. The segments
            # we just sent to Haiku are processed — whether or not the
            # output was useful, we don't want to feed them again. (If we
            # only advanced on non-SKIP, a confused Haiku could keep
            # SKIPping the same content forever.)
            if next_offset is not None:
                last_translated_offset[0] = next_offset
            # Robust SKIP detection: Haiku occasionally ignores the "output
            # exactly: SKIP" rule and prefaces the SKIP token with reasoning
            # prose. Without this guard, that prose gets pushed straight to
            # TTS and read aloud — a UX disaster. Treat ANY output that
            # contains `SKIP` as a standalone line as a skip, dropping the
            # prose alongside.
            out_lines = [ln.strip() for ln in out.splitlines()]
            is_skip = (not out) or (out == "SKIP") or ("SKIP" in out_lines)
            if is_skip:
                print(f"\n[fast] SKIP ({len(window_text.splitlines())} lines in window)")
                continue
            if _is_redundant(out, recent_fast):
                print(f"\n[fast] redundant, dropped: {out[:80]}...")
                continue
            recent_fast.append(out)
            del recent_fast[:-8]
            fast_count[0] += 1
            push_fn(strip_markdown_for_tts(out))
            with open(fast_log_path, "a", encoding="utf-8") as f:
                f.write(f"[{int(now)}] {out}\n\n")
            print(f"\n[fast #{fast_count[0]}]\n{out}\n")

    def slow_tick():
        while not stop.is_set():
            # Wait up to slow_interval, but fire early if a correction lands
            triggered_early = force_slow.wait(timeout=slow_interval)
            if stop.is_set():
                return
            force_slow.clear()
            with lock:
                window_text = _transcript_tail_seconds(accumulated, slow_window)
            if not window_text.strip():
                continue
            slow_priors = dict(priors) if priors else {}
            if corrections:
                slow_priors["user_corrections"] = list(corrections)
            if triggered_early:
                print("[slow] firing early due to user correction")
            try:
                analysis = _analyze_two_pass(mod, system, window_text,
                                             slow_priors or None, state_path)
            except subprocess.CalledProcessError as e:
                print(f"\n[slow] claude failed: {e}")
                continue
            slow_count[0] += 1
            out_path = f"{slow_path_prefix}_{slow_count[0]:02d}.txt"
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(analysis)
            try:
                from report import generate_html
                meta = f"{game} slow pass #{slow_count[0]}"
                html = generate_html(analysis, meta=meta)
                with open(out_path.replace(".txt", ".html"), "w",
                          encoding="utf-8") as f:
                    f.write(html)
            except Exception as e:
                print(f"\n[slow] html render failed: {e}")
            if push_jid:
                # ipc.format_telegram_message is SH-specific; advice-only push for now
                advice = extract_advice(analysis)
                if advice:
                    try:
                        from ipc import push as _ipc_push
                        _ipc_push(advice, jid=push_jid,
                                  analysis_num=slow_count[0])
                    except Exception as e:
                        print(f"\n[slow] telegram push failed: {e}")
            print(f"\n[slow #{slow_count[0]}] saved: {out_path}\n")

    print(f"\n{game} mode | mode={mode or default_mode} | role={role or '-'}")
    print(f"  fast: Haiku every {fast_interval}s on last {fast_window}s window"
          + (" + silence-gap" if silence_trigger else ""))
    print(f"  slow: Sonnet every {slow_interval}s on last {slow_window}s window")
    print(f"  fast log: {fast_log_path}\n")
    print("Enter = quit (or q+Enter)\n")

    threading.Thread(target=fast_tick, daemon=True).start()
    threading.Thread(target=slow_tick, daemon=True).start()
    while True:
        try:
            raw = input("").strip()
        except (EOFError, KeyboardInterrupt):
            break
        cmd = raw.lower()
        if cmd in ("q", "quit"):
            break
        if cmd.startswith("c "):
            text = raw[2:].strip()  # preserve case for proper nouns
            if text:
                corrections.append(text)
                force_slow.set()
                print(f"[correction added] {text}")
                print("[forcing slow pass — re-extracting with correction as authoritative]")
            continue
        if cmd == "c":
            if not corrections:
                print("no corrections set. usage: c <correction text>")
            else:
                print("active corrections:")
                for i, c in enumerate(corrections, 1):
                    print(f"  {i}. {c}")
            continue
        if cmd == "s":
            # Snapshot the current accumulated transcript to disk for ad-hoc reads
            with lock:
                snap = list(accumulated)
            from pathlib import Path
            Path("output").mkdir(exist_ok=True)
            with open("output/live_transcript.json", "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=2)
            with open("output/live_transcript.txt", "w", encoding="utf-8") as f:
                f.write(format_transcript(snap))
            print(f"snapshot: {len(snap)} segments")
            continue
        if cmd == "f":
            # Force a fast pass on the current window, ignoring interval throttle
            last_fast_at[0] = 0
            print("[force] next fast tick will fire immediately")
            continue
        # Empty line or unknown: print help, do not quit
        print("commands: s=snapshot transcript | f=force fast pass | "
              "c <text>=add live correction | c=list corrections | q=quit")
    print("Stopping conversation loop...")
    stop.set()


def live_mode(players: list, role: str, chunk_secs: float = 60, portion: float = 1.0,
             game: str = "secret_hitler", topic: str = "",
             mode: str = "", script: str = "",
             two_pass: bool = False, priors: dict | None = None,
             state_path: str = "output/state.json"):
    """Capture live audio in chunks, transcribe in background, analyse on keypress."""
    import queue
    import threading
    import numpy as np
    import sounddevice as sd
    from faster_whisper import WhisperModel

    SAMPLE_RATE = 16000

    # Check for input device before loading model
    try:
        default_input = sd.query_devices(kind='input')
    except sd.PortAudioError as exc:
        print(f"ERROR: No audio input device found — {exc}")
        print("On WSL: ensure a microphone is passed through or run on the host.")
        return

    print(f"Using input: {default_input['name']}")
    print(f"Chunk size: {chunk_secs}s | Role: {role}")
    if players:
        print(f"Players: {', '.join(players)}")
    print("─" * 50)

    print("Loading Whisper (medium)...")
    model = WhisperModel("medium", device="cpu", compute_type="int8")

    accumulated: list[dict] = []
    audio_q: queue.Queue = queue.Queue()
    lock = threading.Lock()
    stop_event = threading.Event()
    time_offset = 0.0

    def capture_loop():
        nonlocal time_offset
        print(f"Recording... (Enter = analyse, q+Enter = quit)\n")
        while not stop_event.is_set():
            audio_data = sd.rec(
                int(chunk_secs * SAMPLE_RATE),
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype='float32',
            )
            sd.wait()
            if stop_event.is_set():
                break
            audio_q.put((audio_data.flatten(), time_offset))
            time_offset += chunk_secs

    def transcribe_loop():
        while True:
            item = audio_q.get()
            if item is None:
                break
            chunk, offset = item
            segments, _ = model.transcribe(chunk, beam_size=5)
            with lock:
                for seg in segments:
                    accumulated.append({
                        "start":   round(offset + seg.start, 1),
                        "end":     round(offset + seg.end, 1),
                        "speaker": None,
                        "text":    seg.text.strip(),
                    })
            total_secs = int(offset + chunk_secs)
            segs = len(accumulated)
            print(f"\r[{total_secs}s captured | {segs} segments]   ", end="", flush=True)
            audio_q.task_done()

    cap_thread = threading.Thread(target=capture_loop, daemon=True)
    txn_thread = threading.Thread(target=transcribe_loop, daemon=True)
    cap_thread.start()
    txn_thread.start()

    os.makedirs("output", exist_ok=True)
    analysis_count = 0

    while True:
        try:
            cmd = input("").strip().lower()
        except (EOFError, KeyboardInterrupt):
            cmd = "q"

        if cmd == "q":
            print("\nStopping...")
            stop_event.set()
            audio_q.put(None)
            break

        with lock:
            snap = list(accumulated)

        if not snap:
            print("Nothing recorded yet — wait for the first chunk to finish.")
            continue

        if portion < 1.0:
            snap = slice_transcript(snap, portion)
            pct = int(portion * 100)
            print(f"Analysing first {pct}% ({len(snap)} segments)...")
        else:
            print(f"Analysing {len(snap)} segments...")

        text = format_transcript(snap)

        with open("output/live_transcript.json", "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2)
        with open("output/live_transcript.txt", "w", encoding="utf-8") as f:
            f.write(text)

        # Live analysis is always "partial" (game in progress)
        analysis = analyze(text, players, role, partial=True, game=game, topic=topic,
                           mode=mode, script=script,
                           two_pass=two_pass, priors=priors, state_path=state_path)
        analysis_count += 1
        out_path = f"output/live_analysis_{analysis_count:02d}.txt"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(analysis)

        print("\n" + "=" * 60)
        print(analysis)
        print("=" * 60)
        print(f"Saved to {out_path}\n")


PLAYERS_FILE = "config/players.json"


def load_known_players() -> list[str]:
    try:
        with open(PLAYERS_FILE, encoding="utf-8") as f:
            return json.load(f).get("players", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_known_players(players: list[str]) -> None:
    os.makedirs("config", exist_ok=True)
    with open(PLAYERS_FILE, "w", encoding="utf-8") as f:
        json.dump({"players": players}, f, indent=2)


def resolve_players(players: list[str]) -> list[str]:
    """If no players given, offer the saved roster. Save new roster after confirmation."""
    if players:
        save_known_players(players)
        return players

    saved = load_known_players()
    if saved:
        print(f"Last session players: {', '.join(saved)}")
        ans = input("Use these? [Y/n/edit]: ").strip().lower()
        if ans in ("", "y"):
            return saved
        elif ans == "edit":
            raw = input("Enter players (comma-separated): ").strip()
            players = [p.strip() for p in raw.split(",") if p.strip()]
            save_known_players(players)
            return players

    raw = input("Enter players (comma-separated, or leave blank): ").strip()
    players = [p.strip() for p in raw.split(",") if p.strip()]
    if players:
        save_known_players(players)
    return players


# ---------------------------------------------------------------------------
# Phone browser streaming mode
# ---------------------------------------------------------------------------

PHONE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SilentPartner</title>
<style>
  body { background: #0e0a0a; color: #d8cccc; font-family: sans-serif;
         display: flex; flex-direction: column; align-items: center;
         justify-content: center; height: 100vh; gap: 1.2rem; }
  h1   { font-size: 1.2rem; color: #cc2222; letter-spacing: .1em; text-transform: uppercase; }
  #btn { padding: .8rem 2rem; font-size: 1rem; background: #cc2222; color: #fff;
         border: none; border-radius: 6px; cursor: pointer; }
  #btn:disabled { background: #555; }
  #voice { padding: .5rem 1.2rem; font-size: .85rem; background: #2a1a1a; color: #d8cccc;
           border: 1px solid #3a1a1a; border-radius: 6px; cursor: pointer; }
  #voice.on { background: #228844; color: #fff; border-color: #228844; }
  #status { font-size: .85rem; color: #8a7070; text-align: center; }
  #wakeNote { font-size: .7rem; color: #555; }
</style>
</head>
<body>
<h1>SilentPartner</h1>
<button id="btn">Start recording</button>
<button id="voice">🔇 voice off</button>
<p id="status">Tap to begin</p>
<script>
const WS_URL_BASE = 'WSURL_PLACEHOLDER';
const CHUNK_MS = CHUNK_MS_PLACEHOLDER;
// Token comes from the page URL (?t=...) so the WS handshake includes it too.
const PAGE_TOKEN = new URLSearchParams(location.search).get('t') || '';
const WS_URL = WS_URL_BASE + (PAGE_TOKEN ? '?t=' + encodeURIComponent(PAGE_TOKEN) : '');

let ws, stream, active = false, chunkN = 0, wakeLock = null;
let voiceOn = false;
let pending = [];                  // buffered chunks (ArrayBuffer) while WS is down
let recordingLoopRunning = false;  // recordChunk self-perpetuating flag
let reconnectTimeout = null;
let reconnectDelay = 1000;         // ms; doubles on each fail, capped at 10s
const PENDING_CAP = 100;           // ~100min @ 60s/chunk; oldest dropped past this

// --- Voice -----------------------------------------------------------------
// Two paths depending on what the server pushes:
//   - String frame: phone-side speechSynthesis (needs phone TTS voice installed)
//   - Binary frame (ArrayBuffer): server-side TTS audio (WAV) — phone just plays it
// Toggling voice on primes BOTH paths for iOS gesture-lock unlock.
let audioQueue = [];   // queued Blob URLs awaiting playback
let audioEl = null;    // currently-playing HTMLAudioElement (single sequential player)

function setVoice(on) {
  voiceOn = on;
  const el = document.getElementById('voice');
  el.textContent = on ? '🔊 voice on' : '🔇 voice off';
  el.classList.toggle('on', on);
  if (!on) {
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    if (audioEl) { try { audioEl.pause(); } catch {} audioEl = null; }
    audioQueue.forEach(u => { try { URL.revokeObjectURL(u); } catch {} });
    audioQueue = [];
  }
}

document.getElementById('voice').addEventListener('click', () => {
  if (!voiceOn) {
    // Prime speechSynthesis for the text-frame path (iOS gesture unlock).
    if ('speechSynthesis' in window) {
      try {
        const prime = new SpeechSynthesisUtterance(' ');
        prime.volume = 0;
        window.speechSynthesis.speak(prime);
      } catch {}
    }
    // Prime HTMLAudioElement playback for the binary-frame path. iOS blocks
    // programmatic audio.play() until at least one play() has fired from a
    // user gesture; this 1-byte silent WAV satisfies that requirement.
    try {
      const a = new Audio('data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=');
      a.play().catch(() => {});
    } catch {}
    setVoice(true);
  } else {
    setVoice(false);
  }
});

function speak(text) {
  if (!voiceOn || !('speechSynthesis' in window) || !text) return;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 1.05;
  u.pitch = 1.0;
  window.speechSynthesis.speak(u);
}

function playNextAudio() {
  if (audioEl || audioQueue.length === 0) return;
  const url = audioQueue.shift();
  audioEl = new Audio(url);
  const cleanup = () => {
    try { URL.revokeObjectURL(url); } catch {}
    audioEl = null;
    playNextAudio();
  };
  audioEl.onended = cleanup;
  audioEl.onerror = cleanup;
  audioEl.play().catch(err => {
    setStatus('Audio play failed: ' + (err.message || err));
    cleanup();
  });
}

function playAudioBuf(buf) {
  if (!voiceOn) return;
  const blob = new Blob([buf], { type: 'audio/wav' });
  const url = URL.createObjectURL(blob);
  audioQueue.push(url);
  playNextAudio();
}

async function acquireWakeLock() {
  if (!('wakeLock' in navigator)) return;
  try {
    wakeLock = await navigator.wakeLock.request('screen');
    wakeLock.addEventListener('release', () => { wakeLock = null; });
  } catch { /* permission denied / not supported — fall through */ }
}

async function releaseWakeLock() {
  try { if (wakeLock) await wakeLock.release(); } catch {}
  wakeLock = null;
}

// Re-acquire on visibility change (browsers drop the lock when tab hides)
document.addEventListener('visibilitychange', () => {
  if (active && document.visibilityState === 'visible' && !wakeLock) {
    acquireWakeLock();
  }
});

document.getElementById('btn').addEventListener('click', () => {
  if (!active) startRecording(); else stopRecording();
});

async function startRecording() {
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    setStatus('Mic access denied: ' + e.message);
    return;
  }
  await acquireWakeLock();
  active = true;
  pending = [];
  recordingLoopRunning = false;
  reconnectDelay = 1000;
  btn('Stop');
  connectWS();
}

function stopRecording() {
  active = false;
  if (reconnectTimeout) { clearTimeout(reconnectTimeout); reconnectTimeout = null; }
  if (stream) stream.getTracks().forEach(t => t.stop());
  if (ws) ws.close();
  releaseWakeLock();
  btn('Start recording');
  setStatus(`Stopped${pending.length ? ' (' + pending.length + ' unsent chunks dropped)' : ''}`);
  pending = [];
}

function flushPending() {
  let flushed = 0;
  while (pending.length > 0 && ws && ws.readyState === WebSocket.OPEN) {
    const buf = pending.shift();
    try { ws.send(buf); chunkN++; flushed++; }
    catch { pending.unshift(buf); break; }
  }
  return flushed;
}

function connectWS() {
  ws = new WebSocket(WS_URL);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    reconnectDelay = 1000;
    const flushed = flushPending();
    setStatus(flushed
      ? `Connected — flushed ${flushed} buffered chunk(s)`
      : 'Connected — recording...');
    if (active && !recordingLoopRunning) {
      recordingLoopRunning = true;
      recordChunk();
    }
  };
  ws.onclose = () => {
    if (active) {
      const secs = reconnectDelay / 1000;
      setStatus(`Disconnected — reconnecting in ${secs}s (${pending.length} buffered)`);
      reconnectTimeout = setTimeout(connectWS, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 10000);
    } else {
      setStatus('Disconnected');
      btn('Start recording');
      releaseWakeLock();
    }
  };
  ws.onerror = () => setStatus('WebSocket error — will retry');
  // Server pushes either a text frame (browser TTS path) or a binary frame
  // containing WAV audio bytes (server-side TTS path).
  ws.onmessage = (e) => {
    if (typeof e.data === 'string') {
      speak(e.data);
    } else if (e.data instanceof ArrayBuffer) {
      playAudioBuf(e.data);
    } else if (e.data && typeof e.data.arrayBuffer === 'function') {
      // Some browsers deliver Blob even with binaryType='arraybuffer' under
      // certain conditions; convert defensively.
      e.data.arrayBuffer().then(playAudioBuf);
    }
  };
}

function recordChunk() {
  if (!active) { recordingLoopRunning = false; return; }
  const chunks = [];
  const rec = new MediaRecorder(stream);
  rec.ondataavailable = e => { if (e.data.size > 0) chunks.push(e.data); };
  rec.onstop = () => {
    const blob = new Blob(chunks, { type: chunks[0]?.type || 'audio/webm' });
    blob.arrayBuffer().then(buf => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(buf);
        chunkN++;
        setStatus(`Connected — chunk ${chunkN} sent (${Math.round(blob.size/1024)}KB)`);
      } else {
        pending.push(buf);
        if (pending.length > PENDING_CAP) pending.shift();
        setStatus(`Disconnected — buffered ${pending.length} chunk(s)`);
      }
    });
    if (active) recordChunk();
    else recordingLoopRunning = false;
  };
  rec.start();
  setTimeout(() => rec.stop(), CHUNK_MS);
}

function btn(label) { document.getElementById('btn').textContent = label; }
function setStatus(msg) { document.getElementById('status').textContent = msg; }
</script>
</body>
</html>
"""


def _decode_webm_to_pcm(webm_bytes: bytes) -> "numpy.ndarray":
    import numpy as np
    result = subprocess.run(
        ['ffmpeg', '-i', 'pipe:0', '-f', 'f32le', '-ar', '16000', '-ac', '1', 'pipe:1'],
        input=webm_bytes, capture_output=True,
    )
    if not result.stdout:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(result.stdout, dtype=np.float32)


def phone_mode(players: list, role: str, chunk_secs: float = 60, portion: float = 1.0,
               push_jid: str | None = None, game: str = "secret_hitler", topic: str = "",
               mode: str = "", script: str = "",
               two_pass: bool = False, priors: dict | None = None,
               state_path: str = "output/state.json",
               fast_interval: int = 20, slow_interval: int = 180,
               fast_window: int = 60, slow_window: int = 240,
               silence_trigger: bool = False,
               source_lang: str = "", target_lang: str = "en",
               server_tts: str = "browser", piper_voice: str = "",
               piper_rate: float = 0.9,
               lang_lock: bool = False,
               vad_chunking: bool = False,
               vad_silence_ms: int = 600,
               vad_min_dur_s: float = 1.5):
    """Receive audio from phone browser over WebSocket, transcribe, analyse on keypress."""
    import asyncio
    import secrets as _secrets
    import shutil
    import socket
    import urllib.parse as _urlparse
    import numpy as np
    import websockets
    from websockets.http11 import Response
    from websockets.datastructures import Headers
    from faster_whisper import WhisperModel

    PORT = 8766
    # Auth token: required as ?t=<token> on every HTTP and WS request.
    # Caller (e.g. launcher) can pin one via SILENTPARTNER_AUTH_TOKEN; otherwise
    # we mint a fresh random one each session.
    auth_token = os.environ.get("SILENTPARTNER_AUTH_TOKEN") or _secrets.token_urlsafe(16)

    is_wsl = os.path.exists("/proc/version") and "microsoft" in open("/proc/version").read().lower()

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "localhost"

    # HTML uses wss:// when served over a cloudflared https tunnel; ws:// on LAN
    # We inject a placeholder and patch it at runtime once the tunnel URL is known.
    # VAD chunking: phone sends fast 250ms timeslices and the server cuts on
    # silence gaps. Fixed: phone sends one chunk per chunk_secs (legacy path).
    phone_chunk_ms = 250 if vad_chunking else int(chunk_secs * 1000)
    html_template = (PHONE_HTML
                     .replace("WSURL_PLACEHOLDER", "__WS_URL__")
                     .replace("CHUNK_MS_PLACEHOLDER", str(phone_chunk_ms)))

    # Start cloudflared tunnel if available — gives a public https URL
    cloudflared_bin = shutil.which("cloudflared") or "/tmp/cloudflared"
    tunnel_url: list[str] = []  # filled in by tunnel thread

    def start_tunnel():
        if not os.path.exists(cloudflared_bin):
            return
        import re, time as _time
        # cloudflared block-buffers stdout when piped, so route logs to a file
        # we can tail. Truncate first so we don't pick up a stale URL.
        log_path = "/tmp/cloudflared.log"
        try:
            open(log_path, "w").close()
        except OSError:
            pass
        # --protocol http2 forces TCP fallback. Default is QUIC (UDP 7844)
        # which is silently dropped by many network/firewall setups, leaving
        # cloudflared retrying forever with "control stream encountered a
        # failure while serving" in the log but no URL surfaced.
        subprocess.Popen(
            [cloudflared_bin, "tunnel", "--url", f"http://localhost:{PORT}",
             "--protocol", "http2", "--logfile", log_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = _time.time() + 30
        seen = ""
        while _time.time() < deadline:
            try:
                with open(log_path) as f:
                    seen = f.read()
            except OSError:
                seen = ""
            m = re.search(r"https://[a-z0-9\-]+\.trycloudflare\.com", seen)
            if m:
                tunnel_url.append(m.group(0))
                full_url = f"{tunnel_url[0]}/?t={auth_token}"
                sys.stdout.write(
                    f"\n── Tunnel ready ──────────────────────────────────────────\n"
                    f"  Open on your phone: {full_url}\n"
                    f"──────────────────────────────────────────────────────────\n\n"
                )
                sys.stdout.flush()
                return
            _time.sleep(0.5)
        sys.stdout.write("\n[tunnel] gave up waiting after 30s; check /tmp/cloudflared.log\n")
        sys.stdout.flush()

    tunnel_thread = threading.Thread(target=start_tunnel, daemon=True)
    tunnel_thread.start()

    print(f"\nLoading Whisper (medium)...")
    model = WhisperModel("medium", device="cpu", compute_type="int8")

    # Warmup: first transcribe call after load is markedly slower (JIT,
    # internal cache fill). Burn that cost now on a silent buffer so the
    # first real chunk lands at steady-state latency. Also pre-loads the
    # silero VAD model used by vad_filter=True below.
    try:
        import numpy as _np
        print("Warming up Whisper (dummy transcribe + VAD load)...")
        _warm_pcm = _np.zeros(16000, dtype=_np.float32)  # 1s of silence at 16kHz
        list(model.transcribe(_warm_pcm, beam_size=1, vad_filter=True)[0])
    except Exception as e:
        print(f"  warmup skipped: {e}")

    # Optional server-side TTS via Piper. When enabled, fast-pass push goes
    # out as a binary WAV frame (phone plays as Blob) instead of a text frame
    # for browser speechSynthesis. Eliminates the dependency on phone-side
    # TTS voices being installed.
    piper_voice_obj = None
    piper_syn_config = None
    if server_tts == "piper":
        try:
            from piper import PiperVoice, SynthesisConfig
            print(f"Loading Piper voice: {piper_voice}...")
            piper_voice_obj = PiperVoice.load(piper_voice)
            piper_syn_config = SynthesisConfig(length_scale=piper_rate)
            rate_pct = int(round((1.0 - piper_rate) * 100))
            rate_desc = f"{abs(rate_pct)}% {'faster' if rate_pct > 0 else 'slower' if rate_pct < 0 else 'normal'}"
            print(f"  Piper ready (rate={piper_rate}, {rate_desc}).")
        except Exception as e:
            print(f"  Piper load failed: {e}. Falling back to browser TTS.")
            server_tts = "browser"

    def synthesize_wav(text: str) -> bytes:
        """Render text → WAV bytes using the loaded Piper voice. Empty text
        returns empty bytes (caller should skip the push)."""
        import io as _io
        import wave as _wave
        if not piper_voice_obj or not text.strip():
            return b""
        buf = _io.BytesIO()
        wav = _wave.open(buf, "wb")
        try:
            piper_voice_obj.synthesize_wav(text, wav, syn_config=piper_syn_config)
        finally:
            wav.close()
        return buf.getvalue()

    accumulated: list[dict] = []
    lock = threading.Lock()
    time_offset = 0.0
    analysis_count = 0
    os.makedirs("output", exist_ok=True)

    # Active phone clients we can push text frames to (for browser TTS).
    clients: set = set()

    # Conversation-mode VAD signal: ws_handler sets silent_tail=True when the
    # latest chunk ends in silence; the conversation loop reads + clears it.
    # chunk_event fires when a chunk has been transcribed and appended — the
    # fast loop waits on this so translation processes each new chunk
    # immediately instead of waiting for fast_interval to elapse.
    silence_state: dict = {
        "silent_tail": False,
        "chunk_event": threading.Event(),
    }

    def make_html() -> bytes:
        # Use tunnel URL (wss) if available, else LAN ws
        if tunnel_url:
            ws_url = tunnel_url[0].replace("https://", "wss://")
        else:
            ws_url = f"ws://{local_ip}:{PORT}"
        return html_template.replace("__WS_URL__", ws_url).encode()

    def _check_token(path: str) -> bool:
        try:
            qs = _urlparse.urlparse(path).query
            return _urlparse.parse_qs(qs).get("t", [""])[0] == auth_token
        except (ValueError, AttributeError):
            return False

    def _forbidden() -> Response:
        body = b"forbidden\n"
        headers = Headers([
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ])
        return Response(403, "Forbidden", headers, body)

    async def process_request(connection, request):
        """Serve the capture page for plain HTTP GETs; let WebSocket upgrades through.
        Both paths require ?t=<token> matching the session auth token."""
        if not _check_token(request.path):
            return _forbidden()
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        body = make_html()
        headers = Headers([
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ])
        return Response(200, "OK", headers, body)

    # Track in-flight transcription state so the event loop stays free to
    # consume incoming chunks while whisper is busy on the previous one.
    transcribe_busy = {"flag": False, "pending": 0}

    # Language auto-detect lock: opt-in via --lang-lock. When enabled and
    # --source-lang is empty, whisper detects per chunk; once LANG_LOCK_N
    # consecutive chunks agree at >= LANG_LOCK_CONF, the language is pinned
    # for the rest of the session. Off by default because bilingual / code-
    # switching audio (language-learning material, mixed conversations) will
    # lock to whichever language happens to win the first streak and then
    # mistranscribe the other.
    LANG_LOCK_N = 3
    LANG_LOCK_CONF = 0.5
    lang_state = {"locked": source_lang or None, "recent": []}

    # Auto-archive of stale state when priors disagree with detected lang.
    # Translation state from a prior session (e.g. French) carries glossary,
    # topic, source_lang into the new session; if whisper consistently detects
    # a different language at high confidence, archive the file and update
    # priors so the slow pass picks up a clean slate on the next iteration.
    LANG_MISMATCH_N = 3
    LANG_MISMATCH_CONF = 0.85
    priors_lang_state = {
        "expected": (priors or {}).get("source_lang", "") or "",
        "recent": [],
    }

    def _run_transcribe(pcm_arr):
        """Synchronous whisper call. Runs on the executor thread pool so the
        WebSocket event loop is never blocked. Returns a list of segments
        (materialised — the original generator can't cross the executor
        boundary cleanly)."""
        forced = lang_state["locked"]
        segs, info = model.transcribe(
            pcm_arr, beam_size=5,
            language=forced,
            vad_filter=True,
        )
        seg_list = list(segs)
        if forced is None and info is not None:
            lang = getattr(info, "language", None)
            prob = getattr(info, "language_probability", 0.0) or 0.0
            print(f"\n[lang detect: {lang} {prob:.2f}]")

            # Auto-archive stale state when detected lang contradicts priors.
            expected = priors_lang_state["expected"]
            if (expected and lang and lang != expected
                    and prob >= LANG_MISMATCH_CONF):
                priors_lang_state["recent"].append(lang)
                priors_lang_state["recent"] = priors_lang_state["recent"][-LANG_MISMATCH_N:]
                if (len(priors_lang_state["recent"]) == LANG_MISMATCH_N
                        and len(set(priors_lang_state["recent"])) == 1):
                    if os.path.exists(state_path):
                        archived = state_path.replace(
                            ".json", f"_archived_{int(time.time())}.json")
                        try:
                            os.rename(state_path, archived)
                            print(f"\n[priors auto-reset: {expected} → {lang}; archived {archived}]")
                        except OSError as e:
                            print(f"\n[priors auto-reset: archive failed: {e}]")
                    if priors is not None:
                        priors["source_lang"] = lang
                        for k in ("topic_inferred",):
                            priors.pop(k, None)
                    priors_lang_state["expected"] = lang
                    priors_lang_state["recent"] = []
            elif expected and lang == expected:
                priors_lang_state["recent"] = []

            if lang_lock:
                if lang and prob >= LANG_LOCK_CONF:
                    lang_state["recent"].append(lang)
                    lang_state["recent"] = lang_state["recent"][-LANG_LOCK_N:]
                    if (len(lang_state["recent"]) == LANG_LOCK_N
                            and len(set(lang_state["recent"])) == 1):
                        lang_state["locked"] = lang
                        print(f"\n[lang locked: {lang} after {LANG_LOCK_N}× ≥{LANG_LOCK_CONF:.2f}]")
                else:
                    lang_state["recent"] = []
        return seg_list

    # VAD chunking state (per-connection, lazy-init).
    SAMPLE_RATE = 16000
    VAD_FRAME_MS = 30
    VAD_FRAME_BYTES = int(SAMPLE_RATE * VAD_FRAME_MS / 1000) * 2
    VAD_AGGRESSIVENESS = 2
    vad_max_samples = int(SAMPLE_RATE * chunk_secs)         # ceiling
    vad_min_samples = int(SAMPLE_RATE * vad_min_dur_s)      # floor before silence-cut
    vad_silence_threshold_ms = vad_silence_ms

    async def _emit_chunk(pcm_arr, duration_s: float):
        """Run a buffered PCM array through the existing transcribe pipeline."""
        nonlocal time_offset
        offset = time_offset
        time_offset += duration_s

        if game == "conversation" and silence_trigger:
            silence_state["silent_tail"] = _vad_tail_silent(pcm_arr)

        if transcribe_busy["flag"]:
            transcribe_busy["pending"] += 1
            print(f"\n[backlog] whisper busy, queued chunk #{transcribe_busy['pending']}")

        transcribe_busy["flag"] = True
        try:
            segments = await asyncio.get_event_loop().run_in_executor(
                None, _run_transcribe, pcm_arr
            )
        finally:
            transcribe_busy["flag"] = False
            if transcribe_busy["pending"]:
                transcribe_busy["pending"] -= 1

        appended = 0
        with lock:
            for seg in segments:
                accumulated.append({
                    "start":   round(offset + seg.start, 1),
                    "end":     round(offset + seg.end, 1),
                    "speaker": None,
                    "text":    seg.text.strip(),
                })
                appended += 1
        segs = len(accumulated)
        mins = int(time_offset) // 60
        print(f"\r[{mins}m captured | {segs} segments]   ", end="", flush=True)
        if appended:
            silence_state["chunk_event"].set()

    async def ws_handler(websocket):
        clients.add(websocket)

        # Per-connection VAD state. Lazy import; if webrtcvad is unavailable,
        # fall back transparently to fixed-chunk behaviour.
        vad = None
        if vad_chunking:
            try:
                import webrtcvad
                vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
            except ImportError:
                print("\n[vad] webrtcvad missing — falling back to fixed chunks")
        pcm_buffer = np.zeros(0, dtype=np.float32)
        silent_tail_ms = 0

        try:
            async for message in websocket:
                if not isinstance(message, bytes):
                    continue
                pcm = await asyncio.get_event_loop().run_in_executor(
                    None, _decode_webm_to_pcm, message
                )
                if pcm.size == 0:
                    continue

                if vad is None:
                    # Legacy fixed-chunk path: phone delivers one chunk_secs
                    # block per WS message; transcribe each as-is.
                    await _emit_chunk(pcm, chunk_secs)
                    continue

                # VAD path: classify the new piece, append to buffer, decide cut.
                pcm16 = (np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes()
                any_speech = False
                for i in range(0, len(pcm16) - VAD_FRAME_BYTES + 1, VAD_FRAME_BYTES):
                    if vad.is_speech(pcm16[i:i+VAD_FRAME_BYTES], SAMPLE_RATE):
                        any_speech = True
                        break
                piece_ms = int(pcm.size / SAMPLE_RATE * 1000)
                if any_speech:
                    silent_tail_ms = 0
                else:
                    silent_tail_ms += piece_ms

                # Trim leading silence so we don't ship empty preamble to whisper.
                if pcm_buffer.size == 0 and not any_speech:
                    continue
                pcm_buffer = np.concatenate([pcm_buffer, pcm])

                should_emit = False
                if pcm_buffer.size >= vad_max_samples:
                    should_emit = True  # ceiling — long monologue, force cut
                elif (pcm_buffer.size >= vad_min_samples
                        and silent_tail_ms >= vad_silence_threshold_ms):
                    should_emit = True  # natural pause

                if should_emit:
                    cut_pcm = pcm_buffer
                    pcm_buffer = np.zeros(0, dtype=np.float32)
                    silent_tail_ms = 0
                    await _emit_chunk(cut_pcm, cut_pcm.size / SAMPLE_RATE)
        finally:
            clients.discard(websocket)

    async def _broadcast(payload) -> None:
        """Send a frame (str or bytes) to every connected phone client."""
        dead = []
        for c in list(clients):
            try:
                await c.send(payload)
            except Exception:
                dead.append(c)
        for c in dead:
            clients.discard(c)

    def push_text_to_phones(text: str) -> None:
        """Push fast-pass output to phone clients. With server_tts=piper this
        synthesises a WAV server-side and pushes binary bytes; with
        server_tts=browser it pushes the raw text for phone speechSynthesis."""
        if not clients or not text or not text.strip():
            return
        if server_tts == "piper" and piper_voice_obj is not None:
            try:
                wav_bytes = synthesize_wav(text)
            except Exception as e:
                print(f"\n[push] piper synth failed, falling back to text: {e}")
                wav_bytes = b""
            payload = wav_bytes if wav_bytes else text
        else:
            payload = text
        try:
            asyncio.run_coroutine_threadsafe(_broadcast(payload), ws_loop)
        except Exception as e:
            print(f"\n[push] failed: {e}")

    async def ws_server():
        async with websockets.serve(ws_handler, "0.0.0.0", PORT,
                                    process_request=process_request,
                                    ping_interval=None):
            await asyncio.Future()

    ws_loop = asyncio.new_event_loop()
    ws_thread = threading.Thread(
        target=lambda: ws_loop.run_until_complete(ws_server()),
        daemon=True,
    )
    ws_thread.start()

    if is_wsl:
        print(f"\nWSL2 — waiting for cloudflared tunnel URL...")
        print(f"(LAN fallback: http://{local_ip}:{PORT} — may not reach phone)")
    else:
        print(f"\nOpen on your phone: http://{local_ip}:{PORT}")
    print(f"\nChunk size: {chunk_secs}s | Role: {role}")
    if players:
        print(f"Players: {', '.join(players)}")

    if game in ("conversation", "mastermind", "translation"):
        _two_cadence_loop(
            game, accumulated, lock, push_text_to_phones, silence_state,
            players, role, topic, mode, push_jid, state_path,
            fast_interval, slow_interval, fast_window, slow_window,
            silence_trigger, priors,
            source_lang=source_lang, target_lang=target_lang,
        )
        return

    print("\nEnter = analyse now | q+Enter = quit\n")

    while True:
        try:
            cmd = input("").strip().lower()
        except (EOFError, KeyboardInterrupt):
            cmd = "q"

        if cmd == "q":
            print("Stopping...")
            break

        with lock:
            snap = list(accumulated)

        # 's' = snapshot: flush transcript to disk and continue, no analysis.
        # Used by the launcher's pointer flow which runs its own focused prompt.
        if cmd == "s":
            text = format_transcript(snap)
            with open("output/live_transcript.json", "w") as f:
                json.dump(snap, f, indent=2)
            with open("output/live_transcript.txt", "w") as f:
                f.write(text)
            print(f"snapshot: {len(snap)} segments")
            continue

        if not snap:
            print("Nothing received yet — wait for the first chunk.")
            continue

        if portion < 1.0:
            snap = slice_transcript(snap, portion)
            pct = int(portion * 100)
            print(f"Analysing first {pct}% ({len(snap)} segments)...")
        else:
            print(f"Analysing {len(snap)} segments...")

        text = format_transcript(snap)
        with open("output/live_transcript.json", "w") as f:
            json.dump(snap, f, indent=2)
        with open("output/live_transcript.txt", "w") as f:
            f.write(text)

        analysis = analyze(text, players, role, partial=True, game=game, topic=topic,
                           mode=mode, script=script,
                           two_pass=two_pass, priors=priors, state_path=state_path)
        analysis_count += 1
        out_path = f"output/phone_analysis_{analysis_count:02d}.txt"
        with open(out_path, "w") as f:
            f.write(analysis)

        # HTML report
        from report import generate_html
        total_secs = int(time_offset)
        meta = f"Live capture — {total_secs // 60}m {total_secs % 60}s | Analysis #{analysis_count}"
        html_report = generate_html(analysis, meta=meta)
        html_path = f"output/phone_analysis_{analysis_count:02d}.html"
        with open(html_path, "w") as f:
            f.write(html_report)

        # Telegram push
        if push_jid:
            from ipc import push
            push(analysis, jid=push_jid, analysis_num=analysis_count, total_secs=total_secs)

        # Browser TTS push to any connected phone client. The page only speaks
        # if the user has tapped the voice toggle on (opt-in per voice rule).
        # Only the trailing advisory section is spoken — no point reading the
        # full grimoire / claim log / suspicion table aloud.
        advice = strip_markdown_for_tts(extract_advice(analysis))
        if advice:
            push_text_to_phones(advice)

        print("\n" + "=" * 60)
        print(analysis)
        print("=" * 60)
        print(f"Saved: {out_path} | {html_path}\n")


def main():
    parser = argparse.ArgumentParser(description="SilentPartner — game intelligence from conversation")
    parser.add_argument("--file",    default=None,  help="Path to audio or video file (skip to re-analyze saved transcript)")
    parser.add_argument("--game",    default="secret_hitler", help="Game type: secret_hitler / debate / blood_on_the_clocktower / conversation / mastermind / translation (default: secret_hitler)")
    parser.add_argument("--players", default=None,  help="Player/participant names, comma-separated e.g. Alice,Bob,Carol")
    parser.add_argument("--role",    default=None,  help="Your role: secret_hitler=liberal/fascist/hitler/spectator; botc=character or alignment (good/evil)")
    parser.add_argument("--topic",   default="",    help="Debate topic (debate mode only)")
    parser.add_argument("--mode",    default="",    help="Sub-mode (botc only): storyteller / player")
    parser.add_argument("--script",  default="",    help="Script (botc only): Trouble Brewing / Sects & Violets / Bad Moon Rising / custom")
    parser.add_argument("--portion", default=1.0, type=float, help="Fraction of transcript to analyse, e.g. 0.33 for first third")
    parser.add_argument("--live",     action="store_true", help="Live audio capture mode (microphone)")
    parser.add_argument("--phone",    action="store_true", help="Phone browser streaming mode")
    parser.add_argument("--chunk",    default=60, type=float, help="Chunk size in seconds for live/phone mode (default: 60)")
    parser.add_argument("--push",     action="store_true", help="Push analysis to Telegram after each run")
    parser.add_argument("--push-jid", default="tg4:-5117247882", help="Telegram chat JID to push to (default: Stella Support)")
    parser.add_argument("--two-pass", action="store_true", help="Two-pass analysis (all games): extract structured state then reason over it. Enables WATCH FOR predictions resolved by next cycle.")
    parser.add_argument("--priors",   default=None, help="Path to JSON file with pre-game priors (script, player count, role distribution, known roles). Used as ground truth when present.")
    parser.add_argument("--reset-state", action="store_true", help="Wipe any existing two-pass state file at session start (default: continue from existing)")
    parser.add_argument("--fast-interval", default=20, type=int, help="Conversation: Haiku cadence in seconds (default 20)")
    parser.add_argument("--slow-interval", default=180, type=int, help="Conversation: Sonnet cadence in seconds (default 180)")
    parser.add_argument("--fast-window", default=60, type=int, help="Conversation: Haiku transcript window in seconds (default 60)")
    parser.add_argument("--slow-window", default=240, type=int, help="Conversation: Sonnet transcript window in seconds (default 240)")
    parser.add_argument("--silence-trigger", action="store_true", help="Conversation: also fire fast pass on detected silence gaps (requires webrtcvad-wheels)")
    parser.add_argument("--source-lang", default="", help="Translation: source language ISO code or short name (e.g. fr, es, ja). Empty = whisper auto-detect")
    parser.add_argument("--target-lang", default="en", help="Translation: target language ISO code (default: en). Note: phone TTS uses default browser voice; non-en targets may sound wrong-locale until per-utterance lang is wired")
    parser.add_argument("--server-tts", default="piper", choices=["browser", "piper"], help="TTS engine. piper=server-side neural TTS, pushes WAV bytes — works on phones with no installed TTS voice (default). browser=phone speechSynthesis, requires a phone-side voice.")
    parser.add_argument("--piper-voice", default="voices/en_US-ryan-medium.onnx", help="Path to Piper ONNX voice model (only used when --server-tts piper)")
    parser.add_argument("--piper-rate", type=float, default=0.72, help="Piper speech rate as length_scale (lower = faster). 1.0 = normal, 0.85 = 15%% faster, 0.8 = 20%% faster, 0.77 = 23%% faster, 0.72 = 28%% faster. Default 0.72.")
    parser.add_argument("--lang-lock", action="store_true", help="Translation: after 3 chunks agree at >=0.5 confidence, pin source language for the rest of the session (skips per-chunk detection). Off by default — bilingual / code-switching audio breaks the lock.")
    parser.add_argument("--vad-chunking", action="store_true", help="Translation: enable VAD-based dynamic chunking. Phone streams 250ms slices; the server cuts at silence gaps (--vad-silence-ms) once min duration (--vad-min-dur) is reached; --chunk is the ceiling. Off by default — fixed-cadence chunks give whisper a larger consistent window, which is better on continuous speech (lectures, scripted dialogue). Turn on for conversational audio with variable pauses.")
    parser.add_argument("--vad-silence-ms", type=int, default=500, help="VAD chunking: trailing silence required to cut a chunk (default 500ms)")
    parser.add_argument("--vad-min-dur", type=float, default=2.0, help="VAD chunking: minimum chunk duration before silence-cut is allowed (default 2s)")
    args = parser.parse_args()

    game = args.game
    topic = args.topic
    mode = args.mode
    script = args.script

    # Load --priors early so the mastermind interactive prompt can extend it
    priors: dict | None = None
    if args.priors:
        with open(args.priors, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            priors = loaded
            print(f"[priors] loaded from {args.priors}: {list(priors.keys())}")
        else:
            print(f"[priors] WARNING: {args.priors} is not a JSON object; ignoring")

    if game == "secret_hitler":
        role = args.role or input("\nEnter your role (liberal / fascist / hitler / spectator): ").strip().lower()
    elif game == "blood_on_the_clocktower":
        if not mode:
            mode = input("\nMode (storyteller / player) [player]: ").strip().lower() or "player"
        if mode == "player":
            role = args.role or input("Your character or alignment (e.g. Empath, Imp, good, evil): ").strip()
        else:
            role = args.role or ""
    elif game == "conversation":
        if not mode:
            mode = "general"
        role = args.role or ""
        # Phone sends shorter chunks → fast-path can react sooner
        if args.chunk == parser.get_default("chunk"):
            args.chunk = 15
        # Interactive priming for conversation: who you're talking to and your side.
        # Skip if --topic and --role already set on CLI.
        if not topic or not role:
            print("\nConversation context — quick setup (Enter to skip any line)")
            print("  mode: negotiation / interview / debate / general")
            try:
                if not topic:
                    topic = input(f"  topic / what's this about? > ").strip() or topic
                m_in = input(f"  mode [{mode}] > ").strip().lower()
                if m_in:
                    mode = m_in
                if not role:
                    role = input("  your side / role (e.g. 'buyer', 'pro-X', "
                                 "'interviewer') > ").strip()
            except (EOFError, KeyboardInterrupt):
                pass
            if topic or role:
                priors = priors or {}
                if topic:
                    priors["user_topic"] = topic
                if role:
                    priors["user_side"] = role
                print(f"  → topic={topic!r} mode={mode!r} role={role!r}")
    elif game == "mastermind":
        if not mode:
            mode = "broadcast"
        role = args.role or "viewer"
        if args.chunk == parser.get_default("chunk"):
            args.chunk = 15
        # Interactive priming: ask the user what they're watching/listening to.
        # A 5-second answer prevents misclassifications (SC2 vs Brood War, etc.)
        # and feeds the slow pass as authoritative priors.
        if not topic and not args.priors:
            print("\nMastermind setup (Enter to skip any line)")
            print("  topic — what are you watching/listening to?")
            print("    Examples: 'StarCraft Brood War, Korean league commentary'")
            print("              'Premier League — Arsenal vs Spurs'")
            print("              'physics lecture on quantum field theory'")
            print("  company — who are you with? (drives chime-in register)")
            print("    Common: mates / experts / mixed / family / strangers / alone")
            print("    Or free-form: 'in-laws at Sunday lunch', 'PhD students at dept social'")
            try:
                user_topic = input("  topic > ").strip()
                user_company = input("  company > ").strip()
            except (EOFError, KeyboardInterrupt):
                user_topic = ""
                user_company = ""
            if user_topic or user_company:
                priors = priors or {}
                if user_topic:
                    topic = user_topic
                    priors["user_topic"] = user_topic
                if user_company:
                    priors["user_company"] = user_company
                priors["note"] = ("User-provided context at session start. "
                                  "Treat as authoritative for domain/scene/"
                                  "company inference; chime-in register MUST "
                                  "match user_company's register guide.")
                print(f"  → topic={user_topic!r} company={user_company!r}")
    elif game == "translation":
        if not mode:
            mode = "listening"
        role = args.role or ""
        # Translation needs lower latency than mastermind/conversation: each
        # new chunk should produce a translation as soon as it lands. Below
        # 4-5s chunks whisper starts losing sentence-context accuracy, so 5s
        # is the floor we stay above. fast_interval=chunk_secs aligns the
        # loop firing with chunk arrivals; chunk_event then wakes the loop
        # immediately on transcription completion (no extra wait).
        if args.chunk == parser.get_default("chunk"):
            args.chunk = 6.5
        if args.fast_interval == parser.get_default("fast_interval"):
            args.fast_interval = 6
        if args.fast_window == parser.get_default("fast_window"):
            args.fast_window = 6
        # Stash language hints into priors so the game module's prompts
        # (which receive priors) can reference them. The whisper transcribe
        # call reads source_lang directly via the phone_mode parameter.
        priors = priors or {}
        if args.source_lang:
            priors["source_lang"] = args.source_lang
        priors["target_lang"] = args.target_lang
        print(f"\nTranslation mode: source={args.source_lang or 'auto-detect'} → target={args.target_lang}")
        print(f"  chunk={args.chunk}s | fast={args.fast_interval}s window={args.fast_window}s")
        if args.vad_chunking:
            print(f"  vad-chunking=on (silence-cut ≥{args.vad_silence_ms}ms after ≥{args.vad_min_dur}s, ceiling {args.chunk}s)")
        else:
            print(f"  vad-chunking=off (fixed {args.chunk}s)")
    else:
        role = args.role or ""

    players = [p.strip() for p in args.players.split(",")] if args.players else []
    push_jid = args.push_jid if args.push else None

    # Priors loaded above; derive state file path per game.
    state_path = f"output/{game}_state.json"
    if args.reset_state and os.path.exists(state_path):
        archived = state_path.replace(".json", f"_archived_{int(__import__('time').time())}.json")
        os.rename(state_path, archived)
        print(f"[two-pass] archived prior state → {archived}")
    # Also truncate the fast_log so the user only sees current-session output.
    # Stale entries persisting across runs make debugging dedup behaviour
    # confusing — old paraphrases look like new repetition.
    fast_log_path = f"output/{game}_fast.log"
    if args.reset_state and os.path.exists(fast_log_path):
        os.rename(fast_log_path, fast_log_path + f".archived_{int(__import__('time').time())}")
        print(f"[two-pass] archived prior fast log → {fast_log_path}.archived_*")

    if args.live or args.phone:
        players = resolve_players(players)

    if args.phone:
        phone_mode(players, role, chunk_secs=args.chunk, portion=args.portion,
                   push_jid=push_jid, game=game, topic=topic,
                   mode=mode, script=script,
                   two_pass=args.two_pass, priors=priors, state_path=state_path,
                   fast_interval=args.fast_interval, slow_interval=args.slow_interval,
                   fast_window=args.fast_window, slow_window=args.slow_window,
                   silence_trigger=args.silence_trigger,
                   source_lang=args.source_lang, target_lang=args.target_lang,
                   server_tts=args.server_tts, piper_voice=args.piper_voice,
                   piper_rate=args.piper_rate,
                   lang_lock=args.lang_lock,
                   vad_chunking=(game == "translation" and args.vad_chunking),
                   vad_silence_ms=args.vad_silence_ms,
                   vad_min_dur_s=args.vad_min_dur)
        return

    if args.live:
        live_mode(players, role, chunk_secs=args.chunk, portion=args.portion,
                  game=game, topic=topic, mode=mode, script=script,
                  two_pass=args.two_pass, priors=priors, state_path=state_path)
        return

    os.makedirs("output", exist_ok=True)
    transcript_path = "output/transcript.txt"

    if args.file:
        audio_path = extract_audio(args.file)
        transcript = transcribe(audio_path)

        with open("output/transcript.json", "w", encoding="utf-8") as f:
            json.dump(transcript, f, indent=2)

        transcript_text = format_transcript(transcript)
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(transcript_text)
        print("Transcript saved to output/transcript.txt")

    elif os.path.exists("output/transcript.json"):
        print("Using existing transcript at output/transcript.json")
        with open("output/transcript.json", "r", encoding="utf-8") as f:
            transcript = json.load(f)
        transcript_text = format_transcript(transcript)

    elif os.path.exists(transcript_path):
        print("Using existing transcript at output/transcript.txt")
        with open(transcript_path, "r", encoding="utf-8") as f:
            transcript_text = f.read()
        transcript = None

    else:
        print("ERROR: No --file provided and no saved transcript found.")
        return

    partial = args.portion < 1.0
    if partial:
        if transcript is None:
            print("WARNING: --portion requires transcript.json; falling back to full transcript.txt")
        else:
            transcript = slice_transcript(transcript, args.portion)
            transcript_text = format_transcript(transcript)
            pct = int(args.portion * 100)
            print(f"Analysing first {pct}% of transcript ({len(transcript)} segments)")

    analysis = analyze(transcript_text, players, role, partial=partial, game=game, topic=topic,
                       mode=mode, script=script,
                       two_pass=args.two_pass, priors=priors, state_path=state_path)
    with open("output/analysis.txt", "w", encoding="utf-8") as f:
        f.write(analysis)

    from report import generate_html
    html_report = generate_html(analysis)
    with open("output/analysis.html", "w", encoding="utf-8") as f:
        f.write(html_report)

    if push_jid:
        from ipc import push
        push(analysis, jid=push_jid)

    print("\n" + "=" * 60)
    print(analysis)
    print("=" * 60)
    print("\nSaved: output/analysis.txt | output/analysis.html")


if __name__ == "__main__":
    main()
