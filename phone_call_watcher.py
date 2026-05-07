#!/usr/bin/env python3
"""
phone_call_watcher.py — unified call watcher for Otter auto-record.

Handles three scenarios:
  1. Phone call on Mac (iPhone Continuity) — auto-start AND stop Otter
  2. Zoom meeting ends early — stop Otter before scheduled end time
  3. Teams meeting ends early — stop Otter before scheduled end time

The calendar scheduler (call_scheduler.py) still owns the START trigger
for Zoom/Teams. This daemon owns the STOP trigger when a meeting ends
early, and owns both START and STOP for phone calls.

Signals used:
  - Phone call:   callservicesd fd count spikes above threshold
  - Zoom meeting: CptHost process present (only spawned during meetings)
  - Teams meeting: Teams process fd count spikes above threshold

Logs to ~/recordings/calls/phone_watcher.log
"""

import subprocess
import time
import logging
from pathlib import Path

LOG_PATH     = Path.home() / "recordings/calls/phone_watcher.log"
OTTER_SCRIPT = Path.home() / "tomac-cove-pipeline/otter_record.sh"
STATE_FILE   = Path.home() / "recordings/calls/.otter_recording"  # touch when recording

POLL_SECS         = 3
CALL_FD_THRESHOLD = 70    # callservicesd idle ~50; active call ~70+
TEAMS_FD_BASELINE = 300   # Teams idle baseline; calibrate after first meeting
TEAMS_FD_DELTA    = 80    # fd increase that signals an active Teams meeting
MIN_CALL_SECS     = 15    # ignore blips shorter than this

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.info


# ── Process helpers ────────────────────────────────────────────────────────────

def fd_count(process_name: str) -> int:
    r = subprocess.run(["pgrep", process_name], capture_output=True, text=True)
    if r.returncode != 0:
        return 0
    pid = r.stdout.strip().split("
")[0]
    lsof = subprocess.run(["/usr/sbin/lsof", "-p", pid], capture_output=True, text=True)
    return lsof.stdout.count("
")


def process_running(name: str) -> bool:
    return subprocess.run(["pgrep", "-x", name],
                          capture_output=True).returncode == 0


def cpthost_running() -> bool:
    """CptHost is Zoom's in-meeting audio/caption process. Only present in meetings."""
    return subprocess.run(["pgrep", "CptHost"],
                          capture_output=True).returncode == 0


# ── Otter control ──────────────────────────────────────────────────────────────

OTTER_SCRIPT_TIMEOUT = 35   # applet has 30s internal timeout; give 5s buffer


def otter_start(reason: str):
    log(f"START Otter — reason: {reason}")
    try:
        r = subprocess.run(["/bin/bash", str(OTTER_SCRIPT), "start"],
                           capture_output=True, text=True,
                           timeout=OTTER_SCRIPT_TIMEOUT)
    except subprocess.TimeoutExpired:
        log("Otter start TIMED OUT — script hung; leaving state unset")
        return
    if r.returncode == 0:
        STATE_FILE.touch()
        log("Otter start OK")
    else:
        log(f"Otter start FAILED (exit={r.returncode}): {r.stderr.strip()}")


def otter_stop(reason: str):
    log(f"STOP Otter — reason: {reason}")
    try:
        r = subprocess.run(["/bin/bash", str(OTTER_SCRIPT), "stop"],
                           capture_output=True, text=True,
                           timeout=OTTER_SCRIPT_TIMEOUT)
    except subprocess.TimeoutExpired:
        log("Otter stop TIMED OUT — force-killing Otter as fallback")
        subprocess.run(["pkill", "-9", "-f", "Otter"],
                       capture_output=True)
        STATE_FILE.unlink(missing_ok=True)
        return
    STATE_FILE.unlink(missing_ok=True)
    if r.returncode == 0:
        log("Otter stop OK")
    else:
        log(f"Otter stop FAILED (exit={r.returncode}): {r.stderr.strip()}")


def otter_recording() -> bool:
    """True if this daemon (or the calendar scheduler) started Otter."""
    return STATE_FILE.exists()


# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    log("phone_call_watcher started")

    # State
    phone_call_active  = False
    zoom_meeting_active  = False
    teams_meeting_active = False
    call_start = None

    # Calibrate Teams baseline on startup
    teams_baseline = fd_count("Teams") or TEAMS_FD_BASELINE
    log(f"Teams fd baseline: {teams_baseline}")

    while True:
        try:
            # ── Phone call detection (callservicesd) ──────────────────────────
            call_fds  = fd_count("callservicesd")
            phone_now = call_fds >= CALL_FD_THRESHOLD

            if phone_now and not phone_call_active:
                # Don't double-start if Zoom/Teams meeting also running
                if not (cpthost_running() or process_running("Microsoft Teams")):
                    otter_start(f"phone call detected (fds={call_fds})")
                    phone_call_active = True
                    call_start = time.time()
                else:
                    log(f"Phone signal (fds={call_fds}) but video call app running — skip")

            elif not phone_now and phone_call_active:
                duration = time.time() - call_start if call_start else 0
                if duration >= MIN_CALL_SECS:
                    otter_stop(f"phone call ended ({int(duration)}s)")
                else:
                    log(f"Phone blip ignored ({int(duration)}s < {MIN_CALL_SECS}s)")
                phone_call_active = False
                call_start = None

            # ── Zoom early-end detection (CptHost) ────────────────────────────
            zoom_now = cpthost_running()

            if zoom_now and not zoom_meeting_active:
                zoom_meeting_active = True
                # Calendar scheduler already started Otter; touch state file
                # so we know to stop when the meeting ends
                STATE_FILE.touch()
                log("Zoom meeting started (CptHost up) — will stop Otter at end")

            elif not zoom_now and zoom_meeting_active:
                zoom_meeting_active = False
                otter_stop("Zoom meeting ended (CptHost gone)")

            # ── Teams early-end detection (fd spike) ─────────────────────────
            teams_fds = fd_count("Teams")
            teams_now = teams_fds >= (teams_baseline + TEAMS_FD_DELTA)

            if teams_now and not teams_meeting_active:
                teams_meeting_active = True
                STATE_FILE.touch()
                log(f"Teams meeting started (fds={teams_fds}) — will stop Otter at end")

            elif not teams_now and teams_meeting_active:
                teams_meeting_active = False
                otter_stop(f"Teams meeting ended (fds={teams_fds})")

        except Exception as e:
            log(f"Error in main loop: {e}")

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
