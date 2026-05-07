#!/usr/bin/env python3
"""
Calendar-Driven Call Recording System
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Event-driven architecture. No polling. Zero CPU overhead between calls.

HOW IT WORKS:
  1. Google Calendar and Outlook push change notifications to a Railway webhook
  2. The webhook receiver parses the event, detects video/dial-in links
  3. If a recordable meeting is found, it writes two launchd plists to your Mac:
       - one fires call_recorder.py start at T-1min
       - one fires call_recorder.py stop  at meeting end time
  4. macOS launchd handles precise timing natively — no daemon needed

COMPONENTS:
  webhook_server.py  ← runs on Railway (this file, bottom section)
  call_scheduler.py  ← runs on your Mac, called by webhook via SSH or ngrok
  launchd plists     ← written to ~/Library/LaunchAgents/ per meeting

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SETUP OVERVIEW:

  Part A — Railway webhook server (receives Google + Outlook pushes)
  Part B — Mac scheduler (writes launchd jobs when notified)
  Part C — One-time calendar subscription registration

  Full instructions in SETUP section below.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FILE 1: webhook_server.py
# Deploy this to Railway. Receives push notifications from Google and Outlook.
# Calls back to your Mac scheduler via a shared secret + Mac's public endpoint.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

WEBHOOK_SERVER = '''
#!/usr/bin/env python3
"""
webhook_server.py — Deploy on Railway
Receives Google Calendar and Microsoft Graph change notifications.
Forwards parsed meeting details to the Mac scheduler endpoint.

Railway env vars needed:
  WEBHOOK_SECRET      shared secret between Railway and your Mac
  MAC_SCHEDULER_URL   your Mac's public URL (ngrok or Cloudflare Tunnel)
  GOOGLE_CHANNEL_TOKEN  token you set when registering Google watch
