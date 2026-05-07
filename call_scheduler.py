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

import argparse
import hashlib
import hmac
import json
import os
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

# ── firm_context.yaml — load principal email for calendar watch ───────────────

def _load_principal_email() -> str:
    """Read principal.email from firm_context.yaml (cos-pipeline or tomac-cove-pipeline).
    Falls back to hardcoded default so existing installs keep working."""
    candidates = [
        Path.home() / "cos-pipeline" / "firm_context.yaml",
        Path(__file__).parent / "firm_context.yaml",
        Path(__file__).parent.parent / "cos-pipeline" / "firm_context.yaml",
    ]
    for p in candidates:
        if p.exists():
            try:
                import yaml  # optional — only needed here
                ctx = yaml.safe_load(p.read_text())
                email = (ctx or {}).get("principal", {}).get("email", "")
                if email:
                    return email
            except Exception:
                pass
    return "ygontownik@gmail.com"  # fallback for existing installs


# ── Config ────────────────────────────────────────────────────────────────────

WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "")
RAILWAY_URL       = os.environ.get("RAILWAY_URL", "")        # your Railway app URL

# Google OAuth (reuse existing credentials)
GCAL_CREDS_PATH   = os.path.expanduser("~/credentials/client_secret.json")
GCAL_TOKEN_PATH   = os.path.expanduser("~/credentials/gcal_token.json")
GCAL_WATCH_PATH   = os.path.expanduser("~/credentials/gcal_watch_channels.json")

# Microsoft
MS_CLIENT_ID      = os.environ.get("MICROSOFT_CLIENT_ID", "")
MS_CLIENT_SECRET  = os.environ.get("MICROSOFT_CLIENT_SECRET", "")
MS_TENANT_ID      = os.environ.get("MICROSOFT_TENANT_ID", "common")
MS_TOKEN_PATH     = os.path.expanduser("~/credentials/ms_token.json")
MS_SUB_PATH       = os.path.expanduser("~/credentials/ms_graph_subscription.json")

# Paths
RECORDER_SCRIPT         = os.path.expanduser("~/tomac-cove-pipeline/call_recorder.py")
OTTER_SCRIPT            = os.path.expanduser("~/tomac-cove-pipeline/otter_record.sh")
OTTER_BACKFILL_SCRIPT   = os.path.expanduser("~/dashboards/routines/process/cos_otter_backfill.py")
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

WATCHED_CALENDARS = {_load_principal_email()}  # driven by firm_context.yaml → principal.email

