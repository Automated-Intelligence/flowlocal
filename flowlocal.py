"""FlowLocal - offline hold-to-talk dictation (a local Wispr Flow replacement).

Hold the hotkey, speak, release. The transcription is pasted into the
focused application. All processing happens locally via faster-whisper.
"""

import contextlib
import json
import os
import queue
import re
import sys
import threading
import time
import winsound
from pathlib import Path

if getattr(sys, "frozen", False):  # running as a PyInstaller bundle
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "flowlocal.log"
HISTORY_PATH = APP_DIR / "history.jsonl"

DEFAULT_CONFIG = {
    "hotkey": "right ctrl",
    "model": "large-v3-turbo",
    "device": "auto",
    "language": "en",
    "input_device": "",
    "sample_rate": 16000,
    "audio_cues": True,
    "min_recording_seconds": 0.3,
    "paste_mode": "clipboard",
    "remove_fillers": True,
    "smart_spacing": True,
    "initial_prompt": "",
    "wake_word_enabled": False,
    "wake_word": "hey flow",
    "wake_input_device": "",
    "wake_silence_seconds": 1.2,
    "rewrite_selection_hotkey": "f10",
    "rewrite_selection_tone": "professional",
    "rewrite_tone": "off",
    "rewrite_custom_prompt": "",
    "rewrite_model": "qwen2.5:7b",
    "ollama_url": "http://localhost:11434",
}


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            log(f"Bad config.json, using defaults: {e}")
    else:
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    return cfg


def append_history(text: str, seconds: float, raw: str = "") -> None:
    rec = {
        "ts": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M"),
        "seconds": round(seconds, 1),
        "text": text,
    }
    if raw and raw != text:
        rec["raw"] = raw
    try:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        log(f"Could not write history: {e}")