"""

import hashlib
import hmac
import json
import os
import base64
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify, abort

app = Flask(__name__)

WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET", "")
MAC_SCHEDULER_URL    = os.environ.get("MAC_SCHEDULER_URL", "")
GOOGLE_CHANNEL_TOKEN = os.environ.get("GOOGLE_CHANNEL_TOKEN", "")

# Video/dial-in detection patterns
import re
VIDEO_PATTERNS = [
    r"teams\.microsoft\.com",
    r"zoom\.us",
    r"meet\.google\.com",
    r"webex\.com",
    r"gotomeeting\.com",
    r"whereby\.com",
    r"bluejeans\.com",
    r"\+1[\s\-\.]?\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}",
    r"tel:\+?\d{7,}",
    r"dial[\s\-]in",
    r"conference\s+(id|code|pin)",
    r"passcode",
    r"meeting\s+id",
    r"access\s+code",
]
VIDEO_RE = re.compile("|".join(VIDEO_PATTERNS), re.IGNORECASE)


def has_video_link(text: str) -> bool:
    return bool(VIDEO_RE.search(text or ""))


def forward_to_mac(meeting: dict):
    """Send parsed meeting to Mac scheduler."""
    if not MAC_SCHEDULER_URL:
        print(f"MAC_SCHEDULER_URL not set — cannot forward meeting: {meeting['title']}")
        return
    try:
        payload = json.dumps(meeting)
        sig     = hmac.new(
            WEBHOOK_SECRET.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        r = requests.post(
            f"{MAC_SCHEDULER_URL}/schedule",
            data=payload,
            headers={
                "Content-Type":       "application/json",
                "X-Webhook-Signature": sig,
            },
            timeout=10,
        )
        print(f"Forwarded to Mac: {r.status_code} {meeting['title']}")
    except Exception as e:
        print(f"Failed to forward to Mac: {e}")


# ── Google Calendar push endpoint ──────────────────────────────────────────────
# Google sends a sync notification first, then resource notifications.
# We need to fetch the actual event details using the Calendar API.

@app.route("/google/calendar", methods=["POST"])
def google_calendar_push():
    """
    Receives Google Calendar push notifications.
    Google sends headers (X-Goog-Channel-Token, X-Goog-Resource-State)
    and an empty body for sync, or resource ID for changes.
    We respond 200 immediately (required) then fetch changed events.
    """
    channel_token = request.headers.get("X-Goog-Channel-Token", "")
    resource_state = request.headers.get("X-Goog-Resource-State", "")
    resource_id    = request.headers.get("X-Goog-Resource-ID", "")
    channel_id     = request.headers.get("X-Goog-Channel-ID", "")

    # Validate token
    if GOOGLE_CHANNEL_TOKEN and channel_token != GOOGLE_CHANNEL_TOKEN:
        abort(403)

    # Always respond 200 immediately — Google requires this
    # Do processing asynchronously
    if resource_state == "sync":
        print(f"Google Calendar sync notification received for channel {channel_id}")
        return "", 200

    if resource_state in ("exists", "not_exists"):
        # A calendar resource changed — trigger a fetch of upcoming events
        # We call back to the Mac to do the actual Calendar API fetch,
        # since the Mac has the OAuth token for the user's calendar
        print(f"Google Calendar change: state={resource_state} resource={resource_id}")
        forward_to_mac({
            "action":   "refresh_google",
            "resource": resource_id,
            "channel":  channel_id,
        })

    return "", 200


# ── Microsoft Graph push endpoint ─────────────────────────────────────────────

@app.route("/outlook/calendar", methods=["POST"])
def outlook_calendar_push():
    """
    Receives Microsoft Graph change notifications.
    Validation request: POST with validationToken query param → echo it back.
    Change notification: POST with JSON body containing changed resources.
    """
    # Validation handshake (required when registering subscription)
    validation_token = request.args.get("validationToken")
    if validation_token:
        return validation_token, 200, {"Content-Type": "text/plain"}

    # Parse change notification
    try:
        body = request.get_json(force=True)
    except Exception:
        return "", 400

    for notification in body.get("value", []):
        change_type   = notification.get("changeType", "")
        resource      = notification.get("resource", "")
        client_state  = notification.get("clientState", "")

        # Validate client state (shared secret you set when subscribing)
        if WEBHOOK_SECRET and client_state != WEBHOOK_SECRET:
            continue

        print(f"Outlook change: {change_type} {resource}")

        if change_type in ("created", "updated"):
            # Forward to Mac to fetch full event details and schedule
            forward_to_mac({
                "action":   "refresh_outlook",
                "resource": resource,
                "change":   change_type,
            })

    return "", 202


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
'''


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FILE 2: call_scheduler.py
# Runs on your Mac as a lightweight HTTP server (port 8765).
# Receives forwarded meeting details from Railway.
# Writes launchd plists to ~/Library/LaunchAgents/ for precise start/stop.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SCHEDULER_SERVER = '''
#!/usr/bin/env python3
"""
call_scheduler.py — Runs on your Mac
Receives meeting notifications from Railway webhook server.
Writes launchd jobs for precise start/stop timing.
Fetches full event details from Google/Outlook APIs when notified of changes.

Commands:
  python call_scheduler.py start     # start local scheduler server
  python call_scheduler.py stop      # stop it
  python call_scheduler.py list      # show scheduled launchd jobs
  python call_scheduler.py register  # one-time: register calendar subscriptions
  python call_scheduler.py run       # foreground (debugging)