# ── Otter webhook rate-limit ─────────────────────────────────────────────────
_otter_last_fired: float = 0.0          # epoch seconds of last /otter-webhook dispatch
_OTTER_MIN_INTERVAL: int = 60           # seconds — ignore webhook if fired more recently

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(SCHEDULER_LOG), exist_ok=True)
    with open(SCHEDULER_LOG, "a") as f:
        f.write(line + "
")

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
        creds = Credentials.from_authorized_user_file(GCAL_TOKEN_PATH, GCAL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow  = InstalledAppFlow.from_client_secrets_file(GCAL_CREDS_PATH, GCAL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GCAL_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
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
            if cal["id"] not in WATCHED_CALENDARS:
                continue
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
        print(f"
🔑  Microsoft auth required (one-time):")
        print(f"    Go to: {dc['verification_uri']}")
        print(f"    Code:  {dc['user_code']}
")
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

# ── confirmation email ────────────────────────────────────────────────────────

_GMAIL_TOKEN_PATH = os.path.expanduser("~/credentials/token.json")
_NOTIFY_TO        = "ygontownik@gmail.com"

def _send_confirmation_email(title: str, start_at: datetime, end: datetime, source: str):
    """Send a plain-text confirmation that a recording has been armed. Fails silently."""
    try:
        import base64
        from email.mime.text import MIMEText
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(_GMAIL_TOKEN_PATH)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())

        local_start = start_at.astimezone()
        local_end   = end.astimezone()
        body = (
            f"Recording armed: {title}

"
            f"Otter starts : {local_start.strftime('%a %b %-d at %-I:%M %p %Z')}
"
            f"Otter stops  : {local_end.strftime('%-I:%M %p %Z')}
"
            f"Source       : {source or 'Google Calendar'}
"
        )

        msg = MIMEText(body, "plain", "utf-8")
        msg["To"]      = _NOTIFY_TO
        msg["From"]    = _NOTIFY_TO
        msg["Subject"] = f"Recording armed: {title}"

        raw    = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        svc    = build("gmail", "v1", credentials=creds)
        result = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        log(f"📧  Confirmation sent (msg {result['id']})")
    except Exception as e:
        log(f"⚠️  Confirmation email failed (recording still armed): {e}")


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
        start_at = now + timedelta(seconds=30)  # already past — start ASAP (30s buffer avoids immediate re-trigger)

    safe_id    = _safe_label(meeting_id)
    start_label = f"com.tomaccove.recorder.start.{safe_id}"
    stop_label  = f"com.tomaccove.recorder.stop.{safe_id}"

    # Write START plist — triggers Otter recording
    _write_launchd_plist(
        label=start_label,
        program="/bin/bash",
        args=[OTTER_SCRIPT, "start"],
        run_at=start_at,
        log_path=os.path.expanduser(f"~/recordings/calls/{safe_id}_start.log"),
    )

    # Write STOP plist — stops Otter recording
    _write_launchd_plist(
        label=stop_label,
        program="/bin/bash",
        args=[OTTER_SCRIPT, "stop"],
        run_at=end,
        log_path=os.path.expanduser(f"~/recordings/calls/{safe_id}_stop.log"),
    )

    log(f"✅  Scheduled: '{title}'")
    log(f"    Start recording: {start_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    log(f"    Stop recording:  {end.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")

    threading.Thread(
        target=_send_confirmation_email,
        args=(title, start_at, end, meeting.get("source", "")),
        daemon=True,
    ).start()

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

    # Unload if already loaded (ignore rc — job may not exist yet)
    unload = subprocess.run(["launchctl", "unload", plist_path],
                            capture_output=True)
    if unload.returncode not in (0, 3):   # rc=3 → job was not loaded — that's fine
        log(f"⚠️  launchctl unload warning for {label} (rc={unload.returncode}): "
            f"{unload.stderr.decode().strip()}")
    # Brief pause so launchd releases the old slot before we re-register
    time.sleep(0.5)
    result = subprocess.run(["launchctl", "load", plist_path],
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
        if cal_id not in WATCHED_CALENDARS:
            continue
        safe_cal   = re.sub(r"[^A-Za-z0-9\-_]", "-", cal_id)[:30]
        channel_id = f"recorder-{safe_cal}-{int(time.time())}"
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
    print(f"
    GOOGLE_CHANNEL_TOKEN={channel_token}")
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

    def _handle_otter_webhook(self):
        global _otter_last_fired
        secret = self.headers.get("X-Otter-Secret", "")
        if not WEBHOOK_SECRET or not hmac.compare_digest(secret, WEBHOOK_SECRET):
            log("/otter-webhook: rejected (bad secret)")
            self.send_response(403)
            self.end_headers()
            return

        now = time.time()
        if now - _otter_last_fired < _OTTER_MIN_INTERVAL:
            log(f"/otter-webhook: skipped (fired {int(now - _otter_last_fired)}s ago, min {_OTTER_MIN_INTERVAL}s)")
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"skipped","reason":"rate_limited"}')
            return

        _otter_last_fired = now
        log("/otter-webhook: firing cos_otter_backfill.py")
        with open(SCHEDULER_LOG, "a") as lf:
            subprocess.Popen(
                [sys.executable, OTTER_BACKFILL_SCRIPT],
                stdout=lf, stderr=subprocess.STDOUT,
            )
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"accepted"}')

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
        elif self.path == "/record" or self.path.startswith("/record?"):
            html = self._record_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def _record_page(self) -> str:
        """Simple mobile-friendly page to start/stop recording from iPhone."""
        recording = bool(subprocess.run(
            ["pgrep", "-f", "call_recorder.py.*start"],
            capture_output=True
        ).returncode == 0)
        if recording:
            body = """
            <div class="status recording">⏺ Recording in progress</div>
            <form method="POST" action="/record/stop">
              <button type="submit" class="btn stop">Stop &amp; Process</button>
            </form>"""
        else:
            body = """
            <div class="status idle">⏹ Ready to record</div>
            <form method="POST" action="/record/start">
              <input type="text" name="title" placeholder="Call title (optional)"
                     class="title-input" autocomplete="off">
              <button type="submit" class="btn start">Start Recording</button>
            </form>"""
        return f"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Record Call</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #1c1c1e; color: #fff;
         display: flex; flex-direction: column; align-items: center;
         justify-content: center; min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; }}
  .status {{ font-size: 1.4rem; margin-bottom: 32px; }}
  .recording {{ color: #ff453a; }}
  .idle {{ color: #34c759; }}
  .title-input {{ width: 100%; max-width: 320px; padding: 14px; font-size: 1.1rem;
                  border-radius: 12px; border: none; margin-bottom: 16px;
                  background: #2c2c2e; color: #fff; display: block; box-sizing: border-box; }}
  .btn {{ width: 100%; max-width: 320px; padding: 18px; font-size: 1.2rem; font-weight: 600;
          border: none; border-radius: 14px; cursor: pointer; display: block; }}
  .start {{ background: #0a84ff; color: #fff; }}
  .stop  {{ background: #ff453a; color: #fff; }}
</style>
</head><body>
{body}
</body></html>"""

    def do_POST(self):
        # Handle mobile record page POSTs (no signature required — local UI)
        if self.path == "/record/start":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length).decode() if length else ""
            from urllib.parse import parse_qs
            params = parse_qs(body)
            title  = params.get("title", [""])[0].strip() or f"Ad Hoc Call {datetime.now().strftime('%b %d %H:%M')}"
            log(f"Mobile start recording: {title}")
            with open(SCHEDULER_LOG, "a") as lf:
                subprocess.Popen(
                    [sys.executable, RECORDER_SCRIPT, "start", "--title", title, "--engine", "assemblyai-nano"],
                    stdout=lf, stderr=subprocess.STDOUT,
                )
            self.send_response(302)
            self.send_header("Location", "/record")
            self.end_headers()
            return

        if self.path == "/record/stop":
            log("Mobile stop recording")
            with open(SCHEDULER_LOG, "a") as lf:
                subprocess.Popen(
                    [sys.executable, RECORDER_SCRIPT, "stop"],
                    stdout=lf, stderr=subprocess.STDOUT,
                )
            self.send_response(302)
            self.send_header("Location", "/record")
            self.end_headers()
            return

        if self.path == "/otter-webhook":
            self._handle_otter_webhook()
            return

        # All other POSTs require HMAC signature
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else b""

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
                threading.Thread(target=self._refresh_and_schedule,
                                  args=(fetch_gcal_upcoming,), daemon=True).start()

            elif action == "refresh_outlook":
                log("  Outlook change — refreshing upcoming events")
                threading.Thread(target=self._refresh_and_schedule,
                                  args=(fetch_outlook_upcoming,), daemon=True).start()

            elif action == "process_twilio_recording":
                log(f"  Twilio recording ready: {data.get('title','?')} ({data.get('duration',0)}s)")
                threading.Thread(target=self._process_twilio_recording, args=(data,),
                                  daemon=True).start()

            elif data.get("id"):
                if data.get("has_video"):
                    threading.Thread(target=schedule_meeting, args=(data,),
                                      daemon=True).start()

        self.send_response(200)
        self.end_headers()

    def _process_twilio_recording(self, data: dict):
        """Download Twilio MP3 and hand off to call_recorder for transcription + memo."""
        import tempfile, urllib.request
        url      = data.get("recording_url", "")
        title    = data.get("title", "Phone Call")
        duration = data.get("duration", 0)
        sid      = data.get("call_sid", "")

        if not url:
            log("No recording URL in Twilio payload")
            return

        # Download the MP3 (Twilio requires auth)
        twilio_sid    = os.environ.get("TWILIO_ACCOUNT_SID", "")
        twilio_token  = os.environ.get("TWILIO_AUTH_TOKEN", "")
        log(f"Downloading Twilio recording: {url}")
        try:
            pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            pm.add_password(None, url, twilio_sid, twilio_token)
            opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(pm))
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False,
                                              dir=os.path.expanduser("~/recordings/calls"))
            with opener.open(url) as resp:
                tmp.write(resp.read())
            tmp.close()
            log(f"Downloaded to {tmp.name} ({duration}s)")
        except Exception as e:
            log(f"Failed to download Twilio recording: {e}")
            return

        # Hand off to call_recorder's transcribe command
        with open(SCHEDULER_LOG, "a") as lf:
            subprocess.run(
                [sys.executable, RECORDER_SCRIPT, "transcribe",
                 "--file", tmp.name, "--title", title],
                stdout=lf, stderr=subprocess.STDOUT,
            )

    def _refresh_and_schedule(self, fetch_fn):
        events    = fetch_fn(hours_ahead=48)
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

    with open(SCHEDULER_LOG, "a") as lf:
        proc = subprocess.Popen(
            [sys.executable, __file__, "run"],
            stdout=lf, stderr=subprocess.STDOUT,
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
    print(f"
Scheduled meetings ({len(scheduled)}):")
    for mid, s in sorted(scheduled.items(), key=lambda x: x[1].get("start", "")):
        print(f"  [{s['source'][:1].upper()}] {s['start'][:16]}  {s['title'][:60]}")

def cmd_register(args):
    print("Registering calendar subscriptions...
")
    print("── Google Calendar ──")
    register_google_watch()
    print("
── Outlook/Microsoft 365 ──")
    register_outlook_subscription()
    print("
✅  Done. Subscriptions are active.")
    print("    ⚠️  Google watches expire in 7 days — re-run register weekly or set up auto-renewal.")
    print("    ⚠️  Outlook subscriptions expire in 2 days — same.")

def _initial_calendar_fetch():
    scheduled = load_json(SCHEDULED_PATH)
    for ev in fetch_gcal_upcoming(48) + fetch_outlook_upcoming(48):
        if ev.get("has_video") and ev["id"] not in scheduled:
            schedule_meeting(ev)

def cmd_run(args):
    log(f"Scheduler listening on port {PORT}")
    log("Initial calendar fetch...")
    threading.Thread(target=_initial_calendar_fetch, daemon=True).start()

    HTTPServer.allow_reuse_address = True
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