def add_cuda_dlls_to_path() -> None:
    """Make the bundled/pip-installed cuBLAS/cuDNN DLLs visible to ctranslate2."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", APP_DIR))
    else:
        base = Path(sys.prefix) / "Lib" / "site-packages"
    for pkg in ("nvidia/cublas/bin", "nvidia/cudnn/bin"):
        p = base / pkg
        if p.is_dir():
            os.add_dll_directory(str(p))
            os.environ["PATH"] = str(p) + os.pathsep + os.environ.get("PATH", "")


class Beeper:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return self.cfg.get("audio_cues", True)

    def _beep(self, freq: int, ms: int) -> None:
        if self.enabled:
            threading.Thread(
                target=winsound.Beep, args=(freq, ms), daemon=True
            ).start()

    def start(self):
        self._beep(880, 90)

    def stop(self):
        self._beep(660, 90)

    def done(self):
        self._beep(1040, 70)

    def error(self):
        self._beep(220, 250)


def find_input_device(sd, query: str):
    """Return the input-device index matching query, or None for the default."""
    query = (query or "").strip().lower()
    if not query:
        return None
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and query in d["name"].lower():
            return i
    raise RuntimeError(f"No microphone matching {query!r} found")


def resample_audio(np, audio, src_rate: int, dst_rate: int):
    if src_rate == dst_rate:
        return audio
    n_out = int(len(audio) * dst_rate / src_rate)
    x_out = np.linspace(0, len(audio) - 1, n_out)
    return np.interp(x_out, np.arange(len(audio)), audio).astype(np.float32)


class Recorder:
    """Captures microphone audio between start() and stop().

    Records at the device's native sample rate and resamples to the target
    rate on stop, since not every driver accepts 16 kHz directly. Device and
    rate are read from config on every start, so settings apply live.
    """

    def __init__(self, cfg: dict):
        import sounddevice as sd

        self.sd = sd
        self.cfg = cfg
        self.target_rate = int(cfg.get("sample_rate", 16000))
        self._capture_rate = self.target_rate
        self._chunks: list = []
        self._stream = None
        self._lock = threading.Lock()

    def start(self) -> None:
        import numpy as np  # noqa: F401  (ensures numpy present before stream)

        with self._lock:
            if self._stream is not None:
                return
            self.target_rate = int(self.cfg.get("sample_rate", 16000))
            device = find_input_device(self.sd, self.cfg.get("input_device", ""))
            info = self.sd.query_devices(
                device if device is not None else self.sd.default.device[0], "input"
            )
            self._capture_rate = int(info["default_samplerate"])
            self._chunks = []
            self._stream = self.sd.InputStream(
                device=device,
                samplerate=self._capture_rate,
                channels=1,
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:
        self._chunks.append(indata.copy())

    def stop(self):
        import numpy as np

        with self._lock:
            if self._stream is None:
                return None
            self._stream.stop()
            self._stream.close()
            self._stream = None
            if not self._chunks:
                return None
            audio = np.concatenate(self._chunks, axis=0).flatten()
            self._chunks = []
            return resample_audio(np, audio, self._capture_rate, self.target_rate)


class WakeListener(threading.Thread):
    """Siri-style wake word mode.

    Continuously transcribes a rolling audio window with a tiny CPU Whisper
    model (the GPU stays free). On hearing the wake phrase it beeps, records
    until silence, and feeds the audio into the normal dictation pipeline.
    """

    WINDOW_SECONDS = 3.0
    CHECK_SECONDS = 1.0
    SPEECH_THRESHOLD = 0.01  # rms/peak level treated as "someone is talking"
    MAX_COMMAND_SECONDS = 45.0
    MAX_WAIT_FOR_SPEECH = 6.0

    def __init__(self, cfg, jobs, tray, beeper, recording_flag, quit_event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.jobs = jobs
        self.tray = tray
        self.beeper = beeper
        self.recording_flag = recording_flag
        self.quit_event = quit_event
        self._tiny = None

    def _enabled(self) -> bool:
        return bool(self.cfg.get("wake_word_enabled")) and bool(
            (self.cfg.get("wake_word") or "").strip()
        )

    def _device_query(self) -> str:
        """Wake mode's own mic, falling back to the dictation mic."""
        return (
            (self.cfg.get("wake_input_device") or "").strip()
            or self.cfg.get("input_device", "")
        )

    def _tiny_model(self):
        if self._tiny is None:
            from faster_whisper import WhisperModel

            name = "tiny.en" if self.cfg.get("language", "en") == "en" else "tiny"
            log(f"Wake word: loading '{name}' model (cpu)...")
            self._tiny = WhisperModel(name, device="cpu", compute_type="int8")
        return self._tiny

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z ]+", "", s.lower()).strip()

    WORD_SIMILARITY = 0.7   # per-word tolerance ("blow" ~ "flow")
    FUSED_SIMILARITY = 0.8  # whole-phrase-as-one-word tolerance ("heyflo")

    def _matches(self, text: str) -> bool:
        """Fuzzy match: a tiny model mishears similar phonemes ('hey blow')
        or fuses the phrase into one token ('heyflow'). Word-aligned matching
        keeps 'the flow' from triggering while 'hey blow' still does."""
        import difflib

        phrase = self._norm(self.cfg.get("wake_word", ""))
        t = self._norm(text)
        if not phrase or not t:
            return False
        if phrase in t:
            return True

        def sim(a: str, b: str) -> float:
            return difflib.SequenceMatcher(None, a, b).ratio()

        words = t.split()
        p_compact = phrase.replace(" ", "")
        if any(sim(w, p_compact) >= self.FUSED_SIMILARITY for w in words):
            return True
        p_words = phrase.split()
        n = len(p_words)
        for i in range(len(words) - n + 1):
            if all(
                sim(words[i + j], p_words[j]) >= self.WORD_SIMILARITY
                for j in range(n)
            ):
                return True
        return False

    def run(self) -> None:
        while not self.quit_event.is_set():
            if not self._enabled():
                time.sleep(1.0)
                continue
            try:
                self._listen()
            except Exception as e:  # noqa: BLE001 - keep the thread alive
                log(f"Wake listener error (retrying in 5s): {e}")
                self.quit_event.wait(5)

    def _pull(self, chunks, rate: int, seconds: float, np):
        """Collect ~seconds of audio from the stream queue."""
        need = int(seconds * rate)
        got, n = [], 0
        deadline = time.time() + seconds * 2
        while n < need and not self.quit_event.is_set():
            timeout = deadline - time.time()
            if timeout <= 0:
                break
            try:
                c = chunks.get(timeout=timeout)
            except queue.Empty:
                break
            got.append(c)
            n += len(c)
        if not got:
            return None
        return np.concatenate(got).flatten()

    def _listen(self) -> None:
        import numpy as np
        import sounddevice as sd

        device_query = self._device_query()
        device = find_input_device(sd, device_query)
        info = sd.query_devices(
            device if device is not None else sd.default.device[0], "input"
        )
        rate = int(info["default_samplerate"])
        target = int(self.cfg.get("sample_rate", 16000))
        chunks: "queue.Queue" = queue.Queue()

        model = self._tiny_model()
        lang = self.cfg.get("language")
        lang = None if lang in (None, "", "auto") else lang
        log(f"Wake word: listening for '{self.cfg['wake_word']}'.")

        with sd.InputStream(
            device=device, samplerate=rate, channels=1, dtype="float32",
            callback=lambda indata, f, t, s: chunks.put(indata.copy()),
        ):
            window = np.zeros(0, dtype=np.float32)
            while (
                not self.quit_event.is_set()
                and self._enabled()
                and self._device_query() == device_query
            ):
                new = self._pull(chunks, rate, self.CHECK_SECONDS, np)
                if new is None:
                    continue
                if self.recording_flag[0]:  # hotkey dictation in progress
                    window = window[:0]
                    continue
                window = np.concatenate([window, new])
                window = window[-int(self.WINDOW_SECONDS * rate):]
                if float(np.abs(window).max()) < self.SPEECH_THRESHOLD:
                    continue  # room is silent; don't burn CPU transcribing
                segments, _ = model.transcribe(
                    resample_audio(np, window, rate, target),
                    language=lang, beam_size=1, condition_on_previous_text=False,
                )
                if self._matches(" ".join(s.text for s in segments)):
                    audio = self._capture_command(chunks, rate, np)
                    if audio is not None:
                        self.jobs.put(resample_audio(np, audio, rate, target))
                    window = window[:0]
        log("Wake word: listener stopped.")

    def _capture_command(self, chunks, rate: int, np):
        """After the wake word: record until trailing silence (or timeouts)."""
        while True:  # drop buffered audio so the wake phrase isn't transcribed
            try:
                chunks.get_nowait()
            except queue.Empty:
                break
        self.beeper.start()
        self.tray.set_state("recording", "FlowLocal: listening (wake word)...")
        frames = []
        started = False
        silence = elapsed = 0.0
        while elapsed < self.MAX_COMMAND_SECONDS and not self.quit_event.is_set():
            chunk = self._pull(chunks, rate, 0.25, np)
            if chunk is None:
                break
            elapsed += len(chunk) / rate
            frames.append(chunk)
            loud = float(np.sqrt(np.mean(chunk ** 2))) > self.SPEECH_THRESHOLD
            if not started:
                if loud:
                    started = True
                elif elapsed > self.MAX_WAIT_FOR_SPEECH:
                    break
            else:
                silence = 0.0 if loud else silence + len(chunk) / rate
                if silence >= float(self.cfg.get("wake_silence_seconds", 1.2)):
                    break
        self.beeper.stop()
        self.tray.set_state("idle")
        if started and frames:
            return np.concatenate(frames)
        return None