"""

import hashlib
import hmac
import json
import os
import pickle
import plistlib
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────

WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "")
RAILWAY_URL       = os.environ.get("RAILWAY_URL", "")        # your Railway app URL

# Google OAuth (reuse existing credentials)
GCAL_CREDS_PATH   = os.path.expanduser("~/credentials/gdrive_credentials.json")
GCAL_TOKEN_PATH   = os.path.expanduser("~/credentials/gdrive_token.pickle")
GCAL_WATCH_PATH   = os.path.expanduser("~/credentials/gcal_watch_channels.json")

# Microsoft
MS_CLIENT_ID      = os.environ.get("MICROSOFT_CLIENT_ID", "")
MS_CLIENT_SECRET  = os.environ.get("MICROSOFT_CLIENT_SECRET", "")
MS_TENANT_ID      = os.environ.get("MICROSOFT_TENANT_ID", "common")
MS_TOKEN_PATH     = os.path.expanduser("~/credentials/ms_token.json")
MS_SUB_PATH       = os.path.expanduser("~/credentials/ms_graph_subscription.json")

# Paths
RECORDER_SCRIPT   = os.path.expanduser("~/tomac-cove-pipeline/call_recorder.py")
LAUNCHAGENTS_DIR  = os.path.expanduser("~/Library/LaunchAgents")
SCHEDULER_PID     = os.path.expanduser("~/recordings/calls/.scheduler.pid")
SCHEDULER_LOG     = os.path.expanduser("~/recordings/calls/scheduler.log")
SCHEDULED_PATH    = os.path.expanduser("~/recordings/calls/.scheduled_meetings.json")

# Local server port
PORT              = 8765
PRE_START_SECS    = 60    # start recording 60s before meeting

# Video/dial-in detection
VIDEO_PATTERNS = [
    r"teams\.microsoft\.com", r"zoom\.us", r"meet\.google\.com",
    r"webex\.com", r"gotomeeting\.com", r"whereby\.com", r"bluejeans\.com",
    r"\+1[\s\-\.]?\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}",
    r"tel:\+?\d{7,}", r"dial[\s\-]in", r"conference\s+(id|code|pin)",
    r"passcode", r"meeting\s+id", r"access\s+code",
]
VIDEO_RE = re.compile("|".join(VIDEO_PATTERNS), re.IGNORECASE)

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(SCHEDULER_LOG), exist_ok=True)
    with open(SCHEDULER_LOG, "a") as f:
        f.write(line + "\n")

# ── Persistence ───────────────────────────────────────────────────────────────

def load_json(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

# ── Google Calendar API ───────────────────────────────────────────────────────

GCAL_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/calendar.readonly",
]

def get_gcal_service():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(GCAL_TOKEN_PATH):
        with open(GCAL_TOKEN_PATH, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow  = InstalledAppFlow.from_client_secrets_file(GCAL_CREDS_PATH, GCAL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GCAL_TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)
    return build("calendar", "v3", credentials=creds)


def fetch_gcal_upcoming(hours_ahead=48) -> list:
    """Fetch upcoming events from all Google Calendars."""
    try:
        svc      = get_gcal_service()
        now      = datetime.now(timezone.utc)
        time_min = now.isoformat()
        time_max = (now + timedelta(hours=hours_ahead)).isoformat()
        events   = []
        cal_list = svc.calendarList().list().execute()
        for cal in cal_list.get("items", []):
            result = svc.events().list(
                calendarId=cal["id"],
                timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy="startTime",
            ).execute()
            for ev in result.get("items", []):
                normalized = _normalize_gcal(ev)
                if normalized:
                    events.append(normalized)
        return events
    except Exception as e:
        log(f"⚠️  Google Calendar fetch error: {e}")
        return []


def _normalize_gcal(ev: dict) -> dict | None:
    start_raw = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
    end_raw   = ev.get("end",   {}).get("dateTime") or ev.get("end",   {}).get("date", "")
    start = _parse_dt(start_raw)
    end   = _parse_dt(end_raw)
    if not start or not end:
        return None

    # Skip all-day events (no time component)
    if "T" not in str(start_raw):
        return None

    searchable = " ".join(filter(None, [
        ev.get("summary", ""),
        ev.get("description", ""),
        ev.get("location", ""),
        ev.get("hangoutLink", ""),
        str(ev.get("conferenceData", {})),
    ]))

    if not VIDEO_RE.search(searchable):
        # Also check conferenceData entryPoints directly
        for ep in ev.get("conferenceData", {}).get("entryPoints", []):
            if ep.get("entryPointType") in ("video", "phone"):
                searchable += " has_video"
                break

    return {
        "id":     f"gcal_{ev.get('id', '')}",
        "source": "google",
        "title":  ev.get("summary", "Untitled"),
        "start":  start.isoformat(),
        "end":    end.isoformat(),
        "has_video": bool(VIDEO_RE.search(searchable)),
    }


# ── Microsoft Graph ───────────────────────────────────────────────────────────

def get_ms_token() -> str | None:
    if not MS_CLIENT_ID:
        return None
    token_data = load_json(MS_TOKEN_PATH)
    if token_data.get("access_token"):
        if time.time() < token_data.get("expires_at", 0) - 60:
            return token_data["access_token"]
        if token_data.get("refresh_token"):
            return _refresh_ms_token(token_data["refresh_token"])
    return _ms_device_code_flow()

def _refresh_ms_token(refresh_token: str) -> str | None:
    url  = f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": MS_CLIENT_ID, "client_secret": MS_CLIENT_SECRET,
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "scope": "Calendars.Read offline_access",
    }
    try:
        r = requests.post(url, data=data, timeout=30)
        r.raise_for_status()
        t = r.json()
        t["expires_at"] = time.time() + t.get("expires_in", 3600)
        save_json(MS_TOKEN_PATH, t)
        return t["access_token"]
    except Exception as e:
        log(f"⚠️  MS token refresh failed: {e}")
        return None

def _ms_device_code_flow() -> str | None:
    if not MS_CLIENT_ID:
        return None
    url   = f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/devicecode"
    data  = {"client_id": MS_CLIENT_ID, "scope": "Calendars.Read offline_access"}
    try:
        r  = requests.post(url, data=data, timeout=30)
        r.raise_for_status()
        dc = r.json()
        print(f"\n🔑  Microsoft auth required (one-time):")
        print(f"    Go to: {dc['verification_uri']}")
        print(f"    Code:  {dc['user_code']}\n")
        token_url = f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token"
        expires   = time.time() + dc.get("expires_in", 900)
        interval  = dc.get("interval", 5)
        while time.time() < expires:
            time.sleep(interval)
            pr = requests.post(token_url, data={
                "client_id": MS_CLIENT_ID, "client_secret": MS_CLIENT_SECRET,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": dc["device_code"],
            }, timeout=30)
            if pr.status_code == 200:
                t = pr.json()
                t["expires_at"] = time.time() + t.get("expires_in", 3600)
                save_json(MS_TOKEN_PATH, t)
                print("    ✅  Microsoft auth complete.")
                return t["access_token"]
            err = pr.json().get("error", "")
            if err not in ("authorization_pending", "slow_down"):
                break
    except Exception as e:
        log(f"⚠️  MS device code failed: {e}")
    return None

def fetch_outlook_upcoming(hours_ahead=48) -> list:
    token = get_ms_token()
    if not token:
        return []
    now      = datetime.now(timezone.utc)
    time_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + timedelta(hours=hours_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers  = {"Authorization": f"Bearer {token}"}
    params   = {
        "startDateTime": time_min, "endDateTime": time_max,
        "$top": 50, "$orderby": "start/dateTime",
        "$select": "id,subject,start,end,body,location,onlineMeeting,onlineMeetingUrl",
    }
    events = []
    try:
        r = requests.get("https://graph.microsoft.com/v1.0/me/calendarView",
                          headers=headers, params=params, timeout=30)
        r.raise_for_status()
        for ev in r.json().get("value", []):
            normalized = _normalize_outlook(ev)
            if normalized:
                events.append(normalized)
    except Exception as e:
        log(f"⚠️  Outlook fetch error: {e}")
    return events

def _normalize_outlook(ev: dict) -> dict | None:
    start = _parse_dt(ev.get("start", {}).get("dateTime", ""))
    end   = _parse_dt(ev.get("end",   {}).get("dateTime", ""))
    if not start or not end:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    searchable = " ".join(filter(None, [
        ev.get("subject", ""),
        ev.get("body", {}).get("content", ""),
        ev.get("location", {}).get("displayName", ""),
        ev.get("onlineMeetingUrl", ""),
        str(ev.get("onlineMeeting", {})),
    ]))
    has_video = (
        bool(VIDEO_RE.search(searchable))
        or bool(ev.get("onlineMeetingUrl"))
        or bool(ev.get("onlineMeeting"))
    )
    return {
        "id":        f"outlook_{ev.get('id', '')}",
        "source":    "outlook",
        "title":     ev.get("subject", "Untitled"),
        "start":     start.isoformat(),
        "end":       end.isoformat(),
        "has_video": has_video,
    }

# ── launchd plist writer ──────────────────────────────────────────────────────

def _safe_label(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9.]", ".", s)[:80]

def schedule_meeting(meeting: dict):
    """Write launchd plists to start and stop recording at exact times."""
    meeting_id = meeting["id"]
    title      = meeting["title"]
    start      = _parse_dt(meeting["start"])
    end        = _parse_dt(meeting["end"])

    if not start or not end:
        log(f"⚠️  Cannot schedule '{title}' — missing start/end")
        return

    now = datetime.now(timezone.utc)
    if end < now:
        log(f"  Skipping past meeting: '{title}'")
        return

    # Start recording PRE_START_SECS before meeting
    start_at = start - timedelta(seconds=PRE_START_SECS)
    if start_at < now:
        start_at = now + timedelta(seconds=5)   # already past — start ASAP

    safe_id    = _safe_label(meeting_id)
    start_label = f"com.tomaccove.recorder.start.{safe_id}"
    stop_label  = f"com.tomaccove.recorder.stop.{safe_id}"

    # Write START plist
    _write_launchd_plist(
        label=start_label,
        program=sys.executable,
        args=[RECORDER_SCRIPT, "start", "--title", title, "--engine", "assemblyai-nano"],
        run_at=start_at,
        log_path=os.path.expanduser(f"~/recordings/calls/{safe_id}_start.log"),
    )

    # Write STOP plist
    _write_launchd_plist(
        label=stop_label,
        program=sys.executable,
        args=[RECORDER_SCRIPT, "stop"],
        run_at=end,
        log_path=os.path.expanduser(f"~/recordings/calls/{safe_id}_stop.log"),
    )

    log(f"✅  Scheduled: '{title}'")
    log(f"    Start recording: {start_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    log(f"    Stop recording:  {end.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # Track in schedule file
    scheduled = load_json(SCHEDULED_PATH)
    scheduled[meeting_id] = {
        "title":       title,
        "source":      meeting.get("source", ""),
        "start":       meeting["start"],
        "end":         meeting["end"],
        "start_label": start_label,
        "stop_label":  stop_label,
        "scheduled_at": datetime.now().isoformat(),
    }
    save_json(SCHEDULED_PATH, scheduled)


def _write_launchd_plist(label: str, program: str, args: list,
                          run_at: datetime, log_path: str):
    """Write a launchd plist and load it via launchctl."""
    # Convert run_at to local time components for StartCalendarInterval
    local_dt = run_at.astimezone()
    cal_interval = {
        "Year":   local_dt.year,
        "Month":  local_dt.month,
        "Day":    local_dt.day,
        "Hour":   local_dt.hour,
        "Minute": local_dt.minute,
    }

    plist = {
        "Label":                label,
        "ProgramArguments":     [program] + args,
        "StartCalendarInterval": cal_interval,
        "StandardOutPath":      log_path,
        "StandardErrorPath":    log_path,
        "RunAtLoad":            False,
    }

    plist_path = os.path.join(LAUNCHAGENTS_DIR, f"{label}.plist")
    os.makedirs(LAUNCHAGENTS_DIR, exist_ok=True)

    with open(plist_path, "wb") as f:
        plistlib.dump(plist, f)

    # Unload if already loaded, then load
    subprocess.run(["launchctl", "unload", plist_path],
                   capture_output=True)
    result = subprocess.run(["launchctl", "load",   plist_path],
                            capture_output=True)
    if result.returncode != 0:
        log(f"⚠️  launchctl load failed for {label}: {result.stderr.decode()}")


def cancel_meeting(meeting_id: str):
    """Remove launchd plists for a cancelled/updated meeting."""
    scheduled = load_json(SCHEDULED_PATH)
    if meeting_id not in scheduled:
        return
    entry = scheduled[meeting_id]
    for key in ("start_label", "stop_label"):
        label      = entry.get(key, "")
        plist_path = os.path.join(LAUNCHAGENTS_DIR, f"{label}.plist")
        if os.path.exists(plist_path):
            subprocess.run(["launchctl", "unload", plist_path], capture_output=True)
            os.remove(plist_path)
            log(f"  Removed launchd job: {label}")
    del scheduled[meeting_id]
    save_json(SCHEDULED_PATH, scheduled)
    log(f"  Cancelled: {entry.get('title', meeting_id)}")


# ── Calendar subscription registration ───────────────────────────────────────

def register_google_watch():
    """Register a Google Calendar push channel for all calendars."""
    if not RAILWAY_URL:
        print("❌  RAILWAY_URL not set — cannot register Google watch")
        return
    import uuid
    svc      = get_gcal_service()
    channels = load_json(GCAL_WATCH_PATH)
    cal_list = svc.calendarList().list().execute()
    channel_token = os.environ.get("GOOGLE_CHANNEL_TOKEN",
                                    "tomac-cove-recorder-" + str(uuid.uuid4())[:8])

    for cal in cal_list.get("items", []):
        cal_id     = cal["id"]
        channel_id = f"recorder-{cal_id[:20]}-{int(time.time())}"
        body = {
            "id":      channel_id,
            "type":    "web_hook",
            "address": f"{RAILWAY_URL}/google/calendar",
            "token":   channel_token,
            "expiration": str(int((time.time() + 7 * 24 * 3600) * 1000)),  # 7 days, ms
        }
        try:
            r = svc.events().watch(calendarId=cal_id, body=body).execute()
            channels[cal_id] = {
                "channel_id":    r.get("id"),
                "resource_id":   r.get("resourceId"),
                "expiration":    r.get("expiration"),
                "channel_token": channel_token,
            }
            print(f"✅  Google watch registered: {cal.get('summary', cal_id)}")
            print(f"    Expires: {r.get('expiration')}")
        except Exception as e:
            print(f"⚠️  Failed to register watch for {cal_id}: {e}")

    save_json(GCAL_WATCH_PATH, channels)
    print(f"\n    GOOGLE_CHANNEL_TOKEN={channel_token}")
    print(f"    Set this in Railway env vars.")


def register_outlook_subscription():
    """Register a Microsoft Graph change notification subscription."""
    if not RAILWAY_URL:
        print("❌  RAILWAY_URL not set — cannot register Outlook subscription")
        return
    token = get_ms_token()
    if not token:
        print("❌  Could not get MS token")
        return

    expiry = (datetime.now(timezone.utc) + timedelta(days=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    payload = {
        "changeType":         "created,updated,deleted",
        "notificationUrl":    f"{RAILWAY_URL}/outlook/calendar",
        "resource":           "me/events",
        "expirationDateTime": expiry,
        "clientState":        WEBHOOK_SECRET,
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = requests.post(
            "https://graph.microsoft.com/v1.0/subscriptions",
            headers=headers, json=payload, timeout=30,
        )
        r.raise_for_status()
        sub = r.json()
        save_json(MS_SUB_PATH, sub)
        print(f"✅  Outlook subscription registered: {sub.get('id')}")
        print(f"    Expires: {sub.get('expirationDateTime')}")
        print(f"    ⚠️  Outlook subscriptions expire in 2 days — run register again to renew")
    except Exception as e:
        print(f"❌  Outlook subscription failed: {e}")
        if hasattr(e, "response"):
            print(f"    {e.response.text}")


# ── HTTP request handler ──────────────────────────────────────────────────────

class SchedulerHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        log(f"HTTP {args[1]} {self.path}")

    def _verify_signature(self, body: bytes) -> bool:
        if not WEBHOOK_SECRET:
            return True
        sig      = self.headers.get("X-Webhook-Signature", "")
        expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.read(length) if length else b""

        if not self._verify_signature(body):
            self.send_response(403)
            self.end_headers()
            return

        try:
            data = json.loads(body)
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        if self.path == "/schedule":
            action = data.get("action", "")

            if action == "refresh_google":
                log("  Google Calendar change — refreshing upcoming events")
                threading.Thread(target=self._refresh_and_schedule_google,
                                  daemon=True).start()

            elif action == "refresh_outlook":
                log("  Outlook change — refreshing upcoming events")
                threading.Thread(target=self._refresh_and_schedule_outlook,
                                  daemon=True).start()

            elif data.get("id"):
                # Direct meeting object
                if data.get("has_video"):
                    threading.Thread(target=schedule_meeting, args=(data,),
                                      daemon=True).start()

        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            resp = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp)
        elif self.path == "/list":
            scheduled = load_json(SCHEDULED_PATH)
            resp      = json.dumps(scheduled, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_response(404)
            self.end_headers()

    def _refresh_and_schedule_google(self):
        events = fetch_gcal_upcoming(hours_ahead=48)
        scheduled = load_json(SCHEDULED_PATH)
        for ev in events:
            if ev["has_video"] and ev["id"] not in scheduled:
                schedule_meeting(ev)

    def _refresh_and_schedule_outlook(self):
        events = fetch_outlook_upcoming(hours_ahead=48)
        scheduled = load_json(SCHEDULED_PATH)
        for ev in events:
            if ev["has_video"] and ev["id"] not in scheduled:
                schedule_meeting(ev)


# ── Server process management ─────────────────────────────────────────────────

def cmd_start(args):
    if os.path.exists(SCHEDULER_PID):
        pid = open(SCHEDULER_PID).read().strip()
        try:
            os.kill(int(pid), 0)
            print(f"⚠️  Scheduler already running (PID {pid})")
            return
        except ProcessLookupError:
            pass

    proc = subprocess.Popen(
        [sys.executable, __file__, "run"],
        stdout=open(SCHEDULER_LOG, "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    os.makedirs(os.path.dirname(SCHEDULER_PID), exist_ok=True)
    with open(SCHEDULER_PID, "w") as f:
        f.write(str(proc.pid))
    print(f"✅  Scheduler started (PID {proc.pid})")
    print(f"    Listening on port {PORT}")
    print(f"    Log: {SCHEDULER_LOG}")

def cmd_stop(args):
    if not os.path.exists(SCHEDULER_PID):
        print("⚠️  No PID file found.")
        return
    pid = int(open(SCHEDULER_PID).read().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        os.remove(SCHEDULER_PID)
        print(f"✅  Scheduler stopped (PID {pid})")
    except ProcessLookupError:
        os.remove(SCHEDULER_PID)
        print("⚠️  Was not running.")

def cmd_list(args):
    scheduled = load_json(SCHEDULED_PATH)
    if not scheduled:
        print("No meetings scheduled.")
        return
    print(f"\nScheduled meetings ({len(scheduled)}):")
    for mid, s in sorted(scheduled.items(), key=lambda x: x[1].get("start", "")):
        print(f"  [{s['source'][:1].upper()}] {s['start'][:16]}  {s['title'][:60]}")

def cmd_register(args):
    print("Registering calendar subscriptions...\n")
    print("── Google Calendar ──")
    register_google_watch()
    print("\n── Outlook/Microsoft 365 ──")
    register_outlook_subscription()
    print("\n✅  Done. Subscriptions are active.")
    print("    ⚠️  Google watches expire in 7 days — re-run register weekly or set up auto-renewal.")
    print("    ⚠️  Outlook subscriptions expire in 2 days — same.")

def cmd_run(args):
    log(f"Scheduler listening on port {PORT}")
    # Do an initial fetch of upcoming events on startup
    log("Initial calendar fetch...")
    threading.Thread(target=lambda: [
        schedule_meeting(ev)
        for ev in fetch_gcal_upcoming(48) + fetch_outlook_upcoming(48)
        if ev.get("has_video") and ev["id"] not in load_json(SCHEDULED_PATH)
    ], daemon=True).start()

    server = HTTPServer(("127.0.0.1", PORT), SchedulerHandler)
    def shutdown(sig, frame):
        log("Scheduler shutting down.")
        server.shutdown()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()


def _parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:len(fmt)], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


import argparse, hashlib, hmac
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Call recording scheduler")
    sub    = parser.add_subparsers(dest="command")
    sub.add_parser("start",    help="Start scheduler in background")
    sub.add_parser("stop",     help="Stop scheduler")
    sub.add_parser("list",     help="List scheduled meetings")
    sub.add_parser("register", help="Register calendar subscriptions (one-time)")
    sub.add_parser("run",      help="Run in foreground")
    args = parser.parse_args()
    if args.command == "start":    cmd_start(args)
    elif args.command == "stop":   cmd_stop(args)
    elif args.command == "list":   cmd_list(args)
    elif args.command == "register": cmd_register(args)
    elif args.command == "run":    cmd_run(args)
    else: parser.print_help()
'''


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FILE 3: requirements_webhook.txt  (for Railway)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REQUIREMENTS_WEBHOOK = """flask>=3.0
requests>=2.31
gunicorn>=21.0
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FILE 4: Procfile  (for Railway)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PROCFILE = "web: gunicorn webhook_server:app --bind 0.0.0.0:$PORT"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SETUP GUIDE (printed when run directly)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SETUP_GUIDE = """
SETUP GUIDE — Calendar-Driven Call Recording
═════════════════════════════════════════════

PART A — Deploy webhook server to Railway (receives calendar pushes)
──────────────────────────────────────────────────────────────────────
1. Create ~/call-webhook/ with these files:
     webhook_server.py    (extracted from this file below)
     requirements.txt     (flask, requests, gunicorn)
     Procfile             (web: gunicorn webhook_server:app --bind 0.0.0.0:$PORT)

2. Deploy to Railway:
     cd ~/call-webhook
     railway login
     railway init
     railway up

3. Set Railway env vars:
     WEBHOOK_SECRET=<generate a random string, e.g. openssl rand -hex 32>
     GOOGLE_CHANNEL_TOKEN=<set after running register below>
     MAC_SCHEDULER_URL=<your ngrok or Cloudflare Tunnel URL — see Part B>

PART B — Expose your Mac scheduler to the internet
──────────────────────────────────────────────────────────────────────
Railway needs to reach your Mac to forward calendar changes.
Option 1 (free): ngrok
     brew install ngrok
     ngrok http 8765
     → copy the https URL (e.g. https://abc123.ngrok.io)
     → set MAC_SCHEDULER_URL=https://abc123.ngrok.io in Railway

Option 2 (persistent, recommended): Cloudflare Tunnel
     brew install cloudflared
     cloudflared tunnel login
     cloudflared tunnel create call-scheduler
     cloudflared tunnel route dns call-scheduler scheduler.yourdomain.com
     cloudflared tunnel run call-scheduler
     → set MAC_SCHEDULER_URL=https://scheduler.yourdomain.com in Railway
     → add cloudflared to Mac login items to persist across reboots

PART C — Install and start Mac scheduler
──────────────────────────────────────────────────────────────────────
1. Extract call_scheduler.py from this file (or Claude Code does this)

2. Install deps:
     pip install flask requests google-auth google-auth-oauthlib \
         google-api-python-client

3. Set env vars in ~/.zshrc:
     export WEBHOOK_SECRET="<same value as Railway>"
     export RAILWAY_URL="<your Railway app URL>"
     export MICROSOFT_CLIENT_ID="..."
     export MICROSOFT_CLIENT_SECRET="..."
     export MICROSOFT_TENANT_ID="common"

4. Start scheduler:
     python call_scheduler.py start

5. Add to Mac login items (so it starts on reboot):
     Claude Code can write a launchd plist for this automatically

PART D — Register calendar subscriptions (one-time, then weekly renewal)
──────────────────────────────────────────────────────────────────────
     python call_scheduler.py register

   This registers:
   - Google Calendar push watches (expire every 7 days — renew weekly)
   - Microsoft Graph subscriptions (expire every 2 days — renew every 2 days)

   To auto-renew, Claude Code can schedule a weekly task:
     python call_scheduler.py register

PART E — Test it
──────────────────────────────────────────────────────────────────────
     python call_scheduler.py list      # see what's scheduled
     curl http://localhost:8765/health  # check scheduler is running
     curl http://localhost:8765/list    # JSON of scheduled meetings

SUBSCRIPTION RENEWAL SCHEDULE:
  Google Calendar watches: expire 7 days → schedule weekly renewal
  Outlook subscriptions:   expire 2 days → schedule every 2 days
  Both renew automatically when you run: python call_scheduler.py register
═════════════════════════════════════════════
"""


# ── File extractor (Claude Code uses this to write the actual files) ──────────

if __name__ == "__main__":
    import argparse, os

    parser = argparse.ArgumentParser(description="Extract component files")
    parser.add_argument("--extract", action="store_true",
                        help="Extract all component files to current directory")
    parser.add_argument("--setup",   action="store_true",
                        help="Print setup guide")
    args = parser.parse_args()

    if args.setup:
        print(SETUP_GUIDE)

    elif args.extract:
        files = {
            "webhook_server.py":          WEBHOOK_SERVER,
            "call_scheduler.py":          SCHEDULER_SERVER,
            "requirements_webhook.txt":   REQUIREMENTS_WEBHOOK,
            "Procfile":                   PROCFILE,
        }
        for fname, content in files.items():
            with open(fname, "w") as f:
                f.write(content.strip() + "
")
            print(f"✅  Wrote {fname}")
        print("
Next step: python call_scheduler.py --setup")

    else:
        print("Calendar-Driven Call Recording System")
        print("  python call_recording_system.py --extract   # extract files")
        print("  python call_recording_system.py --setup     # print setup guide")
