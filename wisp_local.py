#!/usr/bin/env python3
"""
WispLocal - Dictado por voz local con Whisper + RTX
Mantén pulsado el hotkey para grabar, suéltalo para transcribir y escribir.
"""

import sys
import os
import site
import signal
import threading
import time

# ─── PATH de DLLs de nvidia ANTES de importar ctranslate2 ─────
def _register_nvidia_dlls():
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
# ──────────────────────────────────────────────────────────────

import tkinter as tk
import numpy as np
import sounddevice as sd
import keyboard
import pyperclip
import pyautogui
from faster_whisper import WhisperModel

# ─── Configuración ────────────────────────────────────────────
# Teclas que deben estar pulsadas simultáneamente para grabar
HOTKEY_KEYS = {"windows izquierda", "ctrl"}
#  Otras combis: {"right ctrl"} | {"right alt"} | {"left windows","left alt"}
MODEL_SIZE  = "medium"      # tiny | base | small | medium | large-v3
LANGUAGE    = "es"          # None = auto-detectar idioma
SAMPLE_RATE = 16000
POLL_MS     = 20
# ──────────────────────────────────────────────────────────────


# ─── Indicador visual ─────────────────────────────────────────
class Indicator:
    W, H = 220, 44

    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)           # sin barra de título
        self.root.wm_attributes("-topmost", True)
        self.root.wm_attributes("-alpha", 0.92)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x  = sw - self.W - 24
        y  = sh - self.H - 56                     # justo encima de la barra de tareas
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")

        self.frame = tk.Frame(self.root, bg="#1e1e1e", bd=0)
        self.frame.pack(fill="both", expand=True)

        self.dot = tk.Label(self.frame, text="●", font=("Segoe UI", 13),
                            bg="#1e1e1e", fg="#444444")
        self.dot.pack(side="left", padx=(12, 4))

        self.lbl = tk.Label(self.frame, text="Listo  [right ctrl]",
                            font=("Segoe UI", 10), bg="#1e1e1e", fg="#666666")
        self.lbl.pack(side="left")

        # Menú clic derecho
        self._menu = tk.Menu(self.root, tearoff=0, bg="#2a2a2a", fg="white",
                             activebackground="#444", activeforeground="white",
                             bd=0, font=("Segoe UI", 10))
        self._menu.add_command(label="Cerrar WispLocal", command=_shutdown)
        for widget in (self.root, self.frame, self.dot, self.lbl):
            widget.bind("<Button-3>", self._show_menu)

        self._pulse_after = None
        label = " + ".join(
            k.replace("left ", "").replace("right ", "").replace("windows izquierda", "🪟").replace("alt", "ALT").replace("ctrl", "CTRL")
            for k in sorted(HOTKEY_KEYS)
        )
        self._set(f"🎤  [{label}]", "#1e1e1e", "#444444", "#666666")

    # ── Estados ──
    def idle(self):
        label = " + ".join(
            k.replace("left ", "").replace("right ", "").replace("windows izquierda", "🪟").replace("alt", "ALT").replace("ctrl", "CTRL")
            for k in sorted(HOTKEY_KEYS)
        )
        self.root.after(0, lambda: self._set(
            f"🎤  [{label}]", "#1e1e1e", "#444444", "#666666"))

    def recording(self):
        self.root.after(0, lambda: self._set(
            "Grabando...", "#2a0000", "#ff3333", "#ff6666"))
        self.root.after(0, self._pulse)

    def processing(self):
        if self._pulse_after:
            self.root.after_cancel(self._pulse_after)
            self._pulse_after = None
        self.root.after(0, lambda: self._set(
            "Transcribiendo...", "#1a1400", "#ffaa00", "#ffcc44"))

    # ── Internos ──
    def _set(self, text, bg, dot_fg, lbl_fg):
        self.frame.configure(bg=bg)
        self.dot.configure(bg=bg, fg=dot_fg)
        self.lbl.configure(text=text, bg=bg, fg=lbl_fg)
        self.root.configure(bg=bg)

    def _show_menu(self, event):
        self._menu.tk_popup(event.x_root, event.y_root)

    def _pulse(self, state=True):
        color = "#ff3333" if state else "#991111"
        self.dot.configure(fg=color)
        self._pulse_after = self.root.after(400, lambda: self._pulse(not state))

    def run(self):
        self.root.mainloop()