class Transcriber:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.model = None
        self.device_used = "?"

    def load(self) -> None:
        from faster_whisper import WhisperModel

        model_name = self.cfg["model"]
        want = self.cfg.get("device", "auto")
        attempts = []
        if want in ("auto", "cuda"):
            attempts.append(("cuda", "float16"))
        if want in ("auto", "cpu"):
            attempts.append(("cpu", "int8"))

        last_err = None
        for device, compute in attempts:
            try:
                log(f"Loading model '{model_name}' on {device} ({compute})...")
                self.model = WhisperModel(
                    model_name, device=device, compute_type=compute
                )
                # Force a tiny inference so CUDA kernel problems surface now,
                # not on the first real dictation.
                import numpy as np

                list(self.model.transcribe(np.zeros(1600, dtype=np.float32))[0])
                self.device_used = device
                log(f"Model ready on {device}.")
                return
            except Exception as e:  # noqa: BLE001 - any backend failure -> fallback
                last_err = e
                log(f"Failed on {device}: {e}")
                self.model = None
        raise RuntimeError(f"Could not load model on any device: {last_err}")

    def transcribe(self, audio) -> str:
        kwargs = {
            "beam_size": 5,
            "vad_filter": True,
            "vad_parameters": {"min_silence_duration_ms": 300},
        }
        lang = self.cfg.get("language") or None
        if lang and lang != "auto":
            kwargs["language"] = lang
        prompt = self.cfg.get("initial_prompt") or None
        if prompt:
            kwargs["initial_prompt"] = prompt
        segments, _info = self.model.transcribe(audio, **kwargs)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text


FILLER_RE = re.compile(
    r"[,;]?\s*\b(?:um+|uh+m*|hm+|mhm+|mm+|erm?|ah+m*)\b[,.!?]*\s*",
    re.IGNORECASE,
)


def remove_fillers(text: str) -> str:
    """Strip filler words (um, uh, hmm, ...) and tidy the punctuation left behind."""
    t = FILLER_RE.sub(" ", text)
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)  # no space before punctuation
    t = re.sub(r"^[\s,.;:]+", "", t)  # no orphaned leading punctuation
    t = re.sub(r"\s{2,}", " ", t).strip()
    # re-capitalize sentence starts exposed by a removed filler
    t = re.sub(r"([.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    return t


TONE_INSTRUCTIONS = {
    "clean": (
        "Fix grammar and punctuation in this dictated text. When the speaker "
        "makes a false start or corrects themselves (e.g. 'Tuesday, no wait, "
        "Wednesday' means Wednesday), keep only the final corrected version. "
        "Otherwise keep the speaker's own wording and tone unchanged."
    ),
    "professional": (
        "Rewrite this dictated text in a clear, professional tone suitable for "
        "workplace communication. Fix false starts and self-corrections."
    ),
    "friendly": (
        "Rewrite this dictated text in a warm, friendly, casual tone. "
        "Fix false starts and self-corrections."
    ),
    "concise": (
        "Rewrite this dictated text to be as concise as possible while keeping "
        "all meaning. Fix false starts and self-corrections."
    ),
}

SELECTION_INSTRUCTIONS = {
    "professional": (
        "Rewrite this text in a clear, professional tone suitable for "
        "workplace communication."
    ),
    "clean": (
        "Fix grammar, spelling, and punctuation in this text. Keep the "
        "author's own wording and tone otherwise unchanged."
    ),
    "friendly": "Rewrite this text in a warm, friendly tone.",
    "concise": (
        "Rewrite this text to be as concise as possible while keeping "
        "all meaning."
    ),
}

SELECTION_MAX_CHARS = 6000


_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20, "thirtieth": 30,
}
_MULTIPLIERS = {"hundred": 100, "thousand": 1000, "million": 1_000_000}
_FACT_RE = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|\b(?:january|february|march|april|may|june|july|august|september"
    r"|october|november|december)\b",
    re.IGNORECASE,
)
_ASSISTANT_STARTS = (
    "sure", "here is", "here's", "certainly", "of course", "okay, here",
    "i cannot", "i can't", "as an ai", "i'm sorry", "great question",
)


def _compose_number(values: list) -> int:
    """['4', 1000, 2, 100] style word runs -> 4200."""
    total = current = 0
    for v in values:
        if v in _MULTIPLIERS.values():
            current = (current or 1) * v
            if v >= 1000:
                total += current
                current = 0
        else:
            current += v
    return total + current


