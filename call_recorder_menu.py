#!/usr/bin/env python3
"""
call_recorder_menu.py — macOS menubar app for one-click call recording.
Runs in the system menubar. Click to start/stop recording any call.

Install: launchctl load ~/Library/LaunchAgents/com.tomaccove.recorder.menu.plist
"""

import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import rumps

RECORDER   = str(Path(__file__).parent / "call_recorder.py")
PYTHON     = str(Path(__file__).parent / ".venv" / "bin" / "python3")
LOG_PATH   = os.path.expanduser("~/recordings/calls/menu.log")
STATE_FILE = os.path.expanduser("~/recordings/calls/.menu_recording_pid")


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "
")


def is_recording() -> bool:
    """Check if call_recorder is currently running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "call_recorder.py.*start"],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except Exception:
        return False


class CallRecorderMenuApp(rumps.App):
    def __init__(self):
        super().__init__("⏹", quit_button=None)
        self.recording = False
        self.start_time = None
        self._timer_thread = None
        self._stop_thread = None
        self._check_state()

    def _check_state(self):
        """Sync menubar icon with actual recorder state on startup."""
        if is_recording():
            self.recording = True
            self.title = "⏺ Recording"
            self._start_timer()
        else:
            self.recording = False
            self.title = "⏹"

    def _start_timer(self):
        self.start_time = time.time()
        self._timer_thread = threading.Thread(target=self._update_timer, daemon=True)
        self._timer_thread.start()

    def _is_continuity_call_active(self) -> bool:
        """Return True if a Continuity (iPhone) call is active on this Mac."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "CallHistoryPluginHelper"],
                capture_output=True, text=True
            )
            # Also check if FaceTime has an active audio session
            result2 = subprocess.run(
                ["lsof", "-c", "FaceTime", "-a", "-i"],
                capture_output=True, text=True
            )
            return result.returncode == 0
        except Exception:
            return True   # assume active if we can't tell

    def _update_timer(self):
        """Update clock display. Also auto-stop if a Continuity call ends."""
        call_was_active = self._is_continuity_call_active()
        while self.recording:
            elapsed = int(time.time() - self.start_time)
            h, remainder = divmod(elapsed, 3600)
            m, s = divmod(remainder, 60)
            if h:
                self.title = f"⏺ {h}:{m:02d}:{s:02d}"
            else:
                self.title = f"⏺ {m:02d}:{s:02d}"

            # Auto-stop: if a call was active and has now ended, stop recording
            # Only trigger after at least 30s to avoid false positives on startup
            if elapsed > 30 and call_was_active:
                if not self._is_continuity_call_active():
                    log("Continuity call ended — auto-stopping recording")
                    threading.Thread(target=self._stop, daemon=True).start()
                    break

            time.sleep(1)

    @rumps.clicked("Record Call")
    def toggle_recording(self, _):
        if self.recording:
            self._stop()
        else:
            self._start()

    def _start(self):
        # Ask for call title
        win = rumps.Window(
            message="What is this call about?",
            title="Start Recording",
            default_text="",
            ok="Record",
            cancel="Cancel",
            dimensions=(300, 20),
        )
        resp = win.run()
        if not resp.clicked:
            return

        title = resp.text.strip() or f"Call {datetime.now().strftime('%b %d %H:%M')}"
        log(f"Starting recording: {title}")

        # Start recorder as background process
        # Inherit env + ensure /opt/homebrew/bin is in PATH for ffmpeg
        env = {**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")}
        proc = subprocess.Popen(
            [PYTHON, RECORDER, "start", "--title", title, "--engine", "assemblyai-nano"],
            stdout=open(LOG_PATH, "a"),
            stderr=subprocess.STDOUT,
            env=env,
        )
        log(f"Recorder PID: {proc.pid}")

        self.recording = True
        self._start_timer()
        rumps.notification("Recording Started", title, "Click menubar ⏺ to stop", sound=False)

    def _stop(self):
        log("Stopping recording...")
        self.recording = False
        self.title = "⏳"  # processing

        def do_stop():
            try:
                subprocess.run(
                    [PYTHON, RECORDER, "stop"],
                    timeout=300,   # transcription can take a few minutes
                )
                log("Recording stopped and processed")
                rumps.notification("Recording Complete", "Processing done", "Memo saved to Google Drive", sound=False)
            except subprocess.TimeoutExpired:
                log("Stop timed out after 300s — force-killing Otter")
                subprocess.run(["pkill", "-9", "-f", "Otter"],
                               capture_output=True)
                rumps.notification("Recording Complete", "Transcription in progress", "Check Drive in a few minutes", sound=False)
            except Exception as e:
                log(f"Stop error: {e}")
                rumps.notification("Recording Error", str(e), "", sound=False)
            finally:
                self.title = "⏹"

        # Use daemon=False so the thread is not silently killed if the main
        # process exits — the thread needs to finish stop/transcription cleanly.
        t = threading.Thread(target=do_stop, daemon=False)
        t.start()
        # Keep a reference so quit_app can join on it
        self._stop_thread = t

    @rumps.clicked("Quit Recorder")
    def quit_app(self, _):
        if self.recording:
            if rumps.alert("Recording in progress", "Stop recording and quit?", ok="Stop & Quit", cancel="Cancel") == 1:
                self._stop()
                # Wait for stop thread to finish (up to 310s) instead of sleeping 2s
                t = getattr(self, "_stop_thread", None)
                if t is not None:
                    t.join(timeout=310)
        rumps.quit_application()

    def _build_menu(self):
        if self.recording:
            return ["Stop Recording", None, "Quit Recorder"]
        else:
            return ["Record Call", None, "Quit Recorder"]


if __name__ == "__main__":
    os.makedirs(os.path.expanduser("~/recordings/calls"), exist_ok=True)
    log("Menubar app starting")
    CallRecorderMenuApp().run()
