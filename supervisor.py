"""FlowLocal watchdog: relaunch the app if it dies unexpectedly.

Clean quit (tray menu -> exit code 0) stops the supervisor too. Anything else
(crash, kill, native fault) gets restarted with backoff. Gives up after 5
consecutive fast failures so a broken install doesn't restart forever.
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
LOG_PATH = APP_DIR / "flowlocal.log"
LOCK_PORT = 47821  # single-instance lock; held for the supervisor's lifetime


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [supervisor] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def acquire_lock():
    """Bind the lock port; None means another supervisor is already running."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def kill_orphan_apps() -> None:
    """An app without a live supervisor is an orphan from a dead watchdog:
    replace it with a supervised one so there's exactly one instance."""
    import psutil

    me = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            cmd = " ".join(proc.info["cmdline"] or [])
            if (name.startswith("python") and "flowlocal.py" in cmd
                    and proc.pid != me):
                proc.kill()
                log(f"terminated orphaned app (pid {proc.pid}).")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def main() -> None:
    lock = acquire_lock()
    if lock is None:
        return  # another supervisor already owns the app
    backoff = 5
    fast_failures = 0
    log("supervisor started.")
    kill_orphan_apps()
    while True:
        started = time.time()
        proc = subprocess.Popen([sys.executable, str(APP_DIR / "flowlocal.py")])
        rc = proc.wait()
        ran_for = time.time() - started
        if rc == 0:
            log("app exited cleanly; supervisor stopping.")
            return
        if ran_for > 300:  # it ran fine for a while; treat as a fresh incident
            backoff = 5
            fast_failures = 0
        else:
            fast_failures += 1
            if fast_failures >= 5:
                log(f"app died {fast_failures} times in quick succession "
                    f"(last exit code {rc}); giving up. Check crash.log.")
                return
        log(f"app died unexpectedly (exit code {rc}, ran {ran_for:.0f}s); "
            f"restarting in {backoff}s.")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
