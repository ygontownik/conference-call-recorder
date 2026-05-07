# Conference Call Recorder

Automatically records conference calls (Teams, Zoom, Google Meet, or any dial-in) on Mac, transcribes via AssemblyAI, generates a structured memo via Claude, and saves everything to Google Drive.

**Stack:** Twilio · Railway · AssemblyAI · Claude (Anthropic) · Google Drive

---

## Two Recording Paths — Read This First

This system handles **two different call types** with different automation paths. Know which one you need before setting up.

| | Dial-in calls | Webinar / browser calls |
|---|---|---|
| **What it is** | Conference calls with a phone number + PIN (Teams, Zoom, any bridge) | Browser-based events: Cvent, GoToWebinar, Zoom webinar links, Google Meet |
| **How it joins** | Twilio dials the number and PIN automatically | Chrome opens the URL; AppleScript clicks join |
| **Audio capture** | Twilio records the call in the cloud | BlackHole captures system audio on your Mac |
| **Transcription** | AssemblyAI | AssemblyAI (same) |
| **Trigger** | Calendar event with a dial-in number | Calendar event with a webinar URL |
| **Current dashboard support** | ✅ Fully supported | ⚠️ Requires additional setup (see Webinar Path below) |

> **The current dashboard and scheduler are built for dial-in calls.** If your event has a webinar URL instead of a phone number, follow the Webinar Path section — the Twilio flow will not apply.

---

## How It Works — Dial-in Path

```
Google Calendar / Outlook
        ↓  (push notification — event has phone number + PIN)
Railway (webhook_server.py)
        ↓  (forwards to your Mac)
call_scheduler.py (Mac)
        ↓  (schedules launchd job — Twilio dials in)
Twilio  →  joins the call via phone
call_recorder.py (Mac) records cloud audio
AssemblyAI  →  transcript
Claude      →  structured memo
Google Drive → saved
```

1. Your calendar sends a push notification to Railway when a meeting with a dial-in number is created.
2. Railway forwards it to your Mac.
3. The Mac scheduler creates a launchd job; Twilio dials the conference bridge at the right time.
4. AssemblyAI transcribes, Claude generates memo, both upload to Google Drive.

---

## How It Works — Webinar Path

```
Google Calendar / Outlook
        ↓  (push notification — event has webinar URL)
call_scheduler.py (Mac)
        ↓  (schedules launchd job — opens Chrome)
Chrome opens webinar URL → AppleScript clicks join
BlackHole captures system audio
call_recorder.py (Mac) records local audio
AssemblyAI  →  transcript
Claude      →  structured memo
Google Drive → saved
```

1. Your calendar event contains a webinar URL (Cvent, Zoom, Meet, etc.).
2. The scheduler extracts the URL and opens Chrome at T-2min.
3. AppleScript clicks the join button.
4. BlackHole captures all browser audio; `call_recorder.py` records it.
5. Same transcription and Drive upload as the dial-in path.

**Additional setup required for webinar path:**
- BlackHole must be set as your Mac's system audio output
- AppleScript join automation (see Webinar Setup section below)
- Railway/Twilio are **not used** in this path

---

## Accounts You Need