def _fact_tokens(text: str) -> set:
    """Numbers (digit or spelled), weekdays, and months mentioned in text.

    Spelled numbers contribute both their composed value ('three hundred' ->
    300) and their parts (3, 100) so either written form matches.
    """
    tokens = set()
    t = text.lower().replace("-", " ")
    tokens.update(m.lower() for m in _FACT_RE.findall(t))
    for m in re.finditer(r"\d+(?:\.\d+)?", t):
        tokens.add(m.group().lstrip("0") or "0")

    words = re.findall(r"[a-z]+|\d+(?:\.\d+)?", t)
    run = []
    for w in words + ["."]:  # sentinel flushes the last run
        if w in _NUM_WORDS or w in _MULTIPLIERS or w.replace(".", "").isdigit():
            v = (_NUM_WORDS.get(w) if w in _NUM_WORDS
                 else _MULTIPLIERS.get(w) if w in _MULTIPLIERS
                 else float(w) if "." in w else int(w))
            run.append(v)
            tokens.add(str(v))
        else:
            if run:
                tokens.add(str(_compose_number(run)))
                run = []
    return tokens


def validate_rewrite(raw: str, out: str, tone: str):
    """Return None if the rewrite is trustworthy, else a rejection reason.

    One-way checks: a rewrite may DROP content (false starts, fillers) but must
    never INTRODUCE facts or turn into an assistant reply.
    """
    if not out or not out.strip():
        return "empty output"
    ratio = len(out) / max(len(raw), 1)
    low, high = (0.15, 4.0) if tone in ("concise", "custom") else (0.3, 2.2)
    if not (low <= ratio <= high):
        return f"suspicious length ratio {ratio:.2f}"
    if out.strip().lower().startswith(_ASSISTANT_STARTS):
        return "looks like an assistant reply, not a rewrite"
    introduced = _fact_tokens(out) - _fact_tokens(raw)
    if introduced:
        return f"introduced facts not in the original: {sorted(introduced)}"
    return None


class Rewriter:
    """Rewrites transcriptions in a chosen tone via a local Ollama model."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._start_attempted = False

    @property
    def enabled(self) -> bool:
        return self.cfg.get("rewrite_tone", "off") != "off"

    def _system_prompt(self) -> str:
        tone = self.cfg.get("rewrite_tone", "off")
        if tone == "custom":
            instruction = self.cfg.get("rewrite_custom_prompt") or TONE_INSTRUCTIONS["clean"]
        else:
            instruction = TONE_INSTRUCTIONS.get(tone, TONE_INSTRUCTIONS["clean"])
        return (
            "You are a dictation post-processor. "
            f"{instruction} "
            "Preserve the meaning and the language of the text. Never add new "
            "information and never answer questions contained in the text - only "
            "rewrite it. Output ONLY the rewritten text, with no preamble, "
            "explanation, or surrounding quotes."
        )

    def _request(self, payload: dict, timeout: float):
        import urllib.request

        # Thinking models (gemma4, qwen3.x) reason silently for seconds before
        # answering - useless for a rewrite that must land instantly. Non-
        # thinking models simply ignore the flag.
        payload.setdefault("think", False)
        # Cap the context window: Ollama's default (128K for gemma4) reserves
        # several GB of VRAM a rewrite never uses. 8K comfortably covers the
        # 6000-char selection cap plus prompt and output, and keeps the model
        # resident next to Whisper. (Must be identical on every call, or
        # Ollama reloads the model to resize.)
        payload.setdefault("options", {}).setdefault("num_ctx", 8192)
        req = urllib.request.Request(
            self.cfg.get("ollama_url", "http://localhost:11434") + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _try_start_ollama(self) -> bool:
        """Launch the Ollama desktop app (or bare server) if it isn't running."""
        import subprocess

        self._start_attempted = True
        app = (Path(os.environ.get("LOCALAPPDATA", ""))
               / "Programs" / "Ollama" / "ollama app.exe")
        if app.is_file():
            try:
                subprocess.Popen([str(app)], cwd=str(app.parent))
                log("Ollama wasn't running - started the Ollama app.")
                return True
            except OSError as e:
                log(f"Could not start Ollama app: {e}")
        try:
            subprocess.Popen(["ollama", "serve"],
                             creationflags=0x08000000)  # CREATE_NO_WINDOW
            log("Ollama wasn't running - started 'ollama serve'.")
            return True
        except OSError:
            log("Ollama not installed/found; rewrites will use raw text.")
            return False

    def warm_up(self) -> None:
        """Load the model into VRAM in the background so dictation #1 is fast.
        Starts Ollama itself if it isn't running."""
        if not self.enabled:
            return

        def _warm():
            payload = {
                "model": self.cfg["rewrite_model"],
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "keep_alive": "60m",
                "options": {"num_predict": 1},
            }
            deadline = time.time() + 90
            started_ollama = False
            while True:
                try:
                    self._request(payload, timeout=120)
                    log(f"Rewrite model '{self.cfg['rewrite_model']}' warmed up.")
                    return
                except Exception as e:  # noqa: BLE001
                    if not started_ollama:
                        started_ollama = True
                        if not self._try_start_ollama():
                            return
                    elif time.time() > deadline:
                        log(f"Rewrite warm-up failed (Ollama didn't come up): {e}")
                        return
                    time.sleep(2)

        threading.Thread(target=_warm, daemon=True).start()

    def _call(self, text: str, temperature: float, strict: bool) -> str:
        system = self._system_prompt()
        if strict:
            system += (
                " IMPORTANT: your previous attempt was rejected for altering the "
                "content. Be maximally conservative: change as little as possible "
                "and keep every fact, number, and name exactly as dictated."
            )
        data = self._request(
            {
                "model": self.cfg["rewrite_model"],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
                "stream": False,
                "keep_alive": "60m",
                "options": {"temperature": temperature},
            },
            timeout=30,
        )
        return self._clean_output(data["message"]["content"])

    @staticmethod
    def _clean_output(out: str) -> str:
        out = out.strip()
        # strip <think> blocks (some models) and surrounding quotes
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL).strip()
        if len(out) >= 2 and out[0] in "\"'" and out[-1] == out[0]:
            out = out[1:-1].strip()
        return out

    def _selection_prompt(self) -> str:
        tone = self.cfg.get("rewrite_selection_tone", "professional")
        if tone == "custom":
            instruction = (self.cfg.get("rewrite_custom_prompt")
                           or SELECTION_INSTRUCTIONS["professional"])
        else:
            instruction = SELECTION_INSTRUCTIONS.get(
                tone, SELECTION_INSTRUCTIONS["professional"])
        return (
            "You are a writing assistant. "
            f"{instruction} "
            "Preserve the meaning, the language, and the formatting (line "
            "breaks, bullet points). Never add new information and never "
            "answer questions contained in the text - only rewrite it. "
            "Output ONLY the rewritten text, with no preamble, explanation, "
            "or surrounding quotes."
        )

    def rewrite_selection(self, text: str):
        """Rewrite highlighted text; None means no trustworthy rewrite
        (caller should leave the selection untouched)."""
        if not text.strip():
            return None
        if len(text) > SELECTION_MAX_CHARS:
            log(f"Selection too long to rewrite "
                f"({len(text)} > {SELECTION_MAX_CHARS} chars).")
            return None
        tone = self.cfg.get("rewrite_selection_tone", "professional")
        try:
            for attempt, temperature in ((1, 0.2), (2, 0.0)):
                data = self._request(
                    {
                        "model": self.cfg["rewrite_model"],
                        "messages": [
                            {"role": "system", "content": self._selection_prompt()},
                            {"role": "user", "content": text},
                        ],
                        "stream": False,
                        "keep_alive": "60m",
                        "options": {"temperature": temperature},
                    },
                    timeout=60,
                )
                out = self._clean_output(data["message"]["content"])
                reason = validate_rewrite(text, out, tone)
                if reason is None:
                    return out
                log(f"Selection rewrite attempt {attempt} rejected ({reason}).")
            return None
        except Exception as e:  # noqa: BLE001
            log(f"Selection rewrite failed: {e}")
            if not self._start_attempted:
                self.warm_up()  # bring Ollama up for the next attempt
            return None

    def rewrite(self, text: str) -> str:
        """Return a validated rewrite, or the original text if we can't trust one."""
        if not self.enabled or not text:
            return text
        tone = self.cfg.get("rewrite_tone", "off")
        try:
            for attempt, (temperature, strict) in enumerate(
                [(0.2, False), (0.0, True)], start=1
            ):
                out = self._call(text, temperature, strict)
                reason = validate_rewrite(text, out, tone)
                if reason is None:
                    return out
                log(f"Rewrite attempt {attempt} rejected ({reason})"
                    + ("; retrying strictly" if attempt == 1 else "; using raw text"))
            return text
        except Exception as e:  # noqa: BLE001
            log(f"Rewrite failed, using raw transcription: {e}")
            if not self._start_attempted:
                self.warm_up()  # bring Ollama up so the next dictation is polished
            return text


