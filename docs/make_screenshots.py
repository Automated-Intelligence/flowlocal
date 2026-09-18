"""Regenerate the README screenshots from a throwaway copy of the UI.

Uses sample history/config only - never the real ones. Run from the repo root:
    venv\\Scripts\\python docs\\make_screenshots.py
"""
import ctypes
import json
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp, physical-pixel capture

from PIL import Image, ImageGrab  # noqa: E402
from ui import FlowUI  # noqa: E402

DOCS = ROOT / "docs"
tmp = Path(tempfile.mkdtemp())

SAMPLE_CONFIG = {
    "hotkey": "right ctrl", "model": "large-v3-turbo", "device": "auto",
    "language": "en", "input_device": "", "sample_rate": 16000,
    "audio_cues": True, "min_recording_seconds": 0.3, "paste_mode": "clipboard",
    "initial_prompt": "", "remove_fillers": True, "smart_spacing": True,
    "rewrite_tone": "clean", "rewrite_custom_prompt": "",
    "rewrite_model": "gemma4:12b", "ollama_url": "http://localhost:11434",
    "rewrite_selection_hotkey": "f10", "rewrite_selection_tone": "professional",
    "wake_word_enabled": False, "wake_word": "hey flow",
    "wake_input_device": "", "wake_silence_seconds": 1.2,
}
SAMPLE_HISTORY = [
    ("2026-09-18 09:12", 6.4, "Can you send me the updated proposal before the call this afternoon?"),
    ("2026-09-18 09:15", 4.1, "Let's push the deadline to Thursday and loop in the design team."),
    ("2026-09-18 09:20", 11.8, "Quick recap of today's standup: the login bug is fixed, the "
                               "onboarding flow ships tomorrow, and we still need a decision on pricing."),
    ("2026-09-18 09:31", 3.2, "Remind me to renew the domain next week."),
    ("2026-09-18 09:47", 8.9, "I have already paid $250 for this service, so please confirm the "
                              "refund timeline in writing."),
]

cfg_path = tmp / "config.json"
cfg_path.write_text(json.dumps(SAMPLE_CONFIG, indent=2))
hist_path = tmp / "history.jsonl"
hist_path.write_text("\n".join(
    json.dumps({"ts": 0, "time": t, "seconds": s, "text": x}) for t, s, x in SAMPLE_HISTORY
) + "\n")

root = tk.Tk()
root.withdraw()
ui = FlowUI(root, cfg_path, hist_path, apply_cb=lambda: None)
ui.show()
win = ui.win
win.attributes("-topmost", True)
win.geometry("+120+120")
win.update()
nb = next(w for w in win.winfo_children() if isinstance(w, ttk.Notebook))

# select a history row so the preview pane shows something
ui.tree.selection_set(ui.tree.get_children()[2])
ui._show_preview()


def grab(name: str) -> None:
    win.update()
    time.sleep(0.4)
    x, y = win.winfo_rootx(), win.winfo_rooty()
    w, h = win.winfo_width(), win.winfo_height()
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    img.save(DOCS / name)
    print(f"saved docs/{name} ({w}x{h})")


nb.select(0)
grab("history.png")
nb.select(1)
grab("settings.png")
win.destroy()
root.destroy()

Image.open(ROOT / "icon.ico").resize((256, 256), Image.LANCZOS).save(DOCS / "icon.png")
print("saved docs/icon.png")
