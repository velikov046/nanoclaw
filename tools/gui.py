import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext
import subprocess
import threading
import os

YTDLP    = r"C:\Users\Sol\AppData\Local\Python\pythoncore-3.14-64\Scripts\yt-dlp.exe"
FFMPEG   = r"C:\Users\Sol\ffmpeg\bin"
OUT_DIR  = r"C:\Users\Sol\Videos"

RESOLUTIONS = ["Best", "2160p", "1440p", "1080p", "720p", "480p", "360p"]
FORMATS     = ["mp4", "mkv", "webm"]
AUDIO_FMTS  = ["mp3", "m4a", "opus", "flac"]
SUB_FMTS    = ["srt", "ass", "vtt", "lrc"]


def build_command(url, resolution, fmt, audio_fmt, audio_only, playlist,
                  geo_bypass, subs, auto_subs, sub_lang, sub_fmt, embed_subs, out_dir):
    if playlist:
        out_tmpl = os.path.join(out_dir, "%(playlist)s", "%(playlist_index)s - %(title)s.%(ext)s")
    else:
        out_tmpl = os.path.join(out_dir, "%(title)s.%(ext)s")

    cmd = [
        YTDLP,
        "--ffmpeg-location", FFMPEG,
        "--js-runtimes", "node",
        "-o", out_tmpl,
    ]
    if not playlist:
        cmd.append("--no-playlist")
    if geo_bypass:
        cmd.append("--geo-bypass")

    if audio_only:
        cmd += ["-x", "--audio-format", audio_fmt]
    else:
        if resolution == "Best":
            f = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]"
        else:
            h = resolution.replace("p", "")
            f = f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/best[height<={h}][ext=mp4]"
        cmd += ["-f", f, "--merge-output-format", fmt]

    if subs or auto_subs:
        if subs:
            cmd.append("--write-subs")
        if auto_subs:
            cmd.append("--write-auto-subs")
        if sub_lang.strip():
            cmd += ["--sub-langs", sub_lang.strip()]
        cmd += ["--sub-format", sub_fmt]
        if embed_subs and not audio_only:
            cmd.append("--embed-subs")

    cmd.append(url)
    return cmd


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("yt-dlp Downloader")
        self.resizable(False, False)
        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 5}

        # URL
        tk.Label(self, text="URL:").grid(row=0, column=0, sticky="e", **pad)
        self.url_var = tk.StringVar()
        tk.Entry(self, textvariable=self.url_var, width=55).grid(row=0, column=1, columnspan=2, sticky="ew", **pad)

        # Resolution
        tk.Label(self, text="Resolution:").grid(row=1, column=0, sticky="e", **pad)
        self.res_var = tk.StringVar(value="1080p")
        self.res_cb = ttk.Combobox(self, textvariable=self.res_var, values=RESOLUTIONS, state="readonly", width=15)
        self.res_cb.grid(row=1, column=1, sticky="w", **pad)

        # Format
        tk.Label(self, text="Format:").grid(row=2, column=0, sticky="e", **pad)
        self.fmt_var = tk.StringVar(value="mp4")
        self.fmt_cb = ttk.Combobox(self, textvariable=self.fmt_var, values=FORMATS, state="readonly", width=15)
        self.fmt_cb.grid(row=2, column=1, sticky="w", **pad)

        self.afmt_var = tk.StringVar(value="mp3")
        self.afmt_cb = ttk.Combobox(self, textvariable=self.afmt_var, values=AUDIO_FMTS, state="readonly", width=15)
        self.afmt_cb.grid(row=2, column=1, sticky="w", **pad)
        self.afmt_cb.grid_remove()

        # Checkboxes col 2
        self.audio_only = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="Sound only", variable=self.audio_only,
                       command=self._on_audio_toggle).grid(row=1, column=2, sticky="w", **pad)

        self.playlist = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="Whole playlist", variable=self.playlist).grid(row=2, column=2, sticky="w", **pad)

        self.geo_bypass = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="Geo bypass", variable=self.geo_bypass).grid(row=3, column=2, sticky="w", **pad)

        # Output folder
        tk.Label(self, text="Save to:").grid(row=3, column=0, sticky="e", **pad)
        self.dir_var = tk.StringVar(value=OUT_DIR)
        tk.Entry(self, textvariable=self.dir_var, width=45).grid(row=3, column=1, sticky="ew", **pad)
        tk.Button(self, text="Browse", command=self._browse).grid(row=3, column=2, **pad)

        # Subtitles row
        tk.Label(self, text="Subtitles:").grid(row=4, column=0, sticky="e", **pad)
        sub_frame = tk.Frame(self)
        sub_frame.grid(row=4, column=1, columnspan=2, sticky="w", **pad)

        self.subs = tk.BooleanVar(value=False)
        tk.Checkbutton(sub_frame, text="Download", variable=self.subs).pack(side="left")

        self.auto_subs = tk.BooleanVar(value=False)
        tk.Checkbutton(sub_frame, text="Auto-generated", variable=self.auto_subs).pack(side="left", padx=(8, 0))

        self.embed_subs = tk.BooleanVar(value=False)
        tk.Checkbutton(sub_frame, text="Embed", variable=self.embed_subs).pack(side="left", padx=(8, 0))

        tk.Label(sub_frame, text="Lang:").pack(side="left", padx=(12, 2))
        self.sub_lang = tk.StringVar(value="en")
        tk.Entry(sub_frame, textvariable=self.sub_lang, width=8).pack(side="left")

        tk.Label(sub_frame, text="Fmt:").pack(side="left", padx=(8, 2))
        self.sub_fmt = tk.StringVar(value="srt")
        ttk.Combobox(sub_frame, textvariable=self.sub_fmt, values=SUB_FMTS,
                     state="readonly", width=6).pack(side="left")

        # Download button
        self.dl_btn = tk.Button(self, text="Download", command=self._start, bg="#2196F3", fg="white",
                                font=("Segoe UI", 10, "bold"), padx=10)
        self.dl_btn.grid(row=5, column=0, columnspan=3, pady=8)

        # Log
        self.log = scrolledtext.ScrolledText(self, width=72, height=18, state="disabled",
                                             font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        self.log.grid(row=6, column=0, columnspan=3, padx=10, pady=(0, 10))

    def _on_audio_toggle(self):
        if self.audio_only.get():
            self.res_cb.config(state="disabled")
            self.fmt_cb.grid_remove()
            self.afmt_cb.grid()
        else:
            self.res_cb.config(state="readonly")
            self.afmt_cb.grid_remove()
            self.fmt_cb.grid()

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get())
        if d:
            self.dir_var.set(d)

    def _start(self):
        url = self.url_var.get().strip()
        if not url:
            self._log("Please enter a URL.\n")
            return
        self.dl_btn.config(state="disabled", text="Downloading...")
        self._log(f"Starting: {url}\n")
        threading.Thread(target=self._download, args=(url,), daemon=True).start()

    def _download(self, url):
        cmd = build_command(
            url,
            self.res_var.get(),
            self.fmt_var.get(),
            self.afmt_var.get(),
            self.audio_only.get(),
            self.playlist.get(),
            self.geo_bypass.get(),
            self.subs.get(),
            self.auto_subs.get(),
            self.sub_lang.get(),
            self.sub_fmt.get(),
            self.embed_subs.get(),
            self.dir_var.get(),
        )
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace")
            for line in proc.stdout:
                self._log(line)
            proc.wait()
            self._log("\nDone.\n" if proc.returncode == 0 else f"\nError (code {proc.returncode}).\n")
        except Exception as e:
            self._log(f"\nFailed: {e}\n")
        finally:
            self.dl_btn.config(state="normal", text="Download")

    def _log(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")


if __name__ == "__main__":
    App().mainloop()