def needs_leading_space(text: str, had_previous: bool, user_acted: bool) -> bool:
    """Consecutive dictations with no typing/clicking in between continue the
    same text, so the new chunk needs a separating space."""
    if not text or not had_previous or user_acted:
        return False
    return text[0] not in ".,!?;:)]}"


def _send_unicode(text: str) -> None:
    """Type text via raw Windows SendInput unicode (VK_PACKET) events.

    keyboard.write() simulates physical key presses, which some apps process
    twice (scancode + translated character) and which depends on the active
    layout. Unicode injection is a single clean path: each character is
    delivered exactly once, in any app, regardless of keyboard layout.
    """
    import ctypes
    from ctypes import wintypes

    INPUT_KEYBOARD = 1
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002
    VK = {"\n": 0x0D, "\t": 0x09}
    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR))

    class MOUSEINPUT(ctypes.Structure):  # sizes the INPUT union correctly
        _fields_ = (("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR))

    class _IUNION(ctypes.Union):
        _fields_ = (("ki", KEYBDINPUT), ("mi", MOUSEINPUT))

    class INPUT(ctypes.Structure):
        _fields_ = (("type", wintypes.DWORD), ("u", _IUNION))

    def make(vk: int, scan: int, flags: int) -> INPUT:
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        return inp

    events = []
    for ch in text:
        if ch == "\r":
            continue
        if ch in VK:  # real Enter/Tab keys; apps ignore their unicode forms
            events.append(make(VK[ch], 0, 0))
            events.append(make(VK[ch], 0, KEYEVENTF_KEYUP))
            continue
        data = ch.encode("utf-16-le")  # surrogate pairs become two units
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], "little")
            events.append(make(0, unit, KEYEVENTF_UNICODE))
            events.append(make(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))

    send_input = ctypes.windll.user32.SendInput
    CHUNK = 64  # small batches so slow apps keep up
    for i in range(0, len(events), CHUNK):
        batch = events[i:i + CHUNK]
        arr = (INPUT * len(batch))(*batch)
        sent = send_input(len(batch), arr, ctypes.sizeof(INPUT))
        if sent != len(batch):
            log(f"SendInput delivered {sent}/{len(batch)} events "
                f"(err {ctypes.get_last_error()})")
        time.sleep(0.01)


