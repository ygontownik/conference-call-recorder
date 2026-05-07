#!/usr/bin/env python3
"""
Teams Call Recorder & Transcriber
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Records system audio (+ optionally mic) on Mac using BlackHole or Loopback,
transcribes via AssemblyAI or Deepgram, generates a structured analytical
memo via Claude, and saves everything to Google Drive.

Replicates Otter AI at ~$0.10–$0.75/call depending on transcription engine.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUICK START:

  # Start recording (will prompt for call title)
  python call_recorder.py start

  # Stop recording + transcribe + generate memo + save to Drive
  python call_recorder.py stop

  # Transcribe an existing audio file
  python call_recorder.py transcribe --file ~/recordings/call.mp3

  # List recent transcribed calls
  python call_recorder.py list

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ONE-TIME SETUP:

  1. Install BlackHole (free virtual audio driver):
       brew install blackhole-2ch
     Then in System Settings → Sound → Output: select "BlackHole 2ch"
     (or use a Multi-Output Device in Audio MIDI Setup — see SETUP_NOTES below)

  2. Install ffmpeg (audio recording):
       brew install ffmpeg

  3. Install Python deps:
       pip install requests anthropic pyaudio soundfile numpy \
           google-auth google-auth-oauthlib google-api-python-client

  4. Set env vars in ~/.zshrc:
       export ASSEMBLYAI_API_KEY="..."
       export DEEPGRAM_API_KEY="..."      # optional — for Deepgram mode
       export ANTHROPIC_API_KEY="..."

  5. AirPods / Loopback users: see LOOPBACK_SETUP below

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TRANSCRIPTION ENGINE:

  Default: AssemblyAI nano (cheapest, ~$0.002/min, good for business English)
  Better:  AssemblyAI default (~$0.009/min, best accuracy + diarization)
  Alt:     Deepgram Nova-2 (~$0.0043/min, good accuracy, cheaper than AAI default)

  Override: python call_recorder.py start --engine assemblyai
            python call_recorder.py start --engine assemblyai-nano
            python call_recorder.py start --engine deepgram

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SETUP NOTES (BlackHole + hearing yourself):

  BlackHole alone routes audio AWAY from your speakers/AirPods — you won't
  hear the call. To fix this, create a Multi-Output Device in Audio MIDI Setup:
    1. Open Audio MIDI Setup (Applications → Utilities)
    2. Click "+" → Create Multi-Output Device
    3. Check both "BlackHole 2ch" AND your normal output (Built-in / AirPods)
    4. Set this Multi-Output Device as your System Output in Sound settings
  Now audio goes to BOTH BlackHole (for recording) AND your ears.

LOOPBACK_SETUP (for AirPods + your own mic voice):

  If you have Loopback ($99, rogueamoeba.com/loopback):
    1. Create a new virtual device in Loopback
    2. Add sources: "System Audio" + your microphone
    3. Use that virtual device name as LOOPBACK_DEVICE_NAME below
    4. Run with: python call_recorder.py start --mic
  This captures ALL audio: other participants + your own voice.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import argparse
import json
import os
import pickle
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests

# Usage logger lives in ~/dashboards/routines/_usage.py
sys.path.insert(0, str(Path.home() / "dashboards" / "routines"))
try:
    from _usage import log_usage  # type: ignore
except Exception:
    def log_usage(*_a, **_k): pass  # fail-open if not importable

# ── Config ────────────────────────────────────────────────────────────────────

# launchd strips PATH to /usr/bin:/bin; resolve ffmpeg explicitly
import shutil as _shutil
FFMPEG = _shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
DEEPGRAM_API_KEY   = os.environ.get("DEEPGRAM_API_KEY", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

# Google Drive
GDRIVE_FOLDER_ID   = "1jYntgSVBsW5-5rdx18TeZhHRsI9xT74p"   # Call Recordings folder
CREDS_PATH         = os.path.expanduser("~/credentials/client_secret.json")
TOKEN_PATH         = os.path.expanduser("~/credentials/gcal_token.json")
DOC_INDEX_PATH     = os.path.expanduser("~/credentials/call_doc_index.json")

# Local storage
RECORDINGS_DIR     = os.path.expanduser("~/recordings/calls")
STATE_FILE         = os.path.expanduser("~/recordings/calls/.recording_state.json")
PROCESSED_PATH     = os.path.expanduser("~/credentials/processed_calls.json")

# Audio device names — update if yours differ
BLACKHOLE_DEVICE   = "BlackHole 2ch"          # system audio capture
LOOPBACK_DEVICE    = "Loopback Audio"          # if using Loopback (system + mic)
MIC_DEVICE         = "MacBook Pro Microphone"  # fallback mic name

# Transcription engine default
DEFAULT_ENGINE     = "assemblyai-nano"         # cheapest; change to "assemblyai" for best accuracy

# Document name for the calls doc in Drive
CALLS_DOC_NAME     = "Call Transcripts & Memos"

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
]

TOC_END_MARKER = "——— END OF TABLE OF CONTENTS ———"


# ── Persistence ───────────────────────────────────────────────────────────────

def load_json(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Audio recording via ffmpeg ────────────────────────────────────────────────

def list_audio_devices() -> list[str]:
    """List available audio input devices via ffmpeg."""
    result = subprocess.run(
        [FFMPEG, "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True
    )
    output = result.stderr
    devices = []
    for line in output.splitlines():
        if "[AVFoundation" in line and "]" in line:
            devices.append(line.strip())
    return devices


def find_device_index(device_name: str) -> str | None:
    """Find the avfoundation device index for a named audio device."""
    result = subprocess.run(
        [FFMPEG, "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True
    )
    lines   = result.stderr.splitlines()
    in_audio = False
    for line in lines:
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if in_audio and device_name.lower() in line.lower():
            # Extract index like [0], [1], etc.
            import re
            m = re.search(r'\[(\d+)\]', line)
            if m:
                return m.group(1)
    return None


def start_recording(output_path: str, device_name: str,
                     mic_device: str | None = None) -> subprocess.Popen:
    """
    Start ffmpeg recording from the specified audio device.
    If mic_device is provided, mixes system audio + mic into one file.
    Returns the ffmpeg process.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    sys_idx = find_device_index(device_name)
    if sys_idx is None:
        print(f"⚠️  Device '{device_name}' not found. Available devices:")
        for d in list_audio_devices():
            print(f"   {d}")
        print(f"
   Is BlackHole installed? Run: brew install blackhole-2ch")
        print(f"   Then set it as your output in System Settings → Sound.")
        sys.exit(1)

    if mic_device:
        mic_idx = find_device_index(mic_device)
        if mic_idx is None:
            print(f"⚠️  Mic device '{mic_device}' not found — recording system audio only.")
            mic_device = None

    if mic_device and mic_idx:
        # Mix system audio + mic into stereo file
        # Left channel = system audio (other participants)
        # Right channel = microphone (your voice)
        cmd = [
            FFMPEG, "-y",
            "-f", "avfoundation", "-i", f":{sys_idx}",   # system audio
            "-f", "avfoundation", "-i", f":{mic_idx}",   # mic
            "-filter_complex", "[0:a][1:a]amerge=inputs=2,pan=stereo|c0<c0|c1<c2[a]",
            "-map", "[a]",
            "-acodec", "libmp3lame", "-q:a", "4",
            output_path
        ]
    else:
        # System audio only
        cmd = [
            FFMPEG, "-y",
            "-f", "avfoundation", "-i", f":{sys_idx}",
            "-acodec", "libmp3lame", "-q:a", "4",
            output_path
        ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def stop_recording(proc: subprocess.Popen):
    """Gracefully stop ffmpeg recording."""
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
        proc.wait(timeout=10)
    except Exception:
        proc.terminate()
        proc.wait()


# ── Transcription — AssemblyAI ────────────────────────────────────────────────

def transcribe_assemblyai(audio_path: str, nano: bool = False) -> dict:
    """Upload file to AssemblyAI and poll for transcript with speaker diarization."""
    if not ASSEMBLYAI_API_KEY:
        sys.exit("❌  ASSEMBLYAI_API_KEY not set.")

    headers = {"authorization": ASSEMBLYAI_API_KEY}
    base    = "https://api.assemblyai.com/v2"

    # Upload
    print("  → Uploading to AssemblyAI…")
    with open(audio_path, "rb") as f:
        upload_resp = requests.post(
            f"{base}/upload",
            headers=headers,
            data=f,
            timeout=300,
        )
    upload_resp.raise_for_status()
    upload_url = upload_resp.json()["upload_url"]

    # Submit
    payload = {
        "audio_url":     upload_url,
        "speaker_labels": True,
    }
    if nano:
        payload["speech_models"] = ["universal-2"]

    submit_resp = requests.post(
        f"{base}/transcript",
        json=payload,
        headers={**headers, "content-type": "application/json"},
        timeout=30,
    )
    submit_resp.raise_for_status()
    tid = submit_resp.json()["id"]
    print(f"  → Transcript ID: {tid}")

    # Poll
    for attempt in range(240):
        time.sleep(10)
        r    = requests.get(f"{base}/transcript/{tid}", headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        if attempt % 6 == 0:
            print(f"  → {data['status']} ({attempt * 10}s)")
        if data["status"] == "completed":
            return data
        if data["status"] == "error":
            raise RuntimeError(f"AssemblyAI error: {data.get('error')}")
    raise RuntimeError("AssemblyAI timed out.")


# ── Transcription — Deepgram ──────────────────────────────────────────────────

def transcribe_deepgram(audio_path: str) -> dict:
    """Transcribe via Deepgram Nova-2 with diarization. Returns AAI-compatible dict."""
    if not DEEPGRAM_API_KEY:
        sys.exit("❌  DEEPGRAM_API_KEY not set.")

    print("  → Uploading to Deepgram…")
    url = "https://api.deepgram.com/v1/listen"
    params = {
        "model":       "nova-2",
        "diarize":     "true",
        "punctuate":   "true",
        "smart_format": "true",
        "language":    "en",
    }
    headers = {
        "Authorization": f"Token {DEEPGRAM_API_KEY}",
        "Content-Type":  "audio/mpeg",
    }
    with open(audio_path, "rb") as f:
        resp = requests.post(url, params=params, headers=headers,
                             data=f, timeout=300)
    resp.raise_for_status()
    dg = resp.json()

    # Convert Deepgram format → AssemblyAI-compatible format for downstream use
    words      = dg["results"]["channels"][0]["alternatives"][0].get("words", [])
    full_text  = dg["results"]["channels"][0]["alternatives"][0].get("transcript", "")
    duration   = dg["metadata"].get("duration", 0)

    # Build utterances by grouping consecutive words from same speaker
    utterances = []
    if words:
        current_speaker = words[0].get("speaker", 0)
        current_words   = [words[0]]

        for word in words[1:]:
            spk = word.get("speaker", 0)
            if spk == current_speaker:
                current_words.append(word)
            else:
                utterances.append({
                    "speaker": str(current_speaker),
                    "text":    " ".join(w["word"] for w in current_words),
                    "start":   int(current_words[0]["start"] * 1000),
                    "end":     int(current_words[-1]["end"]  * 1000),
                })
                current_speaker = spk
                current_words   = [word]

        if current_words:
            utterances.append({
                "speaker": str(current_speaker),
                "text":    " ".join(w["word"] for w in current_words),
                "start":   int(current_words[0]["start"] * 1000),
                "end":     int(current_words[-1]["end"]  * 1000),
            })

    return {
        "status":         "completed",
        "text":           full_text,
        "utterances":     utterances,
        "audio_duration": duration,
        "_engine":        "deepgram",
    }


def transcribe(audio_path: str, engine: str) -> dict:
    """Route to the correct transcription engine."""
    print(f"  → Transcribing with {engine}…")
    if engine == "assemblyai":
        return transcribe_assemblyai(audio_path, nano=False)
    elif engine == "assemblyai-nano":
        return transcribe_assemblyai(audio_path, nano=True)
    elif engine == "deepgram":
        return transcribe_deepgram(audio_path)
    else:
        sys.exit(f"❌  Unknown engine: {engine}. Use assemblyai, assemblyai-nano, or deepgram.")


# ── Transcript formatter ──────────────────────────────────────────────────────

def format_transcript_block(data: dict, call_title: str,
                              start_time: datetime) -> str:
    duration = round(data.get("audio_duration", 0) / 60, 1)
    engine   = data.get("_engine", "assemblyai")
    lines    = [
        f"Call:        {call_title}",
        f"Date:        {start_time.strftime('%Y-%m-%d')}",
        f"Started:     {start_time.strftime('%H:%M')}",
        f"Duration:    {duration} min",
        f"Engine:      {engine}",
        f"Transcribed: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "FULL TRANSCRIPT",
        "─" * 40,
        "",
    ]
    utterances = data.get("utterances", [])
    if not utterances:
        lines.append(data.get("text", "(no transcript text returned)"))
    else:
        for utt in utterances:
            spk   = utt.get("speaker", "?")
            text  = utt.get("text", "").strip()
            start = utt.get("start", 0) // 1000
            ts    = f"[{start // 60}:{start % 60:02d}]"
            lines.append(f"Speaker {spk} {ts}:  {text}")
            lines.append("")
    return "
".join(lines)


# ── Claude analytical memo ────────────────────────────────────────────────────

MEMO_PREAMBLE = """\
You are a senior infrastructure private equity analyst. You have just \
participated in a call and read the full transcript. Produce a structured \
analytical memo — the kind a managing director would read in 3-5 minutes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Write the memo using EXACTLY these six sections with these EXACT headings.
Be direct. Use specific names, numbers, and firm names from the transcript.
Do not generalize when specifics exist.

ONE-SENTENCE SUMMARY
A single crisp sentence (max 25 words) capturing the call's core point.
This will appear as a hyperlink label in the table of contents.

THE CORE ARGUMENT
One to two paragraphs. What was the central purpose or thesis of the call?
What is the overarching conclusion or decision reached?

POINTS OF CONSENSUS
Bullet points. What did participants clearly agree on? Attribute by name.

POINTS OF DISAGREEMENT OR TENSION
Bullet points. Where was there pushback, hedging, or unresolved tension?
What was conspicuously vague where specifics were expected?

OPEN QUESTIONS AND UNRESOLVED ISSUES
Bullet points. What was explicitly left open? Pending decisions, missing
data, follow-up items, regulatory or timing dependencies.

WHAT YOU WOULD NEED TO FORM A VIEW
Bullet points. If this call touched on an investment or business decision —
what specific data, diligence questions, or follow-up conversations are
needed before acting? This is the actionable bridge.

KEY NAMES AND FIRMS
Every person and organization named, one line each.
Format: Name / Firm — context.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

MEMO_DYNAMIC_TEMPLATE = """\
Call: {title}
Date: {date}

Full transcript:
{transcript}
"""


def generate_memo(title: str, date: datetime,
                  transcript_text: str) -> tuple[str, str]:
    """Returns (full_memo, one_sentence_summary)."""
    if not ANTHROPIC_API_KEY:
        fallback = "(memo skipped — ANTHROPIC_API_KEY not set)"
        return fallback, "No summary available."

    dynamic = MEMO_DYNAMIC_TEMPLATE.format(
        title=title,
        date=date.strftime("%Y-%m-%d"),
        transcript=transcript_text[:40000],
    )
    headers = {
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    body = {
        "model":      "claude-sonnet-4-6",
        "max_tokens": 2048,
        "messages":   [{
            "role": "user",
            "content": [
                {"type": "text", "text": MEMO_PREAMBLE,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": dynamic},
            ],
        }],
    }
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers=headers, json=body, timeout=90)
        r.raise_for_status()
        resp_json = r.json()
        log_usage("call_recorder", body["model"], resp_json)
        memo = resp_json["content"][0]["text"].strip()
    except Exception as e:
        fallback = f"(memo generation failed: {e})"
        return fallback, "Summary unavailable."

    # Extract one-liner
    one_liner = _extract_one_liner(memo)
    return memo, one_liner


def _extract_one_liner(memo: str) -> str:
    lines  = memo.splitlines()
    in_sec = False
    for line in lines:
        s = line.strip()
        if s.upper() == "ONE-SENTENCE SUMMARY":
            in_sec = True
            continue
        if in_sec:
            if s and not s.upper().startswith("THE CORE"):
                return s
    for line in lines:
        if line.strip():
            return line.strip()[:200]
    return "No summary available."


# ── Google auth ───────────────────────────────────────────────────────────────

def get_services():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDS_PATH):
                sys.exit(
                    f"❌  {CREDS_PATH} not found.
"
                    "    Download OAuth 2.0 Desktop credentials from Google Cloud Console."
                )
            flow  = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds), build("docs", "v1", credentials=creds)


# ── Google Docs helpers ───────────────────────────────────────────────────────

def batch_update(docs_svc, doc_id: str, reqs: list):
    if reqs:
        docs_svc.documents().batchUpdate(
            documentId=doc_id, body={"requests": reqs}
        ).execute()


def get_content(docs_svc, doc_id: str) -> list:
    return docs_svc.documents().get(documentId=doc_id).execute().get(
        "body", {}
    ).get("content", [])


def get_end_index(content: list) -> int:
    return max(1, content[-1].get("endIndex", 2) - 1) if content else 1


def para_text(el: dict) -> str:
    return "".join(
        r.get("textRun", {}).get("content", "")
        for r in el.get("paragraph", {}).get("elements", [])
    ).strip()


def para_style(el: dict) -> str:
    return el.get("paragraph", {}).get("paragraphStyle", {}).get("namedStyleType", "")


def apply_para_style(docs_svc, doc_id: str, content: list,
                     target: str, style: str, nth: str = "last"):
    matches = [el for el in content if para_text(el) == target]
    if not matches:
        return
    el    = matches[-1] if nth == "last" else matches[0]
    start = el.get("startIndex", 1)
    end   = el.get("endIndex", start + len(target) + 1)
    batch_update(docs_svc, doc_id, [{
        "updateParagraphStyle": {
            "range":          {"startIndex": start, "endIndex": end},
            "paragraphStyle": {"namedStyleType": style},
            "fields":         "namedStyleType",
        }
    }])


def get_or_create_doc(drive_svc, doc_index: dict, key: str, name: str) -> str:
    if key in doc_index:
        return doc_index[key]
    meta = {
        "name":     name,
        "mimeType": "application/vnd.google-apps.document",
        "parents":  [GDRIVE_FOLDER_ID],
    }
    doc_id = drive_svc.files().create(body=meta, fields="id").execute()["id"]
    doc_index[key] = doc_id
    save_json(DOC_INDEX_PATH, doc_index)
    print(f"    → Created Google Doc: '{name}'")
    return doc_id


def ensure_toc_block(docs_svc, doc_id: str):
    content    = get_content(docs_svc, doc_id)
    first_text = para_text(content[0]) if content else ""
    if first_text.upper() == "TABLE OF CONTENTS":
        return
    label = "TABLE OF CONTENTS"
    batch_update(docs_svc, doc_id, [
        {"insertText": {"location": {"index": 1},
                        "text": label + "
" + TOC_END_MARKER + "

"}},
        {"updateParagraphStyle": {
            "range":          {"startIndex": 1, "endIndex": 1 + len(label)},
            "paragraphStyle": {"namedStyleType": "HEADING_1"},
            "fields":         "namedStyleType",
        }},
    ])


def get_toc_end_index(content: list) -> int | None:
    for el in content:
        if TOC_END_MARKER in para_text(el):
            return el.get("startIndex")
    return None


def insert_bookmark(docs_svc, doc_id: str, index: int, bm_id: str):
    batch_update(docs_svc, doc_id, [{
        "createNamedRange": {
            "name":  bm_id,
            "range": {"startIndex": index, "endIndex": index + 1},
        }
    }])


def get_named_range_id(docs_svc, doc_id: str, bm_name: str) -> str | None:
    doc   = docs_svc.documents().get(documentId=doc_id).execute()
    entry = doc.get("namedRanges", {}).get(bm_name, {})
    nrs   = entry.get("namedRanges", [])
    return nrs[0].get("namedRangeId") if nrs else None


def prepend_toc_entry(docs_svc, doc_id: str,
                       date_label: str, title: str,
                       one_liner: str, named_range_id: str):
    """Insert a TOC entry (hyperlinked title + italic one-liner) newest first."""
    content    = get_content(docs_svc, doc_id)
    toc_end    = get_toc_end_index(content)
    if toc_end is None:
        return

    # Find insertion point: right after HEADING_1 "TABLE OF CONTENTS"
    insert_idx = toc_end
    for el in content:
        if para_style(el) == "HEADING_1" and para_text(el) == "TABLE OF CONTENTS":
            insert_idx = el.get("endIndex", toc_end)
            break

    entry_line   = f"{date_label} — {title}
"
    summary_line = f"                {one_liner}
"

    batch_update(docs_svc, doc_id, [
        {"insertText": {"location": {"index": insert_idx},
                        "text": entry_line + summary_line}}
    ])

    # Hyperlink the entry line
    link_end = insert_idx + len(entry_line) - 1
    batch_update(docs_svc, doc_id, [{
        "updateTextStyle": {
            "range": {"startIndex": insert_idx, "endIndex": link_end},
            "textStyle": {
                "link":      {"bookmarkId": named_range_id},
                "foregroundColor": {
                    "color": {"rgbColor": {"red": 0.067, "green": 0.396, "blue": 0.753}}
                },
                "underline": True,
            },
            "fields": "link,foregroundColor,underline",
        }
    }])

    # Italic + grey for summary line
    sum_start = insert_idx + len(entry_line)
    sum_end   = sum_start + len(summary_line) - 1
    batch_update(docs_svc, doc_id, [{
        "updateTextStyle": {
            "range": {"startIndex": sum_start, "endIndex": sum_end},
            "textStyle": {
                "italic": True,
                "foregroundColor": {
                    "color": {"rgbColor": {"red": 0.4, "green": 0.4, "blue": 0.4}}
                },
            },
            "fields": "italic,foregroundColor",
        }
    }])


def write_call_to_doc(docs_svc, doc_id: str,
                       call_title: str, start_time: datetime,
                       memo: str, transcript_block: str) -> str:
    """
    Prepend call entry after TOC block. Returns bookmark_id.
    Structure:
      HEADING_1: Call Title  (MMM DD YYYY HH:MM)
        [memo — six sections]
        ─── separator ───
        FULL TRANSCRIPT
      ═══ section end ═══
    """
    import re
    dt_label   = start_time.strftime("%b %d %Y %H:%M")
    ep_heading = f"{call_title}  ({dt_label})"
    bm_id      = re.sub(r"[^a-zA-Z0-9_]", "_",
                         f"call_{call_title}_{start_time.strftime('%Y%m%d_%H%M')}")[:100]
    separator  = "
" + "─" * 60 + "

"
    section_end = "
" + "═" * 60 + "

"

    full_block = (
        f"{ep_heading}
"
        f"{memo}
"
        f"{separator}"
        f"{transcript_block}"
        f"{section_end}"
    )

    content    = get_content(docs_svc, doc_id)
    toc_end    = get_toc_end_index(content)
    insert_idx = toc_end if toc_end else get_end_index(content)

    # Find end of TOC block (after TOC_END_MARKER line)
    for el in content:
        if TOC_END_MARKER in para_text(el):
            insert_idx = el.get("endIndex", insert_idx)
            break

    batch_update(docs_svc, doc_id, [
        {"insertText": {"location": {"index": insert_idx}, "text": full_block}}
    ])

    # Bookmark + style heading
    content2   = get_content(docs_svc, doc_id)
    ep_matches = [el for el in content2 if para_text(el) == ep_heading]
    if ep_matches:
        bm_idx = ep_matches[-1].get("startIndex", insert_idx)
        insert_bookmark(docs_svc, doc_id, bm_idx, bm_id)

    content3 = get_content(docs_svc, doc_id)
    apply_para_style(docs_svc, doc_id, content3, ep_heading, "HEADING_1")

    memo_headers = [
        "ONE-SENTENCE SUMMARY", "THE CORE ARGUMENT", "POINTS OF CONSENSUS",
        "POINTS OF DISAGREEMENT OR TENSION", "OPEN QUESTIONS AND UNRESOLVED ISSUES",
        "WHAT YOU WOULD NEED TO FORM A VIEW", "KEY NAMES AND FIRMS", "FULL TRANSCRIPT",
    ]
    for header in memo_headers:
        c = get_content(docs_svc, doc_id)
        apply_para_style(docs_svc, doc_id, c, header, "HEADING_3")

    return bm_id


# ── Cost estimator ────────────────────────────────────────────────────────────

COST_PER_MIN = {
    "assemblyai":      0.009,
    "assemblyai-nano": 0.002,
    "deepgram":        0.0043,
}

def estimate_cost(duration_secs: float, engine: str) -> str:
    mins = duration_secs / 60
    rate = COST_PER_MIN.get(engine, 0.009)
    return f"~${mins * rate:.3f}  ({mins:.1f} min @ ${rate}/min {engine})"


# ── CLI commands ──────────────────────────────────────────────────────────────

def cmd_start(args):
    """Start recording system audio."""
    # Prompt for call title
    if args.title:
        title = args.title
    else:
        title = input("Call title (press Enter to use timestamp): ").strip()
        if not title:
            title = f"Call {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    engine     = args.engine or DEFAULT_ENGINE
    use_mic    = args.mic
    device     = LOOPBACK_DEVICE if use_mic else BLACKHOLE_DEVICE

    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_title = "".join(c if c.isalnum() or c in " -_()" else "_" for c in title)[:60]
    out_path   = os.path.join(RECORDINGS_DIR, f"{timestamp}_{safe_title}.mp3")

    print(f"
🎙  Starting recording")
    print(f"    Title:   {title}")
    print(f"    Device:  {device}")
    print(f"    Engine:  {engine} (will use at stop)")
    print(f"    Mic mix: {'yes' if use_mic else 'no — system audio only'}")
    print(f"    Output:  {out_path}")
    print(f"
    ⚠️  Make sure '{device}' is set as your Mac audio output.")
    print(f"    Press Ctrl+C or run 'python call_recorder.py stop' to end.
")

    mic_device = MIC_DEVICE if use_mic else None
    proc       = start_recording(out_path, device, mic_device)

    # Save state so `stop` knows what to do
    state = {
        "pid":        proc.pid,
        "title":      title,
        "engine":     engine,
        "audio_path": out_path,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    save_json(STATE_FILE, state)
    print(f"✅  Recording started (PID {proc.pid}). Run 'python call_recorder.py stop' when done.")

    # Keep alive if running interactively
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("
  Stopping…")
        stop_recording(proc)
        _process_recording(state)


def cmd_stop(args):
    """Stop the active recording and process it."""
    state = load_json(STATE_FILE)
    if not state:
        print("❌  No active recording found.")
        return

    audio_path = state.get("audio_path", "")

    # Kill the PID saved at start time
    pid = state.get("pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
        except ProcessLookupError:
            pass

    # Fallback: kill any ffmpeg process writing to the same file (handles PID drift)
    if audio_path:
        try:
            result = subprocess.run(
                ["lsof", "-t", audio_path],
                capture_output=True, text=True
            )
            for lsof_pid in result.stdout.strip().splitlines():
                try:
                    lsof_pid = int(lsof_pid)
                    if lsof_pid != pid:
                        os.kill(lsof_pid, signal.SIGTERM)
                        print(f"  Stopped stale ffmpeg process (PID {lsof_pid})")
                        time.sleep(1)
                except (ValueError, ProcessLookupError):
                    pass
        except FileNotFoundError:
            pass  # lsof not available

    print(f"⏹  Recording stopped.")
    try:
        _process_recording(state)
    except Exception as e:
        print(f"❌  Processing failed: {e}")
    finally:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)


def _process_recording(state: dict):
    """Transcribe, generate memo, and save to Drive."""
    title      = state.get("title", "Untitled Call")
    engine     = state.get("engine", DEFAULT_ENGINE)
    audio_path = state.get("audio_path", "")
    started_at = state.get("started_at", datetime.now(timezone.utc).isoformat())
    start_time = datetime.fromisoformat(started_at.replace("Z", "+00:00"))

    if not os.path.exists(audio_path):
        print(f"❌  Audio file not found: {audio_path}")
        return

    file_size = os.path.getsize(audio_path) / 1024 / 1024
    print(f"
📼  Processing: {title}")
    print(f"    File: {audio_path} ({file_size:.1f} MB)")

    # 1. Transcribe
    try:
        aai_data = transcribe(audio_path, engine)
    except Exception as e:
        print(f"❌  Transcription failed: {e}")
        traceback.print_exc()
        return

    duration     = aai_data.get("audio_duration", 0)
    cost_str     = estimate_cost(duration, engine)
    print(f"    Duration: {duration/60:.1f} min  |  Cost: {cost_str}")

    # 2. Format transcript
    transcript_block = format_transcript_block(aai_data, title, start_time)

    # 3. Generate memo
    print("  → Generating analytical memo…")
    raw_text        = aai_data.get("text", "") or transcript_block
    memo, one_liner = generate_memo(title, start_time, raw_text)

    # 4. Save to Google Drive
    print("  → Saving to Google Drive…")
    try:
        drive_svc, docs_svc = get_services()
        doc_index           = load_json(DOC_INDEX_PATH)

        doc_id = get_or_create_doc(drive_svc, doc_index, "__calls__", CALLS_DOC_NAME)

        ensure_toc_block(docs_svc, doc_id)
        bm_id = write_call_to_doc(
            docs_svc, doc_id, title, start_time, memo, transcript_block
        )

        # Add TOC entry with hyperlink
        nr_id = get_named_range_id(docs_svc, doc_id, bm_id)
        if nr_id:
            date_label = start_time.strftime("%b %d %Y %H:%M")
            prepend_toc_entry(docs_svc, doc_id, date_label, title, one_liner, nr_id)

    except Exception as e:
        print(f"❌  Drive save failed: {e}")
        traceback.print_exc()
        # Save locally as fallback
        fallback_path = audio_path.replace(".mp3", "_transcript.txt")
        with open(fallback_path, "w") as f:
            f.write(f"MEMO
{'='*60}
{memo}

{'='*60}

{transcript_block}")
        print(f"    ⚠️  Saved locally as fallback: {fallback_path}")
        return

    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"

    # Mark processed
    call_id = f"{title}_{start_time.isoformat()}"
    p       = load_json(PROCESSED_PATH)
    p[call_id] = {
        "title":      title,
        "date":       start_time.isoformat(),
        "engine":     engine,
        "audio_path": audio_path,
        "doc_url":    doc_url,
        "cost":       cost_str,
    }
    save_json(PROCESSED_PATH, p)

    # Trigger COS follow-up extraction in background — non-blocking, ~$0.003/call
    try:
        subprocess.Popen(
            [sys.executable, os.path.expanduser("~/tomac-cove-pipeline/cos_transcript_hook.py"),
             "--doc-id", doc_id, "--title", title, "--category", "auto"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    print(f"
✅  Done!")
    print(f"    {doc_url}")
    print(f"    Cost: {cost_str}")


def cmd_transcribe(args):
    """Transcribe an existing audio file."""
    if not args.file:
        print("❌  Provide --file path")
        return
    title  = args.title or Path(args.file).stem
    engine = args.engine or DEFAULT_ENGINE
    state  = {
        "title":      title,
        "engine":     engine,
        "audio_path": args.file,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _process_recording(state)


def cmd_list(args):
    """List recent transcribed calls."""
    processed = load_json(PROCESSED_PATH)
    if not processed:
        print("No calls processed yet.")
        return
    print(f"
{'─'*70}")
    print(f"  TRANSCRIBED CALLS ({len(processed)} total)")
    print(f"{'─'*70}")
    for call_id, meta in sorted(processed.items(),
                                 key=lambda x: x[1].get("date", ""),
                                 reverse=True)[:20]:
        print(f"  {meta.get('date','')[:16]}  {meta.get('title','')[:50]}")
        print(f"             {meta.get('cost','')}  →  {meta.get('doc_url','')}")
    doc_index = load_json(DOC_INDEX_PATH)
    if "__calls__" in doc_index:
        print(f"
  All calls doc:")
        print(f"  https://docs.google.com/document/d/{doc_index['__calls__']}/edit")


def cmd_devices(args):
    """List available audio devices to help with setup."""
    print("
Available audio devices (ffmpeg avfoundation):
")
    devices = list_audio_devices()
    for d in devices:
        print(f"  {d}")
    print(f"
  Looking for: '{BLACKHOLE_DEVICE}' or '{LOOPBACK_DEVICE}'")
    print(f"  If BlackHole is missing: brew install blackhole-2ch")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Record Teams/Zoom/Meet calls and transcribe to Google Drive"
    )
    sub = parser.add_subparsers(dest="command")

    # start
    p_start = sub.add_parser("start", help="Start recording")
    p_start.add_argument("--title",  help="Call title (prompted if omitted)")
    p_start.add_argument("--engine", choices=["assemblyai", "assemblyai-nano", "deepgram"],
                          help=f"Transcription engine (default: {DEFAULT_ENGINE})")
    p_start.add_argument("--mic",    action="store_true",
                          help="Mix microphone input (requires Loopback)")

    # stop
    p_stop = sub.add_parser("stop", help="Stop recording and process")

    # transcribe
    p_tx = sub.add_parser("transcribe", help="Transcribe an existing audio file")
    p_tx.add_argument("--file",   required=True, help="Path to audio file")
    p_tx.add_argument("--title",  help="Call title")
    p_tx.add_argument("--engine", choices=["assemblyai", "assemblyai-nano", "deepgram"],
                       help=f"Transcription engine (default: {DEFAULT_ENGINE})")

    # list
    sub.add_parser("list",    help="List recent transcribed calls")

    # devices
    sub.add_parser("devices", help="List audio devices (for setup)")

    args = parser.parse_args()

    if args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "transcribe":
        cmd_transcribe(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "devices":
        cmd_devices(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