# ─── Detección de hotkey (solo las teclas del hotkey, sin keylogger) ──
_pressed_keys: set = set()

def _on_hotkey_key(e):
    """Solo se llama para las teclas definidas en HOTKEY_KEYS."""
    if e.event_type == keyboard.KEY_DOWN:
        _pressed_keys.add(e.name)
    elif e.event_type == keyboard.KEY_UP:
        _pressed_keys.discard(e.name)

for _key in HOTKEY_KEYS:
    keyboard.hook_key(_key, _on_hotkey_key)

def _hotkey_active() -> bool:
    return HOTKEY_KEYS.issubset(_pressed_keys)

# ─── Audio + Whisper ──────────────────────────────────────────
try:
    print(f"Cargando modelo Whisper '{MODEL_SIZE}' en GPU...")
    model = WhisperModel(MODEL_SIZE, device="cuda", compute_type="float16")
    print("  (GPU activa)")
except Exception:
    print("  ⚠️  GPU no disponible, usando CPU...")
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
print("✅ Modelo listo.\n")

_recording    = False
_audio_chunks = []
_hotkey_down  = False
_stream       = None
_ui: Indicator = None   # se asigna después de crear la ventana


def _audio_callback(indata, frames, t, status):
    _audio_chunks.append(indata.copy())


def _open_mic():
    global _stream
    _stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                              dtype="float32", callback=_audio_callback)
    _stream.start()


def _close_mic():
    global _stream
    if _stream:
        _stream.stop()
        _stream.close()
        _stream = None


def _transcribe_and_type(chunks):
    audio = np.concatenate(chunks).flatten()

    print("  Transcribiendo...", end="", flush=True)
    segments, _ = model.transcribe(
        audio,
        beam_size=5,
        language=LANGUAGE,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=300),
    )
    text = "".join(s.text for s in segments).strip()

    if not text:
        print(" (nada detectado)")
        _ui.idle()
        return

    print(f" ✅ {text}")
    _ui.idle()

    old_clip = pyperclip.paste()
    pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.1)
    pyperclip.copy(old_clip)


def _hotkey_monitor():
    global _recording, _audio_chunks, _hotkey_down
    while True:
        pressed = _hotkey_active()

        if pressed and not _hotkey_down:
            _hotkey_down = True
            _audio_chunks = []
            _open_mic()
            print("🎙️  Grabando...", end="", flush=True)
            _ui.recording()

        elif not pressed and _hotkey_down:
            _hotkey_down = False
            _close_mic()
            chunks = list(_audio_chunks)
            secs = round(len(chunks) * (POLL_MS / 1000), 1)
            print(f" ({secs}s)")

            if chunks:
                _ui.processing()
                threading.Thread(
                    target=_transcribe_and_type,
                    args=(chunks,),
                    daemon=True,
                ).start()
            else:
                _ui.idle()

        time.sleep(POLL_MS / 1000)


# ─── Arranque ─────────────────────────────────────────────────
def _shutdown(*_):
    _close_mic()
    try:
        _ui.root.destroy()
    except Exception:
        pass
    sys.exit(0)

_ui = Indicator()

hotkey_label = " + ".join(sorted(HOTKEY_KEYS))
print(f"🎤 WispLocal activo  |  Hotkey: [{hotkey_label.upper()}]  |  Modelo: {MODEL_SIZE}")
print("   Mantén pulsado para grabar, suelta para escribir.")
print("   Clic derecho en el indicador para cerrar.\n")

signal.signal(signal.SIGINT, _shutdown)
_ui.root.protocol("WM_DELETE_WINDOW", _shutdown)

# Permite que SIGINT interrumpa el mainloop de tkinter cada 200ms
def _tick():
    _ui.root.after(200, _tick)

_ui.root.after(200, _tick)
threading.Thread(target=_hotkey_monitor, daemon=True).start()
_ui.run()
