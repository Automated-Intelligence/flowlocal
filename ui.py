"""Settings & transcription-history window for FlowLocal (tkinter)."""

import json
import tkinter as tk
from tkinter import messagebox, ttk

MODELS = [
    "large-v3-turbo",
    "distil-large-v3",
    "large-v3",
    "medium",
    "medium.en",
    "small",
    "small.en",
    "base.en",
]
LANGUAGES = ["en", "auto", "es", "fr", "de", "pt", "it", "nl", "ja", "zh", "ko", "ru"]
HISTORY_DISPLAY_LIMIT = 500


def _list_ollama_models() -> list:
    import json as _json
    import urllib.request

    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as r:
            data = _json.loads(r.read().decode("utf-8"))
        return sorted(m["name"] for m in data.get("models", []))
    except Exception:  # noqa: BLE001 - Ollama not running
        return ["qwen2.5:7b"]


def _list_microphones() -> list:
    import sounddevice as sd

    names = []
    for d in sd.query_devices():
        if d["max_input_channels"] > 0 and d["name"] not in names:
            names.append(d["name"])
    return names


class FlowUI:
    """Owns the (single) settings/history window. Created against a hidden root."""

    def __init__(self, root, config_path, history_path, apply_cb):
        self.root = root
        self.config_path = config_path
        self.history_path = history_path
        self.apply_cb = apply_cb
        self.win = None
        self._records = []

    def show(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.win.deiconify()
            self.win.lift()
            self._load_history()
            return

        self.win = tk.Toplevel(self.root)
        self.win.title("FlowLocal")
        self.win.geometry("720x520")
        self.win.protocol("WM_DELETE_WINDOW", self.win.withdraw)

        nb = ttk.Notebook(self.win)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_history_tab(nb)
        self._build_settings_tab(nb)

    # ---------------- history ----------------

    def _build_history_tab(self, nb) -> None:
        frame = ttk.Frame(nb)
        nb.add(frame, text="  History  ")

        cols = ("time", "secs", "text")
        tree = self.tree = ttk.Treeview(frame, columns=cols, show="headings")
        tree.heading("time", text="When")
        tree.heading("secs", text="Audio")
        tree.heading("text", text="Transcription")
        tree.column("time", width=140, stretch=False)
        tree.column("secs", width=55, stretch=False, anchor="e")
        tree.column("text", width=460)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)

        preview = self.preview = tk.Text(frame, height=4, wrap="word", state="disabled")

        btns = ttk.Frame(frame)
        ttk.Button(btns, text="Copy selected", command=self._copy_selected).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(btns, text="Refresh", command=self._load_history).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(btns, text="Delete all...", command=self._clear_history).pack(
            side="right"
        )

        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        preview.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        tree.bind("<<TreeviewSelect>>", lambda e: self._show_preview())
        tree.bind("<Double-1>", lambda e: self._copy_selected())
        self._load_history()

    def _load_history(self) -> None:
        self._records = []
        if self.history_path.exists():
            for line in self.history_path.read_text(encoding="utf-8").splitlines():
                try:
                    self._records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        self._records.reverse()  # newest first
        self._records = self._records[:HISTORY_DISPLAY_LIMIT]
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self._records):
            one_line = " ".join(r.get("text", "").split())
            self.tree.insert(
                "", "end", iid=str(i),
                values=(r.get("time", "?"), f"{r.get('seconds', 0):.0f}s", one_line),
            )

    def _selected_text(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self._records[int(sel[0])].get("text", "")

    def _show_preview(self) -> None:
        text = self._selected_text() or ""
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def _copy_selected(self) -> None:
        text = self._selected_text()
        if not text:
            return
        self.win.clipboard_clear()
        self.win.clipboard_append(text)
        self.win.title("FlowLocal — copied!")
        self.win.after(1200, lambda: self.win.winfo_exists() and self.win.title("FlowLocal"))

    def _clear_history(self) -> None:
        if messagebox.askyesno(
            "FlowLocal", "Delete the entire transcription history?", parent=self.win
        ):
            self.history_path.write_text("", encoding="utf-8")
            self._load_history()

    # ---------------- settings ----------------

    def _build_settings_tab(self, nb) -> None:
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        frame = ttk.Frame(nb, padding=12)
        nb.add(frame, text="  Settings  ")

        self.vars = {
            "hotkey": tk.StringVar(value=cfg.get("hotkey", "right ctrl")),
            "input_device": tk.StringVar(value=cfg.get("input_device", "")),
            "model": tk.StringVar(value=cfg.get("model", "large-v3-turbo")),
            "device": tk.StringVar(value=cfg.get("device", "auto")),
            "language": tk.StringVar(value=cfg.get("language", "en")),
            "paste_mode": tk.StringVar(value=cfg.get("paste_mode", "clipboard")),
            "audio_cues": tk.BooleanVar(value=cfg.get("audio_cues", True)),
            "remove_fillers": tk.BooleanVar(value=cfg.get("remove_fillers", True)),
            "smart_spacing": tk.BooleanVar(value=cfg.get("smart_spacing", True)),
            "wake_word_enabled": tk.BooleanVar(value=cfg.get("wake_word_enabled", False)),
            "wake_word": tk.StringVar(value=cfg.get("wake_word", "hey flow")),
            "wake_input_device": tk.StringVar(value=cfg.get("wake_input_device", "")),
            "rewrite_selection_hotkey": tk.StringVar(
                value=cfg.get("rewrite_selection_hotkey", "f10")),
            "rewrite_selection_tone": tk.StringVar(
                value=cfg.get("rewrite_selection_tone", "professional")),
            "initial_prompt": tk.StringVar(value=cfg.get("initial_prompt", "")),
            "min_recording_seconds": tk.StringVar(
                value=str(cfg.get("min_recording_seconds", 0.3))
            ),
            "sample_rate": tk.StringVar(value=str(cfg.get("sample_rate", 16000))),
            "rewrite_tone": tk.StringVar(value=cfg.get("rewrite_tone", "off")),
            "rewrite_custom_prompt": tk.StringVar(
                value=cfg.get("rewrite_custom_prompt", "")
            ),
            "rewrite_model": tk.StringVar(value=cfg.get("rewrite_model", "qwen2.5:7b")),
        }

        def row(r, label, widget, hint=""):
            ttk.Label(frame, text=label).grid(row=r, column=0, sticky="w", pady=4)
            widget.grid(row=r, column=1, sticky="ew", pady=4, padx=(10, 0))
            if hint:
                ttk.Label(frame, text=hint, foreground="#888").grid(
                    row=r, column=2, sticky="w", padx=(10, 0)
                )

        mics = [""] + _list_microphones()
        row(0, "Hotkey", ttk.Entry(frame, textvariable=self.vars["hotkey"]),
            "hold to talk; combos ok, e.g. ctrl+esc")
        row(1, "Microphone", ttk.Combobox(frame, textvariable=self.vars["input_device"],
            values=mics, state="readonly"), "empty = system default")
        row(2, "Model", ttk.Combobox(frame, textvariable=self.vars["model"],
            values=MODELS), "larger = more accurate")
        row(3, "Language", ttk.Combobox(frame, textvariable=self.vars["language"],
            values=LANGUAGES), "auto = detect")
        row(4, "Processing", ttk.Combobox(frame, textvariable=self.vars["device"],
            values=["auto", "cuda", "cpu"], state="readonly"),
            "auto = GPU, falls back to CPU")
        row(5, "Insert mode", ttk.Combobox(frame, textvariable=self.vars["paste_mode"],
            values=["clipboard", "type"], state="readonly"),
            "type = for apps that block paste")
        row(6, "Vocabulary hint", ttk.Entry(frame, textvariable=self.vars["initial_prompt"]),
            "names/jargon you dictate often")
        row(7, "Min recording (s)", ttk.Spinbox(frame, from_=0.0, to=5.0, increment=0.1,
            textvariable=self.vars["min_recording_seconds"], width=8),
            "shorter presses are ignored")
        row(8, "Sample rate (Hz)", ttk.Entry(frame, textvariable=self.vars["sample_rate"],
            width=8), "leave at 16000 (what Whisper expects)")
        row(9, "Rewrite tone", ttk.Combobox(frame, textvariable=self.vars["rewrite_tone"],
            values=["off", "clean", "professional", "friendly", "concise", "custom"],
            state="readonly"), "AI rewrite via local Ollama model")
        row(10, "Custom tone prompt", ttk.Entry(
            frame, textvariable=self.vars["rewrite_custom_prompt"]),
            'used when tone = custom, e.g. "pirate speak"')
        row(11, "Rewrite model", ttk.Combobox(
            frame, textvariable=self.vars["rewrite_model"],
            values=_list_ollama_models()), "any model you have in Ollama")
        row(12, "Wake word", ttk.Entry(frame, textvariable=self.vars["wake_word"]),
            'say this to start dictating, e.g. "hey flow"')
        row(13, "Wake microphone", ttk.Combobox(
            frame, textvariable=self.vars["wake_input_device"], values=mics,
            state="readonly"), "empty = same as dictation mic")
        row(14, "Rewrite-selection key", ttk.Entry(
            frame, textvariable=self.vars["rewrite_selection_hotkey"]),
            "highlight text, press it, get a rewrite")
        row(15, "Rewrite-selection tone", ttk.Combobox(
            frame, textvariable=self.vars["rewrite_selection_tone"],
            values=["professional", "clean", "friendly", "concise", "custom"],
            state="readonly"), "custom uses the custom tone prompt")
        ttk.Checkbutton(frame, text="Wake word mode (always-on listening)",
                        variable=self.vars["wake_word_enabled"]).grid(
            row=16, column=1, sticky="w", pady=4, padx=(10, 0))
        ttk.Checkbutton(frame, text="Audio cues (beeps)",
                        variable=self.vars["audio_cues"]).grid(
            row=17, column=1, sticky="w", pady=4, padx=(10, 0))
        ttk.Checkbutton(frame, text="Remove filler words (um, uh, ...)",
                        variable=self.vars["remove_fillers"]).grid(
            row=18, column=1, sticky="w", pady=4, padx=(10, 0))
        ttk.Checkbutton(
            frame,
            text="Smart spacing (space between back-to-back dictations)",
            variable=self.vars["smart_spacing"]).grid(
            row=19, column=1, sticky="w", pady=4, padx=(10, 0))

        frame.columnconfigure(1, weight=1)
        ttk.Button(frame, text="Save", command=lambda: self._save(silent=False)).grid(
            row=20, column=1, sticky="w", pady=(16, 0), padx=(10, 0))
        ttk.Label(frame,
                  text="Everything applies live — text fields on Enter or when "
                       "you click away. No restarts.",
                  foreground="#888").grid(row=21, column=1, sticky="w", padx=(10, 0))

        # auto-apply discrete controls (checkboxes / readonly dropdowns) on change
        self._autosave_job = None
        for key in ("wake_word_enabled", "audio_cues", "remove_fillers",
                    "smart_spacing", "paste_mode", "device", "rewrite_tone",
                    "rewrite_selection_tone", "wake_input_device", "input_device"):
            self.vars[key].trace_add("write", lambda *a: self._schedule_autosave())

        # text fields (hotkey, wake word, prompts, model, numbers) apply the
        # moment you press Enter or click away - so changing the hotkey alone
        # takes effect without having to touch another control first.
        self._bind_text_autoapply(frame)

    def _bind_text_autoapply(self, parent) -> None:
        for w in parent.winfo_children():
            if isinstance(w, (ttk.Entry, ttk.Spinbox, ttk.Combobox)):
                w.bind("<Return>", lambda e: self._schedule_autosave(), add="+")
                w.bind("<FocusOut>", lambda e: self._schedule_autosave(), add="+")
            self._bind_text_autoapply(w)

    def _schedule_autosave(self) -> None:
        if self._autosave_job is not None:
            self.win.after_cancel(self._autosave_job)
        self._autosave_job = self.win.after(400, self._autosave)

    def _autosave(self) -> None:
        self._autosave_job = None
        self._save(silent=True)

    def _save(self, silent: bool = False) -> None:
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        for key, var in self.vars.items():
            cfg[key] = var.get()
        cfg["hotkey"] = cfg["hotkey"].strip().lower()
        try:
            cfg["min_recording_seconds"] = float(cfg["min_recording_seconds"])
            cfg["sample_rate"] = int(cfg["sample_rate"])
            if not cfg["hotkey"]:
                raise ValueError("hotkey cannot be empty")
            if cfg["sample_rate"] < 8000:
                raise ValueError("sample rate must be at least 8000")
        except ValueError as e:
            # On autosave (silent) a half-typed value is transient - just skip
            # saving until it's valid. On an explicit Save click, tell the user.
            if not silent:
                messagebox.showerror(
                    "FlowLocal", f"Invalid setting: {e}", parent=self.win)
            return
        cfg["wake_word"] = cfg["wake_word"].strip().lower()
        # nothing changed on disk? don't churn (avoids needless re-registration)
        if self.config_path.exists() and \
                json.loads(self.config_path.read_text(encoding="utf-8")) == cfg:
            return
        self.config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        try:
            self.apply_cb()
        except Exception as e:  # noqa: BLE001 - surface it; pythonw has no stderr
            if not silent:
                messagebox.showerror(
                    "FlowLocal", f"Settings saved but applying them failed:\n{e}",
                    parent=self.win,
                )
            return
        self.win.title("FlowLocal — settings applied!")
        self.win.after(1500, lambda: self.win.winfo_exists() and self.win.title("FlowLocal"))
