#!/usr/bin/env python3
"""
Emotion Tagger — GUI
Inserts ElevenLabs v3 audio tags into text via Claude.
"""

import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

try:
    import anthropic
except ImportError:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("Missing dependency", "anthropic package not found.\n\nRun: pip install anthropic")
    sys.exit(1)

if '/home/aurellian/nanoclaw/tools' not in sys.path:
    sys.path.insert(0, '/home/aurellian/nanoclaw/tools')
from claude_oauth import make_client

# ── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_CHARACTER = """Stella is sharp, cheeky, warm, and direct. Confident but not cold. Dry humour is her default. Never flat or monotone."""

DEFAULT_CONTEXT = ""

TAGGER_SYSTEM = """You are a voice direction assistant.

Your job: take the provided text and insert ElevenLabs v3 audio tags at natural points so the \
voice is performed correctly — not just read aloud.

## Character profile
{character}

## Tag syntax
Tags go in square brackets immediately before the word or phrase they affect: `[playfully] oh really?`
Tags can be stacked: `[whispers][nervous] don't tell him I said that`

## Available tags
Emotions: [excited] [happy] [nervous] [curious] [mischievously] [calm]
Delivery: [whispers] [playfully] [cheerfully] [flatly] [deadpan] [quietly]
Reactions: [laughs] [light chuckle] [sighs] [sigh of relief] [gasps] [gulps]
Pacing: [pause] [hesitates] [stammers]
Sensual/slow: [slowly] [softly] [breathy] [warmly]

## Rules
- One or two tags per sentence maximum. Less is more.
- Only tag where the delivery would genuinely differ from neutral speech.
- Never tag every sentence. Leave untagged lines where neutral is correct.
- Do not explain your choices. Return only the tagged text."""


# ── Env helper ──────────────────────────────────────────────────────────────

def _env(key):
    val = os.environ.get(key)
    if val:
        return val
    for path in [
        r"\\wsl.localhost\Ubuntu\home\aurellian\nanoclaw\.env",
        "/home/aurellian/nanoclaw/.env",
    ]:
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(key + "="):
                        return line.split("=", 1)[1]
        except FileNotFoundError:
            continue
    return None


# ── Core tagger ─────────────────────────────────────────────────────────────

def tag_text(text: str, character: str, context: str) -> str:
    system = TAGGER_SYSTEM.format(character=character.strip())
    user_parts = []
    if context.strip():
        user_parts.append(f"## Conversation context\n{context.strip()}\n")
    user_parts.append(f"## Text to tag\n{text.strip()}")

    client = make_client(api_key=_env("ANTHROPIC_API_KEY"))
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": "\n\n".join(user_parts)}],
    )
    return msg.content[0].text


# ── GUI ─────────────────────────────────────────────────────────────────────

BG       = "#1e1e2e"
SURFACE  = "#2a2a3d"
ACCENT   = "#7c6af7"
FG       = "#cdd6f4"
FG_DIM   = "#6c7086"
FONT     = ("Segoe UI", 10)
FONT_MONO= ("Consolas", 10)


class TaggerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Emotion Tagger")
        self.configure(bg=BG)
        self.geometry("860x780")
        self.resizable(True, True)
        self._build()

    def _label(self, parent, text):
        return tk.Label(parent, text=text, bg=BG, fg=FG_DIM, font=("Segoe UI", 9), anchor="w")

    def _textarea(self, parent, height, font=FONT):
        t = tk.Text(
            parent, height=height, font=font,
            bg=SURFACE, fg=FG, insertbackground=FG,
            relief="flat", padx=8, pady=6,
            wrap="word", undo=True,
            selectbackground=ACCENT, selectforeground="#ffffff",
        )
        sb = ttk.Scrollbar(parent, command=t.yview)
        t.configure(yscrollcommand=sb.set)
        return t, sb

    def _build(self):
        pad = dict(padx=14, pady=4)

        # ── Character ──
        self._label(self, "Character profile").pack(fill="x", **pad)
        row = tk.Frame(self, bg=BG)
        row.pack(fill="both", expand=False, padx=14, pady=(0, 8))
        self.char_box, sb = self._textarea(row, height=5)
        self.char_box.insert("1.0", DEFAULT_CHARACTER)
        self.char_box.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # ── Context ──
        self._label(self, "Conversation context  (optional — last few messages for mood)").pack(fill="x", **pad)
        row2 = tk.Frame(self, bg=BG)
        row2.pack(fill="both", expand=False, padx=14, pady=(0, 8))
        self.ctx_box, sb2 = self._textarea(row2, height=4)
        self.ctx_box.insert("1.0", DEFAULT_CONTEXT)
        self.ctx_box.pack(side="left", fill="both", expand=True)
        sb2.pack(side="right", fill="y")

        # ── Input ──
        self._label(self, "Text to tag").pack(fill="x", **pad)
        row3 = tk.Frame(self, bg=BG)
        row3.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self.input_box, sb3 = self._textarea(row3, height=8)
        self.input_box.pack(side="left", fill="both", expand=True)
        sb3.pack(side="right", fill="y")

        # ── Button ──
        self.btn = tk.Button(
            self, text="Tag it", command=self._run,
            bg=ACCENT, fg="#ffffff", activebackground="#6a59e0",
            font=("Segoe UI", 10, "bold"), relief="flat",
            padx=20, pady=8, cursor="hand2",
        )
        self.btn.pack(pady=(4, 8))

        self.status = tk.Label(self, text="", bg=BG, fg=FG_DIM, font=("Segoe UI", 9))
        self.status.pack()

        # ── Output ──
        self._label(self, "Tagged output").pack(fill="x", **pad)
        row4 = tk.Frame(self, bg=BG)
        row4.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.output_box, sb4 = self._textarea(row4, height=8, font=FONT_MONO)
        self.output_box.configure(state="disabled")
        self.output_box.pack(side="left", fill="both", expand=True)
        sb4.pack(side="right", fill="y")

    def _run(self):
        text = self.input_box.get("1.0", "end").strip()
        if not text:
            self.status.config(text="No input text.")
            return
        self.btn.config(state="disabled")
        self.status.config(text="Tagging…")
        self._set_output("")
        threading.Thread(target=self._worker, args=(text,), daemon=True).start()

    def _worker(self, text):
        char = self.char_box.get("1.0", "end").strip()
        ctx  = self.ctx_box.get("1.0", "end").strip()
        try:
            result = tag_text(text, char, ctx)
            self.after(0, self._set_output, result)
            self.after(0, self.status.config, {"text": "Done."})
        except Exception as e:
            self.after(0, self._set_output, f"Error: {e}")
            self.after(0, self.status.config, {"text": "Failed."})
        finally:
            self.after(0, self.btn.config, {"state": "normal"})

    def _set_output(self, text):
        self.output_box.configure(state="normal")
        self.output_box.delete("1.0", "end")
        self.output_box.insert("1.0", text)
        self.output_box.configure(state="disabled")


if __name__ == "__main__":
    app = TaggerApp()
    app.mainloop()
