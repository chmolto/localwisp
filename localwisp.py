#!/usr/bin/env python3
"""
LocalWisp - push-to-talk voice dictation that runs 100% on your machine.

Hold the hotkey, speak, release: the audio is transcribed locally with
faster-whisper (GPU if available, CPU otherwise) and pasted into whatever
window has focus. Nothing is ever sent to the internet.
"""

import argparse
import os
import signal
import site
import sys
import threading
import time


# ─── Make pip-installed NVIDIA DLLs visible BEFORE importing ctranslate2 ───
def _register_nvidia_dlls():
    if not hasattr(os, "add_dll_directory"):  # only needed on Windows
        return
    extra = []
    for pkg in site.getsitepackages():
        nvidia = os.path.join(pkg, "nvidia")
        if not os.path.isdir(nvidia):
            continue
        for sub in os.listdir(nvidia):
            bin_dir = os.path.join(nvidia, sub, "bin")
            if os.path.isdir(bin_dir):
                extra.append(bin_dir)
                os.add_dll_directory(bin_dir)
    if extra:
        os.environ["PATH"] = os.pathsep.join(extra) + os.pathsep + os.environ.get("PATH", "")


_register_nvidia_dlls()

import tkinter as tk  # noqa: E402

import keyboard  # noqa: E402
import numpy as np  # noqa: E402
import pyautogui  # noqa: E402
import pyperclip  # noqa: E402
import sounddevice as sd  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402

# ─── Defaults (all of them can be overridden from the command line) ───
DEFAULT_HOTKEY = "ctrl+left windows"  # keys that must be held together
DEFAULT_MODEL = "medium"              # tiny | base | small | medium | large-v3 | turbo
DEFAULT_LANGUAGE = "es"               # ISO code, or "auto" to detect it
SAMPLE_RATE = 16000
POLL_MS = 20


def parse_args():
    p = argparse.ArgumentParser(
        prog="localwisp",
        description="Push-to-talk voice dictation with a local Whisper model.",
    )
    p.add_argument("--hotkey", default=DEFAULT_HOTKEY,
                   help='keys to hold, joined with "+" (default: "%(default)s"; '
                        'e.g. "f9", "ctrl+alt")')
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help="Whisper model: tiny, base, small, medium, large-v3, turbo... "
                        "(default: %(default)s)")
    p.add_argument("--language", default=DEFAULT_LANGUAGE,
                   help='spoken language as an ISO code (es, en, de...) or "auto" '
                        "(default: %(default)s)")
    p.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto",
                   help="where to run the model (default: %(default)s = GPU if available)")
    return p.parse_args()


# ─── Hotkey (only listens to the hotkey's own keys: this is NOT a keylogger) ───
class Hotkey:
    """Tracks whether every key of a combination is currently held down.

    Keys are matched by scan code rather than by name, because the `keyboard`
    library reports localized names (e.g. "windows izquierda" on a Spanish
    Windows), which would break name-based matching on other systems.
    """

    def __init__(self, combo: str):
        self.keys = [k.strip().lower() for k in combo.split("+") if k.strip()]
        if not self.keys:
            raise ValueError("empty hotkey")
        self._codes = []
        for key in self.keys:
            codes = set()
            for code in keyboard.key_to_scan_codes(key):
                codes.add(code)
                codes.add(code & 0xFF)  # extended keys (0xE0xx) arrive as their raw scan code
            self._codes.append(codes)
        self._pressed = set()
        for code in set().union(*self._codes):
            keyboard.hook_key(code, self._on_key)

    def _on_key(self, event):
        if event.event_type == keyboard.KEY_DOWN:
            self._pressed.add(event.scan_code)
        elif event.event_type == keyboard.KEY_UP:
            self._pressed.discard(event.scan_code)

    def is_held(self) -> bool:
        return all(codes & self._pressed for codes in self._codes)

    def label(self) -> str:
        def pretty(key):
            key = key.replace("left ", "").replace("right ", "")
            return "🪟" if key == "windows" else key.upper()
        return " + ".join(pretty(k) for k in self.keys)