| Service | Free tier? | What it's for |
|---------|-----------|---------------|
| [Twilio](https://twilio.com) | Yes (trial) | Dials into conference calls |
| [Railway](https://railway.app) | Yes ($5/mo credit) | Hosts the webhook server |
| [AssemblyAI](https://assemblyai.com) | Yes (free credits) | Transcription (~$0.009/min) |
| [Anthropic](https://console.anthropic.com) | Pay-as-you-go | Memo generation |
| Google Cloud | Free | Drive API for saving output |

---

## Account Setup (do this first)

### Twilio

1. Go to [twilio.com](https://twilio.com) → **Sign Up** (free trial, no credit card required)
2. Verify your email and phone number
3. In the Console, click **Get a Trial Number** — this gives you a real US phone number for free
4. Go to **Account → API keys & tokens** → copy **Account SID** and **Auth Token**
5. Your trial number is your `TWILIO_PHONE_NUMBER` (format: `+1xxxxxxxxxx`)

> Trial accounts can only call verified numbers. To call any number, upgrade to a paid account (~$1/mo for the number + per-minute usage).

---

### Railway

1. Go to [railway.app](https://railway.app) → **Login with GitHub**
2. No credit card needed — you get $5/mo free credit (enough for this server)
3. You don't need to create a project yet — the CLI handles that in Setup step 4 below

---

### AssemblyAI

1. Go to [assemblyai.com](https://assemblyai.com) → **Sign Up**
2. Verify your email
3. You get free credits on signup (~$50 worth)
4. Go to **Dashboard → API Keys** → copy your key

---

### Anthropic (Claude)

1. Go to [console.anthropic.com](https://console.anthropic.com) → **Sign Up**
2. Add a payment method (pay-as-you-go, no subscription)
3. Go to **API Keys** → **Create Key** → copy it
4. Add $5–10 in credits to start — memo generation costs ~$0.02–0.05 per call

---

### Google Cloud (Drive API)

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → sign in with your Google account
2. Click **Select a project** → **New Project** → name it (e.g. `call-recorder`) → **Create**
3. In the left menu go to **APIs & Services → Library**
4. Search for **Google Drive API** → click it → **Enable**
5. Search for **Google Docs API** → click it → **Enable**
6. Go to **APIs & Services → Credentials** → **Create Credentials → Service Account**
7. Give it any name → click through to **Done**
8. Click the service account you just created → go to **Keys** tab → **Add Key → Create new key → JSON** → download the file
9. Save it to `~/credentials/service_account.json` on your Mac
10. Go to [drive.google.com](https://drive.google.com) → create a new folder called **Call Recordings**
11. Right-click the folder → **Share** → paste in your service account email (it looks like `name@project.iam.gserviceaccount.com`) → give it **Editor** access
12. Open the folder → copy the ID from the URL: `drive.google.com/drive/folders/`**THIS_PART**

---

## Prerequisites (Mac)

```bash
# Virtual audio driver — routes system audio to recorder
brew install blackhole-2ch

# Audio recording
brew install ffmpeg

# Python deps
pip install flask requests gunicorn twilio assemblyai anthropic \
    google-auth google-auth-oauthlib google-api-python-client pyyaml
```

After installing BlackHole, open **System Settings → Sound → Output** and select **BlackHole 2ch** (or create a Multi-Output Device in Audio MIDI Setup so you hear audio AND it records simultaneously).

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/ygontownik/conference-call-recorder.git
cd conference-call-recorder
```

### 2. Set environment variables

Copy the template and fill in your keys:

```bash
cp .env.example .env
```

Edit `.env`:

```
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_PHONE_NUMBER=+1xxxxxxxxxx

ASSEMBLYAI_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

GOOGLE_SERVICE_ACCOUNT_JSON=~/credentials/service_account.json
GOOGLE_DRIVE_FOLDER_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

WEBHOOK_SECRET=any-random-string-you-choose
RAILWAY_URL=https://your-app.railway.app   # fill in after step 4
MAC_SCHEDULER_URL=https://your-ngrok-url   # fill in after step 5
```

### 3. Set up Google Drive

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project → enable **Google Drive API** and **Google Docs API**
3. Create a **Service Account** → download the JSON key → save to `~/credentials/service_account.json`
4. Create a folder in Google Drive for call recordings → copy the folder ID from the URL
5. Share that folder with your service account email (it looks like `name@project.iam.gserviceaccount.com`)

### 4. Deploy webhook server to Railway

```bash
# Install Railway CLI
npm install -g @railway/cli

# Log in
railway login

# Create a new project
railway init

# Deploy
railway up
```

In the Railway dashboard, go to **Variables** and add all env vars from your `.env` file.

Copy your Railway app URL (e.g. `https://your-app.railway.app`) and set it as `RAILWAY_URL` in your `.env`.

### 5. Expose your Mac with ngrok

Railway needs to reach your Mac to forward calendar notifications.

```bash
# Install ngrok
brew install ngrok

# Start a tunnel on port 8765 (or whichever port call_scheduler uses)
ngrok http 8765
```

Copy the `https://xxxx.ngrok.io` URL and set it as `MAC_SCHEDULER_URL` in your `.env` and in Railway's Variables.

### 6. Start the Mac scheduler

```bash
# Load env vars
source .env   # or add them to ~/.zshrc

# Register calendar subscriptions (one-time)
python call_scheduler.py register

# Start the scheduler daemon
python call_scheduler.py start
```

The scheduler will listen for forwarded notifications from Railway and create launchd jobs for upcoming calls.

### 7. Test it

Manually trigger a recording:

```bash
# Start recording
python call_recorder.py start

# Stop, transcribe, generate memo, upload to Drive
python call_recorder.py stop
```

Check your Google Drive folder — you should see a transcript + memo doc.

---

## Usage

```bash
# Start recording now (prompts for call title)
python call_recorder.py start

# Stop and process (transcribe → memo → Drive)
python call_recorder.py stop

# Transcribe an existing audio file
python call_recorder.py transcribe --file ~/recordings/call.mp3

# List recent calls
python call_recorder.py list

# Check scheduled upcoming recordings
python call_scheduler.py list
```

---

## Transcription Engines

| Engine | Cost/min | Quality |
|--------|----------|---------|
| AssemblyAI default | ~$0.009 | Best — speaker diarization |
| AssemblyAI nano | ~$0.002 | Good for business English |
| Deepgram Nova-2 | ~$0.0043 | Good alternative |

Override at runtime:
```bash
python call_recorder.py start --engine assemblyai
python call_recorder.py start --engine assemblyai-nano
python call_recorder.py start --engine deepgram
```

---

## Environment Variables Reference

| Variable | Where to get it |
|----------|----------------|
| `TWILIO_ACCOUNT_SID` | Twilio Console → Account Info |
| `TWILIO_AUTH_TOKEN` | Twilio Console → Account Info |
| `TWILIO_PHONE_NUMBER` | Twilio Console → Phone Numbers |
| `ASSEMBLYAI_API_KEY` | assemblyai.com → API Keys |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google Cloud Console → IAM → Service Accounts |
| `GOOGLE_DRIVE_FOLDER_ID` | From Drive folder URL: `drive.google.com/drive/folders/THIS_PART` |
| `WEBHOOK_SECRET` | Any random string — must match on Railway and Mac |
| `RAILWAY_URL` | Your Railway app URL after deploying |
| `MAC_SCHEDULER_URL` | Your ngrok tunnel URL |

---

## Webinar Setup (browser-based calls)

If you skipped the Twilio/Railway setup because your calls are webinar links, you only need:

### What to install

```bash
brew install blackhole-2ch ffmpeg
pip install assemblyai anthropic google-auth google-auth-oauthlib google-api-python-client
```

### Configure BlackHole as system output

1. Open **Audio MIDI Setup** (search in Spotlight)
2. Click **+** → **Create Multi-Output Device**
3. Check both **BlackHole 2ch** and your speakers/headphones
4. Go to **System Settings → Sound → Output** → select the Multi-Output Device
5. You will now hear audio normally AND BlackHole captures it for recording

### Who shows up and mute behavior

**Display name:** The name shown in the webinar is pulled from whichever account Chrome is logged into:

| Platform | Name source |
|----------|------------|
| Cvent | Your registration name from when you signed up for the event |
| Zoom | Your Zoom account display name |
| Google Meet | Your Google account name |
| Teams | Your Microsoft account name |

Make sure Chrome is logged into the relevant account before the webinar. If the event is guest-join (no login), the AppleScript below pre-fills your name before clicking join.

**Mute:** Most webinar platforms (including Cvent) mute attendees by default — you have no mic to unmute. For platforms that don't auto-mute, the AppleScript handles it explicitly after joining.

**Note:** Since recording happens via BlackHole (system audio output), your microphone is never involved regardless of mute state. BlackHole captures only what the webinar plays to you.

### Registration numbers

Some platforms (Cvent in particular) email a unique registration confirmation link or number that is required to join — the generic event URL alone will not work. The scheduler extracts this automatically from your Gmail confirmation email.

Save this as `~/scripts/extract_webinar_registration.py`:

```python
#!/usr/bin/env python3
"""
Searches Gmail for a webinar registration confirmation and extracts:
- The unique join URL (preferred)
- Or the registration/confirmation number as fallback

Usage: python extract_webinar_registration.py "Event Title Keywords"
Returns: the join URL or registration number, printed to stdout
"""

import os
import re
import sys
import base64
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import pickle

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_PATH = os.path.expanduser("~/credentials/gmail_token.pickle")
CREDS_PATH = os.path.expanduser("~/credentials/gdrive_credentials.json")

# Patterns to extract from confirmation emails
JOIN_URL_PATTERNS = [
    r"https://web\.cvent\.com/event/[^\s\"<>]+",         # Cvent unique join link
    r"https://[^\s\"<>]+/join\?[^\s\"<>]+registrant[^\s\"<>]+",  # Zoom registrant link
    r"https://[^\s\"<>]+\?tk=[^\s\"<>]+",                # generic token-based join
]
CONFIRMATION_PATTERNS = [
    r"[Cc]onfirmation\s+(?:[Nn]umber|#|[Ii][Dd])[:\s]+([A-Z0-9\-]{6,})",
    r"[Rr]egistration\s+(?:[Nn]umber|#|[Ii][Dd])[:\s]+([A-Z0-9\-]{6,})",
    r"[Aa]ttendee\s+[Ii][Dd][:\s]+([A-Z0-9\-]{6,})",
]

def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError("Gmail token missing — run gmail OAuth flow first")
    return build("gmail", "v1", credentials=creds)

def search_confirmation_email(service, keywords):
    query = f"subject:(registration OR confirmation OR registered) {keywords}"
    results = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
    return results.get("messages", [])

def get_email_body(service, msg_id):
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    parts = msg.get("payload", {}).get("parts", [])
    body = ""
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            body += base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    if not body:
        data = msg.get("payload", {}).get("body", {}).get("data", "")
        if data:
            body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return body

def extract_join_info(body):
    for pattern in JOIN_URL_PATTERNS:
        match = re.search(pattern, body)
        if match:
            return ("url", match.group(0))
    for pattern in CONFIRMATION_PATTERNS:
        match = re.search(pattern, body)
        if match:
            return ("number", match.group(1))
    return (None, None)

if __name__ == "__main__":
    keywords = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    service = get_gmail_service()
    messages = search_confirmation_email(service, keywords)
    for msg in messages:
        body = get_email_body(service, msg["id"])
        kind, value = extract_join_info(body)
        if value:
            print(f"{kind}:{value}")
            sys.exit(0)
    print("none:")
    sys.exit(1)
```

### Updated AppleScript — uses registration URL if found

Save this as `~/scripts/join_webinar.applescript`:

```applescript
on run argv
    set webinarURL to item 1 of argv
    set regInfo to item 2 of argv  -- "url:https://..." or "number:ABC123" or "none:"
    set yourName to "Yoni Gontownik"

    -- use unique registration URL if found, otherwise fall back to generic URL
    set joinURL to webinarURL
    if regInfo starts with "url:" then
        set joinURL to text 5 thru -1 of regInfo
    end if

    tell application "Google Chrome"
        open location joinURL
        activate
    end tell

    delay 12

    tell application "System Events"
        tell process "Google Chrome"
            -- if platform prompts for name or confirmation number
            if regInfo starts with "number:" then
                set regNumber to text 8 thru -1 of regInfo
                keystroke regNumber  -- enter confirmation number
                delay 1
                keystroke tab       -- move to next field
                delay 0.5
                keystroke yourName  -- enter name if prompted
            else
                keystroke yourName  -- guest join — just type name
            end if
            delay 1
            keystroke return  -- click join

            -- mute after joining
            delay 5
            keystroke "d" using command down                   -- Zoom
            keystroke "d" using {command down, shift down}    -- Google Meet
            -- Cvent webinar attendees are muted by default
        end tell
    end tell
end run
```

### How the scheduler calls it

```python
import subprocess

# 1. search Gmail for registration info using event title keywords
result = subprocess.run(
    ["python", "~/scripts/extract_webinar_registration.py", meeting_title],
    capture_output=True, text=True
)
reg_info = result.stdout.strip() or "none:"

# 2. open Chrome with the correct URL + pass registration info
subprocess.Popen([
    "osascript", "~/scripts/join_webinar.applescript",
    meeting_url,   # fallback URL from calendar event
    reg_info       # "url:...", "number:...", or "none:"
])

# 3. start recording
subprocess.Popen(["python", "call_recorder.py", "start", "--title", meeting_title])
```

### Wire it into the scheduler

In `call_scheduler.py`, the scheduler already extracts meeting URLs from calendar events. Add this to the job creation logic:

```python
if is_webinar_url(meeting_url):  # checks for cvent/zoom/meet/teams patterns
    # open browser + start BlackHole recording
    subprocess.Popen(["osascript", "~/scripts/join_webinar.applescript", meeting_url])
    subprocess.Popen(["python", "call_recorder.py", "start", "--title", meeting_title])
else:
    # existing dial-in path via Twilio
    twilio_dial(meeting_phone, meeting_pin)
```

### Env vars needed (webinar path only)

```
ASSEMBLYAI_API_KEY=...
ANTHROPIC_API_KEY=...
GOOGLE_SERVICE_ACCOUNT_JSON=...
GOOGLE_DRIVE_FOLDER_ID=...
```

Twilio, Railway, and ngrok are **not required** for the webinar path.

---

## Cost Estimate

| Component | Cost |
|-----------|------|
| 1-hour call (AssemblyAI default) | ~$0.54 |
| Claude memo generation | ~$0.02–0.05 |
| Railway hosting | ~$0–5/mo |
| Twilio (inbound/outbound minutes) | ~$0.01–0.02/min |

Total per call: **~$0.60–1.00** depending on length.