def capture_selection():
    """Copy whatever is highlighted in the focused window.

    Returns (selected_text_or_None, previous_clipboard_or_None). The caller
    is responsible for restoring the previous clipboard when done.
    """
    import keyboard
    import pyperclip

    old_clip = None
    try:
        old_clip = pyperclip.paste()
    except Exception:  # noqa: BLE001 - clipboard may hold non-text data
        pass
    try:
        pyperclip.copy("")  # sentinel: stays empty if nothing is selected
        keyboard.send("ctrl+c")
        time.sleep(0.25)
        text = pyperclip.paste()
    except Exception as e:  # noqa: BLE001
        log(f"Selection capture failed: {e}")
        return None, old_clip
    return (text or None), old_clip


def paste_via_clipboard(text: str) -> None:
    """Paste text into the focused window (replaces any selection)."""
    import keyboard
    import pyperclip

    pyperclip.copy(text)
    time.sleep(0.05)
    keyboard.send("ctrl+v")
    time.sleep(0.15)


def type_text(text: str, mode: str) -> None:
    """Insert text into the focused window."""
    import keyboard

    if mode == "type":
        time.sleep(0.15)  # let the hotkey release settle in the target app
        _send_unicode(text)
        return

    # Clipboard paste: fast and unicode-safe. Save and restore the clipboard.
    import pyperclip

    old_clip = None
    try:
        old_clip = pyperclip.paste()
    except Exception:  # noqa: BLE001 - clipboard may hold non-text data
        pass
    pyperclip.copy(text)
    time.sleep(0.05)
    keyboard.send("ctrl+v")
    time.sleep(0.15)
    if old_clip is not None:
        try:
            pyperclip.copy(old_clip)
        except Exception:  # noqa: BLE001
            pass


class TrayIcon:
    """System tray icon reflecting app state (idle / recording / busy)."""

    COLORS = {
        "loading": (128, 128, 128),
        "idle": (70, 130, 240),
        "recording": (230, 60, 60),
        "busy": (240, 180, 40),
        "error": (0, 0, 0),
    }

    def __init__(self, hotkey: str, on_quit, on_open=None):
        self._on_quit = on_quit
        self._on_open = on_open
        self._hotkey = hotkey
        self._icon = None
        self._state = "loading"

    def _image(self, state: str):
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([8, 8, 56, 56], fill=self.COLORS[state] + (255,))
        # microphone glyph
        d.rounded_rectangle([26, 18, 38, 38], radius=6, fill=(255, 255, 255, 255))
        d.line([32, 40, 32, 46], fill=(255, 255, 255, 255), width=3)
        d.line([24, 46, 40, 46], fill=(255, 255, 255, 255), width=3)
        return img

    def run_detached(self) -> None:
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem(f"FlowLocal — hold [{self._hotkey}] to talk", None, enabled=False),
            pystray.MenuItem(
                "Settings & History",
                lambda: self._on_open and self._on_open(),
                default=True,
            ),
            pystray.MenuItem("Quit", lambda: self._quit()),
        )
        self._icon = pystray.Icon(
            "FlowLocal", self._image("loading"), "FlowLocal (loading model...)", menu
        )
        self._icon.run_detached()

    def _quit(self) -> None:
        self.stop()
        self._on_quit()

    def stop(self) -> None:
        try:
            if self._icon:
                self._icon.stop()
                self._icon = None
        except Exception:  # noqa: BLE001 - already stopped
            pass

    def set_state(self, state: str, tip: str = "") -> None:
        self._state = state
        if self._icon:
            self._icon.icon = self._image(state)
            self._icon.title = tip or f"FlowLocal ({state})"