# ─── On-screen indicator ───
class Indicator:
    W, H = 220, 44

    def __init__(self, hotkey_label: str, on_close):
        self.hotkey_label = hotkey_label
        self.root = tk.Tk()
        self.root.title("LocalWisp")
        self.root.overrideredirect(True)           # no title bar
        self.root.wm_attributes("-topmost", True)
        self.root.wm_attributes("-alpha", 0.92)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = sw - self.W - 24
        y = sh - self.H - 56                        # just above the taskbar
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")

        self.frame = tk.Frame(self.root, bg="#1e1e1e", bd=0)
        self.frame.pack(fill="both", expand=True)

        self.dot = tk.Label(self.frame, text="●", font=("Segoe UI", 13),
                            bg="#1e1e1e", fg="#444444")
        self.dot.pack(side="left", padx=(12, 4))

        self.lbl = tk.Label(self.frame, text="", font=("Segoe UI", 10),
                            bg="#1e1e1e", fg="#666666")
        self.lbl.pack(side="left")

        # Right-click menu
        self._menu = tk.Menu(self.root, tearoff=0, bg="#2a2a2a", fg="white",
                             activebackground="#444", activeforeground="white",
                             bd=0, font=("Segoe UI", 10))
        self._menu.add_command(label="Quit LocalWisp", command=on_close)
        for widget in (self.root, self.frame, self.dot, self.lbl):
            widget.bind("<Button-3>", self._show_menu)

        self._pulse_after = None
        self._set_idle()

    # ── States (thread-safe: always scheduled on the Tk thread) ──
    def idle(self):
        self.root.after(0, self._set_idle)

    def recording(self):
        self.root.after(0, lambda: self._set(
            "Recording...", "#2a0000", "#ff3333", "#ff6666"))
        self.root.after(0, self._pulse)

    def processing(self):
        self.root.after(0, self._set_processing)

    # ── Internals ──
    def _set_idle(self):
        self._stop_pulse()
        self._set(f"🎤  [{self.hotkey_label}]", "#1e1e1e", "#444444", "#666666")

    def _set_processing(self):
        self._stop_pulse()
        self._set("Transcribing...", "#1a1400", "#ffaa00", "#ffcc44")

    def _stop_pulse(self):
        if self._pulse_after:
            self.root.after_cancel(self._pulse_after)
            self._pulse_after = None

    def _set(self, text, bg, dot_fg, lbl_fg):
        self.frame.configure(bg=bg)
        self.dot.configure(bg=bg, fg=dot_fg)
        self.lbl.configure(text=text, bg=bg, fg=lbl_fg)
        self.root.configure(bg=bg)

    def _show_menu(self, event):
        self._menu.tk_popup(event.x_root, event.y_root)

    def _pulse(self, state=True):
        self.dot.configure(fg="#ff3333" if state else "#991111")
        self._pulse_after = self.root.after(400, lambda: self._pulse(not state))

    def run(self):
        self.root.mainloop()


# ─── Model ───
def load_model(name: str, device: str) -> WhisperModel:
    if device in ("auto", "cuda"):
        try:
            print(f"Loading Whisper model '{name}' on GPU...")
            model = WhisperModel(name, device="cuda", compute_type="float16")
            print("  (GPU active)")
            return model
        except Exception as exc:
            if device == "cuda":
                raise
            print(f"  ⚠️  GPU not available ({exc.__class__.__name__}), falling back to CPU...")
    print(f"Loading Whisper model '{name}' on CPU...")
    return WhisperModel(name, device="cpu", compute_type="int8")


# ─── Dictation loop ───
class Dictation:
    def __init__(self, model, language, hotkey: Hotkey, ui: Indicator):
        self.model = model
        self.language = language
        self.hotkey = hotkey
        self.ui = ui
        self._chunks = []
        self._stream = None

    def _audio_callback(self, indata, frames, t, status):
        self._chunks.append(indata.copy())

    def open_mic(self):
        self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                      dtype="float32", callback=self._audio_callback)
        self._stream.start()

    def close_mic(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def _transcribe_and_type(self, chunks):
        try:
            audio = np.concatenate(chunks).flatten()
            print("  Transcribing...", end="", flush=True)
            segments, _ = self.model.transcribe(
                audio,
                beam_size=5,
                language=self.language,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=300),
            )
            text = "".join(s.text for s in segments).strip()
        except Exception as exc:
            print(f" ❌ {exc}")
            self.ui.idle()
            return

        if not text:
            print(" (nothing detected)")
            self.ui.idle()
            return

        print(f" ✅ {text}")
        self.ui.idle()

        # Paste through the clipboard (works with accents and any keyboard layout),
        # then restore whatever the user had copied before.
        old_clip = pyperclip.paste()
        pyperclip.copy(text)
        time.sleep(0.05)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.1)
        pyperclip.copy(old_clip)

    def run(self):
        held = False
        while True:
            pressed = self.hotkey.is_held()

            if pressed and not held:
                held = True
                self._chunks = []
                self.open_mic()
                print("🎙️  Recording...", end="", flush=True)
                self.ui.recording()

            elif not pressed and held:
                held = False
                self.close_mic()
                chunks = list(self._chunks)
                secs = sum(len(c) for c in chunks) / SAMPLE_RATE
                print(f" ({secs:.1f}s)")

                if chunks:
                    self.ui.processing()
                    threading.Thread(target=self._transcribe_and_type,
                                     args=(chunks,), daemon=True).start()
                else:
                    self.ui.idle()

            time.sleep(POLL_MS / 1000)


# ─── Startup ───
def main():
    # Emoji-safe output even when stdout is piped/redirected (cp1252 on Windows).
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    language = None if args.language.lower() == "auto" else args.language

    try:
        hotkey = Hotkey(args.hotkey)
    except ValueError as exc:
        sys.exit(f"Invalid hotkey {args.hotkey!r}: {exc}")

    model = load_model(args.model, args.device)
    print("✅ Model ready.\n")

    dictation = None

    def shutdown(*_):
        if dictation:
            dictation.close_mic()
        try:
            ui.root.destroy()
        except Exception:
            pass
        sys.exit(0)

    ui = Indicator(hotkey.label(), on_close=shutdown)
    dictation = Dictation(model, language, hotkey, ui)

    print(f"🎤 LocalWisp running  |  Hotkey: [{hotkey.label()}]  |  "
          f"Model: {args.model}  |  Language: {language or 'auto'}")
    print("   Hold the hotkey to record, release to type.")
    print("   Right-click the indicator to quit.\n")

    signal.signal(signal.SIGINT, shutdown)
    ui.root.protocol("WM_DELETE_WINDOW", shutdown)

    # Wake the Tk mainloop every 200 ms so Ctrl+C in the console is handled.
    def tick():
        ui.root.after(200, tick)

    ui.root.after(200, tick)
    threading.Thread(target=dictation.run, daemon=True).start()
    ui.run()


if __name__ == "__main__":
    main()
