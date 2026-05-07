# Conference Call Recorder

Automatically records conference calls (Teams, Zoom, Google Meet, or any dial-in) on Mac, transcribes via AssemblyAI, generates a structured memo via Claude, and saves everything to Google Drive.

**Stack:** Twilio · Railway · AssemblyAI · Claude (Anthropic) · Google Drive

---

## How It Works

```
Google Calendar / Outlook
        ↓  (push notification)
Railway (webhook_server.py)
        ↓  (forwards to your Mac)
call_scheduler.py (Mac)
        ↓  (schedules launchd job)
call_recorder.py (Mac)
        ↓  records audio via BlackHole
AssemblyAI  →  transcript
Claude      →  structured memo
Google Drive → saved
```

1. Your calendar sends a push notification to Railway when a meeting is created or updated.
2. Railway forwards it to your Mac (via a public tunnel URL).
3. The Mac scheduler creates a launchd job timed to start recording when the call begins.
4. `call_recorder.py` captures system audio (BlackHole virtual audio driver), sends it to AssemblyAI for transcription, generates a memo via Claude, and uploads both to Google Drive.

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

## Cost Estimate

| Component | Cost |
|-----------|------|
| 1-hour call (AssemblyAI default) | ~$0.54 |
| Claude memo generation | ~$0.02–0.05 |
| Railway hosting | ~$0–5/mo |
| Twilio (inbound/outbound minutes) | ~$0.01–0.02/min |

Total per call: **~$0.60–1.00** depending on length.
