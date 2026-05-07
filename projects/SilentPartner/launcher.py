#!/usr/bin/env python3
"""
Host-side launcher daemon for SilentPartner phone mode.

Watches an IPC directory shared with the Velikov container and manages
the lifecycle of a single phone-mode session: start, stop, on-demand
analysis, idle timeout.

Request files (written by the MCP tool, deleted by the daemon after
processing):
  - start.json    {game, role?, players?, topic?, chunk?}
  - stop.json     {}
  - analyze.json  {}

Response files (written by the daemon, read by the MCP tool):
  - url.json      {url, started_at, game}            — written when tunnel ready
  - status.json   {state, chunks, last_chunk_at, ...} — refreshed continuously
  - analysis.json {txt_path, html_path, segments}    — written after analyze
  - error.json    {phase, message}                   — written on any failure

State machine: IDLE -> STARTING -> RUNNING -> STOPPING -> IDLE.
Only one session at a time. Idle timeout: 15 min with no captured chunks.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_DIR = Path("/home/aurellian/nanoclaw/projects/SilentPartner")
# Both Velikov and Velikov-Visions can drive SilentPartner. Each writes
# requests into its own IPC subtree; the launcher watches all of them and
# mirrors response files back to every dir so whichever agent issued the
# request can find them.
IPC_DIRS = [
    Path("/home/aurellian/nanoclaw/data/ipc/velikov/silentpartner"),
    Path("/home/aurellian/nanoclaw/data/ipc/velikov-visions/silentpartner"),
]
VENV_PY = PROJECT_DIR / "venv" / "bin" / "python"
TUNNEL_LOG = Path("/tmp/cloudflared.log")

POLL_INTERVAL_S = 1.0
TUNNEL_DEADLINE_S = 45.0
IDLE_TIMEOUT_S = 15 * 60
# Analysis cost scales with transcript length. claude --print over a 6-min
# debate took >3 min in testing, so we keep the floor generous and add per-
# segment headroom. analyse() picks the live deadline from session.chunks.
ANALYSIS_DEADLINE_BASE_S = 240.0
ANALYSIS_DEADLINE_PER_SEGMENT_S = 8.0
ANALYSIS_DEADLINE_MAX_S = 900.0
POINTER_DEADLINE_S = 60.0
CHUNK_RE = re.compile(r"\[(\d+)m captured \| (\d+) segments\]")
TUNNEL_RE = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")


def log(msg: str) -> None:
    print(f"[launcher {time.strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass
class Session:
    proc: subprocess.Popen
    output_path: Path
    started_at: float
    game: str
    auth_token: str
    url: Optional[str] = None
    chunks: int = 0
    last_chunk_at: Optional[float] = None
    analysis_count: int = 0
    stopping: bool = False


class Launcher:
    def __init__(self) -> None:
        self.session: Optional[Session] = None
        self.stop_flag = threading.Event()

    # ---- IPC helpers ----

    def write_response(self, name: str, data: dict) -> None:
        # Mirror to every IPC dir so any caller can read it.
        payload = json.dumps(data, indent=2)
        for d in IPC_DIRS:
            d.mkdir(parents=True, exist_ok=True)
            path = d / name
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(payload)
            os.replace(tmp, path)

    def write_status(self) -> None:
        s = self.session
        if s is None:
            data = {"state": "idle"}
        else:
            data = {
                "state": "stopping" if s.stopping else ("running" if s.url else "starting"),
                "url": s.url,
                "game": s.game,
                "chunks": s.chunks,
                "last_chunk_at": s.last_chunk_at,
                "started_at": s.started_at,
                "analysis_count": s.analysis_count,
            }
        self.write_response("status.json", data)

    def write_error(self, phase: str, message: str) -> None:
        log(f"ERROR [{phase}] {message}")
        self.write_response("error.json", {"phase": phase, "message": message,
                                           "timestamp": time.time()})

    def consume_request(self, name: str) -> Optional[dict]:
        # Look across all IPC dirs; first one to have the request wins.
        for d in IPC_DIRS:
            path = d / name
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                log(f"bad request {name} in {d}: {e}")
                data = {}
            try:
                path.unlink()
            except OSError:
                pass
            return data
        return None

    # ---- Process management ----

    def ensure_cloudflared(self) -> bool:
        bin_path = shutil.which("cloudflared") or "/tmp/cloudflared"
        if Path(bin_path).exists():
            return True
        log("cloudflared missing, downloading")
        try:
            subprocess.run(
                ["curl", "-sL", "-o", "/tmp/cloudflared",
                 "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"],
                check=True, timeout=60,
            )
            os.chmod("/tmp/cloudflared", 0o755)
            return True
        except (subprocess.SubprocessError, OSError) as e:
            self.write_error("cloudflared_install", str(e))
            return False

    def start(self, req: dict) -> None:
        if self.session is not None:
            self.write_error("start", "already running; stop first")
            return

        if not self.ensure_cloudflared():
            return

        game = req.get("game", "debate")
        role = req.get("role", "spectator")
        players = req.get("players", "spectator")
        topic = req.get("topic", "")
        mode = req.get("mode", "")
        script = req.get("script", "")
        chunk = int(req.get("chunk", 15))

        # Reset cloudflared log so we don't pick up a stale URL
        try:
            TUNNEL_LOG.write_text("")
        except OSError:
            pass

        output_path = PROJECT_DIR / "output" / "launcher_session.log"
        output_path.parent.mkdir(exist_ok=True)
        out_fh = open(output_path, "w")

        cmd = [str(VENV_PY), "main.py", "--phone",
               "--game", game,
               "--players", players,
               "--chunk", str(chunk)]
        if game == "secret_hitler" and role:
            cmd += ["--role", role]
        if game == "debate" and topic:
            cmd += ["--topic", topic]
        if game == "blood_on_the_clocktower":
            if mode:
                cmd += ["--mode", mode]
            if script:
                cmd += ["--script", script]
            if role:
                cmd += ["--role", role]

        # Per-session auth token; pinned via env so main.py uses the same one.
        auth_token = secrets.token_urlsafe(16)
        env = os.environ.copy()
        env["SILENTPARTNER_AUTH_TOKEN"] = auth_token

        log(f"spawning: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd, cwd=str(PROJECT_DIR),
            stdin=subprocess.PIPE,
            stdout=out_fh, stderr=subprocess.STDOUT,
            env=env,
        )

        self.session = Session(
            proc=proc, output_path=output_path,
            started_at=time.time(), game=game, auth_token=auth_token,
        )
        self.write_status()

        # Start a thread to wait for the URL to land
        threading.Thread(target=self._await_url, daemon=True).start()

    def _await_url(self) -> None:
        """Poll the cloudflared log until the tunnel URL appears."""
        s = self.session
        if s is None:
            return
        deadline = time.time() + TUNNEL_DEADLINE_S
        while time.time() < deadline:
            if s.proc.poll() is not None:
                self.write_error("tunnel", f"main.py exited early (rc={s.proc.returncode})")
                self._cleanup()
                return
            try:
                content = TUNNEL_LOG.read_text()
            except OSError:
                content = ""
            m = TUNNEL_RE.search(content)
            if m:
                bare = m.group(0)
                s.url = f"{bare}/?t={s.auth_token}"
                self.write_response("url.json", {
                    "url": s.url,
                    "started_at": s.started_at,
                    "game": s.game,
                })
                self.write_status()
                log(f"tunnel ready: {bare} (auth-token attached)")
                return
            time.sleep(0.5)
        self.write_error("tunnel", "no tunnel URL after 45s")
        self.stop()

    def stop(self) -> None:
        s = self.session
        if s is None:
            self.write_status()
            return
        s.stopping = True
        self.write_status()
        log("stopping session")
        try:
            s.proc.terminate()
            s.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            s.proc.kill()
        except Exception as e:
            log(f"terminate error: {e}")
        self._cleanup()

    def _cleanup(self) -> None:
        s = self.session
        if s is None:
            return
        # Kill any cloudflared we may have spawned (main.py owns it but it
        # can leak on abnormal exit)
        subprocess.run(["pkill", "-f", "cloudflared tunnel --url http://localhost:8766"],
                       check=False)
        for stale in ("url.json",):
            for d in IPC_DIRS:
                try:
                    (d / stale).unlink()
                except FileNotFoundError:
                    pass
        self.session = None
        self.write_status()

    # ---- Pointer (mid-debate tactical Q&A) ----

    POINTER_PROMPT_TEMPLATE = """You are a debate strategist watching a live debate. Below is the transcript captured so far. The user is about to speak again and is asking for tactical guidance. Be concise — 3–6 short bullet points or a short paragraph. Specific over generic.

