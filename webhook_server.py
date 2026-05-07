#!/usr/bin/env python3
"""
webhook_server.py — Deploy on Railway
Receives Google Calendar, Microsoft Graph, and Twilio notifications.
Forwards parsed meeting/recording details to the Mac scheduler endpoint.

Railway env vars needed:
  WEBHOOK_SECRET        shared secret between Railway and your Mac
  MAC_SCHEDULER_URL     your Mac's public URL (ngrok tunnel)
  GOOGLE_CHANNEL_TOKEN  token set when registering Google watch
  TWILIO_ACCOUNT_SID    Twilio credentials
  TWILIO_AUTH_TOKEN
  TWILIO_NUMBER         your Twilio phone number e.g. +18665807882
  YONI_MOBILE           your personal mobile e.g. +12015551234
"""

import hashlib
import hmac
import json
import os
import re
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify, abort, Response

app = Flask(__name__)

WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET", "")
MAC_SCHEDULER_URL    = os.environ.get("MAC_SCHEDULER_URL", "")
GOOGLE_CHANNEL_TOKEN = os.environ.get("GOOGLE_CHANNEL_TOKEN", "")
TWILIO_ACCOUNT_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN    = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER        = os.environ.get("TWILIO_NUMBER", "")
YONI_MOBILE          = os.environ.get("YONI_MOBILE", "")   # filled in after user provides it
RAILWAY_URL          = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
if RAILWAY_URL and not RAILWAY_URL.startswith("http"):
    RAILWAY_URL = f"https://{RAILWAY_URL}"

VIDEO_PATTERNS = [
    r"teams\.microsoft\.com", r"zoom\.us", r"meet\.google\.com",
    r"webex\.com", r"gotomeeting\.com", r"whereby\.com", r"bluejeans\.com",
    r"\+1[\s\-\.]?\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}",
    r"tel:\+?\d{7,}", r"dial[\s\-]in", r"conference\s+(id|code|pin)",
    r"passcode", r"meeting\s+id", r"access\s+code",
]
VIDEO_RE = re.compile("|".join(VIDEO_PATTERNS), re.IGNORECASE)


def has_video_link(text: str) -> bool:
    return bool(VIDEO_RE.search(text or ""))