def install_crash_handlers() -> None:
    """Leave evidence if we die: native faults -> crash.log, Python exceptions
    (any thread, incl. Tk callbacks) -> flowlocal.log."""
    import faulthandler
    import traceback

    global _crash_file  # keep the handle alive for faulthandler
    _crash_file = open(APP_DIR / "crash.log", "a", buffering=1, encoding="utf-8")
    _crash_file.write(f"\n--- session {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    faulthandler.enable(_crash_file)

    def _log_exception(prefix, exc_type, exc, tb) -> None:
        log(f"{prefix}: "
            + "".join(traceback.format_exception(exc_type, exc, tb)).strip())

    sys.excepthook = lambda *a: _log_exception("UNHANDLED EXCEPTION", *a)
    threading.excepthook = lambda args: _log_exception(
        f"THREAD CRASH ({args.thread.name})",
        args.exc_type, args.exc_value, args.exc_traceback,
    )


def main() -> None:
    install_crash_handlers()
    cfg = load_config()
    add_cuda_dlls_to_path()

    import keyboard

    beeper = Beeper(cfg)
    recorder = Recorder(cfg)
    transcriber = Transcriber(cfg)
    rewriter = Rewriter(cfg)
    rewriter.warm_up()
    jobs: "queue.Queue" = queue.Queue()
    quit_event = threading.Event()
    open_request = threading.Event()

    tray = TrayIcon(cfg["hotkey"], on_quit=quit_event.set, on_open=open_request.set)
    tray.run_detached()

    try:
        transcriber.load()
    except Exception as e:  # noqa: BLE001
        log(f"FATAL: {e}")
        tray.set_state("error", "FlowLocal: model failed to load (see log)")
        beeper.error()
        quit_event.wait()
        sys.exit(1)

    tray.set_state("idle", f"FlowLocal ready ({transcriber.device_used}) — hold [{cfg['hotkey']}]")

    recording_started_at = [0.0]
    is_recording = [False]

    # --- smart spacing: watch for user activity between dictations ---
    had_previous_paste = [False]
    user_acted = [False]
    injecting = [False]

    hotkey_ignore = set()

    def on_any_key(event) -> None:
        name = (event.name or "").lower()
        if injecting[0]:
            return
        if name in hotkey_ignore:
            return
        user_acted[0] = True

    key_hook = [keyboard.hook(on_any_key)]
    try:
        import mouse

        def on_mouse(event) -> None:
            # clicks and scrolls move the caret context; pure movement doesn't
            if not injecting[0] and isinstance(
                event, (mouse.ButtonEvent, mouse.WheelEvent)
            ):
                user_acted[0] = True

        mouse.hook(on_mouse)
    except Exception as e:  # noqa: BLE001
        log(f"Mouse hook unavailable (smart spacing uses keyboard only): {e}")

    def on_press() -> None:
        if is_recording[0]:  # key auto-repeat while held
            return
        is_recording[0] = True
        try:
            recorder.start()
            recording_started_at[0] = time.time()
            tray.set_state("recording", "FlowLocal: recording...")
            beeper.start()
        except Exception as e:  # noqa: BLE001
            log(f"Mic error: {e}")
            tray.set_state("error", f"FlowLocal: mic error: {e}")
            beeper.error()

    def on_release() -> None:
        if not is_recording[0]:
            return
        is_recording[0] = False
        audio = recorder.stop()
        beeper.stop()
        duration = time.time() - recording_started_at[0]
        if audio is None or duration < cfg["min_recording_seconds"]:
            tray.set_state("idle")
            return
        jobs.put(audio)

    selection_busy = [False]

    def on_rewrite_selection() -> None:
        """Rewrite the highlighted text in place (rewrite-selection hotkey)."""
        if selection_busy[0] or is_recording[0]:
            return
        selection_busy[0] = True
        threading.Thread(target=_rewrite_selection_run, daemon=True).start()

    def _rewrite_selection_run() -> None:
        old_clip = None
        try:
            tray.set_state("busy", "FlowLocal: rewriting selection...")
            injecting[0] = True
            try:
                text, old_clip = capture_selection()
            finally:
                injecting[0] = False
            if not text or not text.strip():
                log("Rewrite key pressed but nothing was selected.")
                beeper.error()
                return
            t0 = time.time()
            new = rewriter.rewrite_selection(text)
            if not new:
                beeper.error()  # guards rejected it; selection left untouched
                return
            injecting[0] = True
            try:
                paste_via_clipboard(new)
            finally:
                injecting[0] = False
            user_acted[0] = True  # the caret moved; next dictation shouldn't
            had_previous_paste[0] = False  # inherit dictation smart-spacing
            append_history(new, 0.0, raw=text)
            log(f"Rewrote selection: {len(text)} -> {len(new)} chars "
                f"in {time.time() - t0:.2f}s: {new[:80]!r}")
            beeper.done()
        except Exception as e:  # noqa: BLE001
            log(f"Selection rewrite error: {e}")
            beeper.error()
        finally:
            if old_clip is not None:
                try:
                    import pyperclip

                    pyperclip.copy(old_clip)
                except Exception:  # noqa: BLE001
                    pass
            selection_busy[0] = False
            tray.set_state("idle")

    def worker() -> None:
        while not quit_event.is_set():
            try:
                audio = jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            tray.set_state("busy", "FlowLocal: transcribing...")
            try:
                t0 = time.time()
                text = transcriber.transcribe(audio)
                if cfg.get("remove_fillers", True):
                    text = remove_fillers(text)
                raw = text
                text = rewriter.rewrite(text)
                dt = time.time() - t0
                if text:
                    audio_secs = len(audio) / cfg["sample_rate"]
                    append_history(text, audio_secs, raw=raw)
                    out = text
                    if cfg.get("smart_spacing", True) and needs_leading_space(
                        out, had_previous_paste[0], user_acted[0]
                    ):
                        out = " " + out
                    mode = cfg.get("paste_mode", "clipboard")
                    injecting[0] = True
                    try:
                        if mode == "type":
                            # detach our keyboard hooks so injected keys aren't
                            # fed back / replayed (which doubles every character)
                            with keyboard_quiet_for_typing():
                                type_text(out, mode)
                        else:
                            type_text(out, mode)
                    finally:
                        injecting[0] = False
                    had_previous_paste[0] = True
                    user_acted[0] = False
                    log(f"{audio_secs:.1f}s audio -> "
                        f"{len(text)} chars in {dt:.2f}s: {text[:80]!r}")
                    beeper.done()
                else:
                    log("No speech detected.")
            except Exception as e:  # noqa: BLE001
                log(f"Transcription error: {e}")
                beeper.error()
            tray.set_state("idle")

    threading.Thread(target=worker, daemon=True).start()

    hotkey_hooks = []
    hotkey_lock = threading.Lock()

    def _unbind_hotkey() -> None:
        for kind, handle in hotkey_hooks:
            try:
                if kind == "hotkey":
                    keyboard.remove_hotkey(handle)
                else:
                    keyboard.unhook(handle)
            except Exception:  # noqa: BLE001 - already removed
                pass
        hotkey_hooks.clear()

    def register_hotkey() -> None:
        """(Re)bind the hold-to-talk hotkey; safe to call live on config change."""
        with hotkey_lock:
            _unbind_hotkey()

            hotkey = cfg["hotkey"]
            hotkey_ignore.clear()
            for part in hotkey.split("+"):
                part = part.strip().lower()
                hotkey_ignore.add(part)
                base = part.replace("left ", "").replace("right ", "")
                hotkey_ignore.update({base, f"left {base}", f"right {base}"})

            if "+" in hotkey:
                # Combo hotkey (e.g. "ctrl+esc"): suppress it so the OS doesn't
                # also act on it, and treat release of the final key as
                # end-of-recording.
                main_key = hotkey.split("+")[-1].strip()
                hotkey_hooks.append(("hotkey", keyboard.add_hotkey(
                    hotkey, on_press, suppress=True, trigger_on_release=False)))
                hotkey_hooks.append(("hook", keyboard.on_release_key(
                    main_key, lambda e: on_release(), suppress=False)))
            else:
                hotkey_hooks.append(("hook", keyboard.on_press_key(
                    hotkey, lambda e: on_press(), suppress=False)))
                hotkey_hooks.append(("hook", keyboard.on_release_key(
                    hotkey, lambda e: on_release(), suppress=False)))

            # rewrite-selection key: suppressed, because bare F-keys like F10
            # have app meanings (menu bar) that would kill the selection.
            rw = (cfg.get("rewrite_selection_hotkey") or "").strip().lower()
            if rw and rw != hotkey:
                try:
                    hotkey_hooks.append(("hotkey", keyboard.add_hotkey(
                        rw, on_rewrite_selection, suppress=True,
                        trigger_on_release=False)))
                except Exception as e:  # noqa: BLE001 - bad key name in config
                    log(f"Could not bind rewrite-selection key {rw!r}: {e}")
            elif rw == hotkey:
                log(f"Rewrite-selection key {rw!r} clashes with the dictation "
                    "hotkey; not binding it.")

    @contextlib.contextmanager
    def keyboard_quiet_for_typing():
        """Remove ALL of our keyboard hooks while we inject 'type'-mode text.

        The keyboard library routes injected keystrokes back through its own
        listener; with a suppress=True hotkey it even replays them, so
        keyboard.write() ends up typing every character twice. Detaching our
        hooks for the duration guarantees a single, clean injection. Restored
        immediately after."""
        with hotkey_lock:
            _unbind_hotkey()
        try:
            keyboard.unhook(key_hook[0])
        except (KeyError, ValueError):
            pass
        try:
            yield
        finally:
            key_hook[0] = keyboard.hook(on_any_key)
            register_hotkey()

    register_hotkey()

    def hook_refresh_loop() -> None:
        """Windows silently removes global hooks it deems slow (heavy load,
        sleep/resume), leaving the app alive but deaf. Rather than probing
        with synthetic keystrokes - which pollutes other keyboard apps - just
        rebuild the hotkey hooks periodically. A fresh hook is always live,
        so a dropped one costs at most one refresh interval."""
        while not quit_event.is_set():
            if quit_event.wait(600):
                return
            if is_recording[0]:
                continue  # never swap hooks mid-dictation
            try:
                register_hotkey()
            except Exception as e:  # noqa: BLE001
                log(f"Hotkey refresh failed: {e}")

    threading.Thread(target=hook_refresh_loop, daemon=True,
                     name="hook-refresh").start()

    wake_listener = WakeListener(cfg, jobs, tray, beeper, is_recording, quit_event)
    wake_listener.start()

    def apply_settings() -> None:
        """Reload config.json and apply everything without restarting."""
        old_model = (cfg.get("model"), cfg.get("device"))
        old_hotkey = cfg.get("hotkey")
        new = load_config()
        cfg.clear()
        cfg.update(new)

        if cfg["hotkey"] != old_hotkey:
            register_hotkey()
            tray.set_state("idle", f"FlowLocal ready — hold [{cfg['hotkey']}]")
        if (cfg.get("model"), cfg.get("device")) != old_model:
            def _reload_model():
                tray.set_state("busy", f"FlowLocal: loading {cfg['model']}...")
                candidate = Transcriber(cfg)
                try:
                    candidate.load()
                    transcriber.model = candidate.model
                    transcriber.device_used = candidate.device_used
                    log(f"Switched to model '{cfg['model']}' on {candidate.device_used}.")
                except Exception as e:  # noqa: BLE001
                    log(f"Model switch failed; keeping the previous model: {e}")
                tray.set_state("idle")

            threading.Thread(target=_reload_model, daemon=True).start()
        rewriter.warm_up()
        log("Settings applied live.")

    log(f"FlowLocal running. Hold [{cfg['hotkey']}] to dictate. Model: {cfg['model']} "
        f"on {transcriber.device_used}.")

    # --- UI event loop (hidden Tk root; window opens from the tray menu) ---
    import tkinter as tk

    from ui import FlowUI

    root = tk.Tk()
    root.withdraw()
    root.report_callback_exception = lambda et, e, tb: log(
        f"Tk callback error: {et.__name__}: {e}")
    flow_ui = FlowUI(root, CONFIG_PATH, HISTORY_PATH, apply_cb=apply_settings)

    def poll() -> None:
        if quit_event.is_set():
            root.destroy()
            return
        try:
            if open_request.is_set():
                open_request.clear()
                flow_ui.show()
        except Exception as e:  # noqa: BLE001 - keep the poll loop alive
            log(f"UI error: {e}")
        root.after(150, poll)

    root.after(150, poll)
    root.mainloop()

    tray.stop()
    try:
        keyboard.unhook_all()
    except Exception:  # noqa: BLE001
        pass
    log("FlowLocal exiting.")


if __name__ == "__main__":
    main()