Focus this answer on: {focus}

User question: {question}

Transcript so far:
{transcript}
"""

    def pointer(self, req: dict) -> None:
        s = self.session
        if s is None or s.url is None:
            self.write_error("pointer", "no session running")
            return

        question = (req.get("question") or "").strip()
        if not question:
            self.write_error("pointer", "missing question")
            return
        focus = req.get("focus") or "tactical pointers"

        # Tell main.py to flush the transcript to disk
        try:
            assert s.proc.stdin is not None
            s.proc.stdin.write(b"s\n")
            s.proc.stdin.flush()
        except (OSError, AssertionError) as e:
            self.write_error("pointer", f"snapshot stdin write failed: {e}")
            return

        # Wait for the snapshot file to appear / get fresh content
        live_path = PROJECT_DIR / "output" / "live_transcript.txt"
        deadline = time.time() + 8
        before_mtime = live_path.stat().st_mtime if live_path.exists() else 0
        transcript_text = ""
        while time.time() < deadline:
            if live_path.exists() and live_path.stat().st_mtime > before_mtime:
                try:
                    transcript_text = live_path.read_text()
                except OSError:
                    transcript_text = ""
                break
            time.sleep(0.3)
        if not transcript_text:
            self.write_error("pointer", "snapshot file did not appear")
            return

        prompt = self.POINTER_PROMPT_TEMPLATE.format(
            focus=focus, question=question, transcript=transcript_text,
        )

        try:
            result = subprocess.run(
                ["claude", "--print", "--model", "claude-sonnet-4-6"],
                input=prompt, capture_output=True, text=True,
                timeout=POINTER_DEADLINE_S, check=True,
            )
            answer = result.stdout.strip()
        except subprocess.TimeoutExpired:
            self.write_error("pointer", f"claude --print timed out after {POINTER_DEADLINE_S}s")
            return
        except subprocess.CalledProcessError as e:
            self.write_error("pointer", f"claude --print failed: {e.stderr.strip()[:200]}")
            return

        self.write_response("pointer_result.json", {
            "answer": answer,
            "question": question,
            "focus": focus,
            "produced_at": time.time(),
            "segments_seen": s.chunks,
        })
        log(f"pointer answered ({len(answer)} chars)")

    # ---- Analysis ----

    def analyse(self) -> None:
        s = self.session
        if s is None or s.url is None:
            self.write_error("analyze", "no session running")
            return
        prev_count = s.analysis_count
        existing = sorted((PROJECT_DIR / "output").glob("phone_analysis_*.txt"))
        prev_max = max((int(p.stem.rsplit("_", 1)[1]) for p in existing), default=0)
        # Trigger analysis by writing a newline to main.py's stdin
        try:
            assert s.proc.stdin is not None
            s.proc.stdin.write(b"\n")
            s.proc.stdin.flush()
        except (OSError, AssertionError) as e:
            self.write_error("analyze", f"stdin write failed: {e}")
            return

        # Wait for a new analysis file to appear. claude --print over a long
        # transcript easily takes 4–8 min; size the deadline accordingly.
        deadline_s = min(
            ANALYSIS_DEADLINE_MAX_S,
            ANALYSIS_DEADLINE_BASE_S + ANALYSIS_DEADLINE_PER_SEGMENT_S * s.chunks,
        )
        deadline = time.time() + deadline_s
        log(f"analysis: waiting up to {deadline_s:.0f}s ({s.chunks} chunks)")
        while time.time() < deadline:
            files = sorted((PROJECT_DIR / "output").glob("phone_analysis_*.txt"))
            new = [p for p in files
                   if int(p.stem.rsplit("_", 1)[1]) > prev_max]
            if new:
                latest = new[-1]
                html = latest.with_suffix(".html")
                s.analysis_count = prev_count + 1
                self.write_response("analysis.json", {
                    "txt_path": str(latest),
                    "html_path": str(html) if html.exists() else None,
                    "segments": s.chunks,
                    "produced_at": time.time(),
                })
                self.write_status()
                log(f"analysis ready: {latest.name}")
                return
            time.sleep(1)
        self.write_error("analyze", f"analysis did not complete within {deadline_s:.0f}s")

    # ---- Output watcher ----

    def parse_output(self) -> None:
        """Parse main.py's output for chunk-arrival lines."""
        s = self.session
        if s is None:
            return
        try:
            content = s.output_path.read_text()
        except OSError:
            return
        matches = CHUNK_RE.findall(content)
        if matches and len(matches) > s.chunks:
            s.chunks = len(matches)
            s.last_chunk_at = time.time()
            self.write_status()

    # ---- Main loop ----

    def main_loop(self) -> None:
        for d in IPC_DIRS:
            d.mkdir(parents=True, exist_ok=True)
            # Clear any stale request files from a prior run
            for stale in ("start.json", "stop.json", "analyze.json"):
                try:
                    (d / stale).unlink()
                except FileNotFoundError:
                    pass
        self.write_status()
        log(f"watching {[str(d) for d in IPC_DIRS]}")

        while not self.stop_flag.is_set():
            req = self.consume_request("start.json")
            if req is not None:
                self.start(req)

            req = self.consume_request("analyze.json")
            if req is not None:
                self.analyse()

            req = self.consume_request("pointer.json")
            if req is not None:
                self.pointer(req)

            req = self.consume_request("stop.json")
            if req is not None:
                self.stop()

            self.parse_output()

            # Detect process exit
            s = self.session
            if s is not None and s.proc.poll() is not None and not s.stopping:
                log(f"main.py exited unexpectedly (rc={s.proc.returncode})")
                self._cleanup()

            # Idle timeout
            if s is not None and s.url is not None:
                idle_for = time.time() - (s.last_chunk_at or s.started_at)
                if idle_for > IDLE_TIMEOUT_S:
                    log(f"idle timeout after {idle_for:.0f}s")
                    self.stop()

            time.sleep(POLL_INTERVAL_S)


def main() -> None:
    launcher = Launcher()

    def shutdown(_signum, _frame):
        log("shutdown signal received")
        launcher.stop_flag.set()
        launcher.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    launcher.main_loop()


if __name__ == "__main__":
    main()