def forward_to_mac(payload_dict: dict):
    """Send payload to Mac scheduler with HMAC signature."""
    if not MAC_SCHEDULER_URL:
        print(f"MAC_SCHEDULER_URL not set — cannot forward: {payload_dict.get('action','?')}")
        return
    try:
        payload = json.dumps(payload_dict)
        sig = hmac.new(
            WEBHOOK_SECRET.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        r = requests.post(
            f"{MAC_SCHEDULER_URL}/schedule",
            data=payload,
            headers={"Content-Type": "application/json", "X-Webhook-Signature": sig},
            timeout=10,
        )
        print(f"Forwarded to Mac: {r.status_code} {payload_dict.get('action','?')}")
    except Exception as e:
        print(f"Failed to forward to Mac: {e}")


# ── Google Calendar ────────────────────────────────────────────────────────────

@app.route("/google/calendar", methods=["POST"])
def google_calendar_push():
    channel_token  = request.headers.get("X-Goog-Channel-Token", "")
    resource_state = request.headers.get("X-Goog-Resource-State", "")
    resource_id    = request.headers.get("X-Goog-Resource-ID", "")
    channel_id     = request.headers.get("X-Goog-Channel-ID", "")

    if GOOGLE_CHANNEL_TOKEN and channel_token != GOOGLE_CHANNEL_TOKEN:
        abort(403)

    if resource_state == "sync":
        print(f"Google Calendar sync for channel {channel_id}")
        return "", 200

    if resource_state in ("exists", "not_exists"):
        print(f"Google Calendar change: state={resource_state} resource={resource_id}")
        forward_to_mac({"action": "refresh_google", "resource": resource_id, "channel": channel_id})

    return "", 200


# ── Microsoft Graph ────────────────────────────────────────────────────────────

@app.route("/outlook/calendar", methods=["POST"])
def outlook_calendar_push():
    validation_token = request.args.get("validationToken")
    if validation_token:
        return validation_token, 200, {"Content-Type": "text/plain"}

    try:
        body = request.get_json(force=True)
    except Exception:
        return "", 400

    for notification in body.get("value", []):
        change_type  = notification.get("changeType", "")
        resource     = notification.get("resource", "")
        client_state = notification.get("clientState", "")
        if WEBHOOK_SECRET and client_state != WEBHOOK_SECRET:
            continue
        print(f"Outlook change: {change_type} {resource}")
        if change_type in ("created", "updated"):
            forward_to_mac({"action": "refresh_outlook", "resource": resource, "change": change_type})

    return "", 202


# ── Twilio Voice ───────────────────────────────────────────────────────────────

def twiml(xml: str) -> Response:
    return Response(xml, mimetype="text/xml")


@app.route("/twilio/voice", methods=["POST"])
def twilio_voice():
    """
    Handles all calls to the Twilio number.

    Workflow — merge recording (most common):
      You're on a live call → iPhone Add Call → dial +18665807882 →
      hear beep → tap Merge Calls → hang up when done.
      Recording fires automatically when call ends.

    Workflow — outbound bridge:
      Call +18665807882 fresh → hear beep → recording starts →
      then use iPhone Add Call to dial the other person → Merge.
      Same result either way.

    No menus, no input required. Just beep and record.
    """
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>https://api.twilio.com/cowbell.mp3</Play>
  <Record maxLength="14400"
          recordingStatusCallback="{RAILWAY_URL}/twilio/recording"
          recordingStatusCallbackMethod="POST"
          recordingStatusCallbackEvent="completed" />
</Response>"""
    return twiml(xml)


@app.route("/twilio/dial-out", methods=["POST"])
def twilio_dial_out():
    """Yoni entered a number — dial it and record the call."""
    digits = request.form.get("Digits", "").strip()
    if not digits:
        return twiml("""<?xml version="1.0" encoding="UTF-8"?>
<Response><Say>No number received. Goodbye.</Say></Response>""")

    # Add + prefix if not present and looks like a US number
    if not digits.startswith("+"):
        digits = f"+1{digits}" if len(digits) == 10 else f"+{digits}"

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Connecting and recording.</Say>
  <Dial record="record-from-answer"
        recordingStatusCallback="{RAILWAY_URL}/twilio/recording"
        recordingStatusCallbackMethod="POST"
        recordingStatusCallbackEvent="completed"
        callerId="{TWILIO_NUMBER}">
    <Number>{digits}</Number>
  </Dial>
</Response>"""
    return twiml(xml)


@app.route("/twilio/recording", methods=["POST"])
def twilio_recording():
    """
    Twilio calls this when a recording is ready.
    We forward the MP3 URL to the Mac for AssemblyAI transcription + Claude memo.
    """
    recording_url = request.form.get("RecordingUrl", "")
    duration      = int(request.form.get("RecordingDuration", "0"))
    call_sid      = request.form.get("CallSid", "")
    caller        = request.form.get("From", "unknown")
    called        = request.form.get("To", "unknown")

    if not recording_url or duration < 10:
        print(f"Skipping short/empty recording: {duration}s from {caller}")
        return "", 204

    # Twilio recording URLs need .mp3 appended
    mp3_url = recording_url + ".mp3"
    title   = f"Call {caller} → {called} {datetime.now(timezone.utc).strftime('%b %d %Y %H:%M')}"

    print(f"Recording ready: {duration}s — {mp3_url}")
    forward_to_mac({
        "action":        "process_twilio_recording",
        "recording_url": mp3_url,
        "duration":      duration,
        "call_sid":      call_sid,
        "caller":        caller,
        "called":        called,
        "title":         title,
        "source":        "twilio",
    })

    return "", 204


# ── Health ─────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "twilio": bool(TWILIO_ACCOUNT_SID),
        "mobile_set": bool(YONI_MOBILE),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
