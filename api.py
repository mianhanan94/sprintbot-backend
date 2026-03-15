import os
import json
import time
import base64
import asyncio
import logging
import requests
from datetime import datetime
from typing import Any

from fastapi import FastAPI, BackgroundTasks, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import uvicorn

from main import run_sprintbot, generate_audio_summary
from live_session import LiveSession
from firestore_client import (
    save_meeting,
    update_participant_history,
    get_meeting,
    list_meetings,
    get_participant_history,
)

load_dotenv()

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sprintbot")

# ── Config ───────────────────────────────────────────────────
RECALL_API_BASE = os.getenv("RECALL_API_BASE", "https://us-west-2.recall.ai/api/v1")
# Cloud Run mounts a writable /tmp; locally falls back to sprintbot_results/
RESULTS_DIR = os.getenv("RESULTS_DIR", "sprintbot_results")

# Transcript polling: wait up to ~2 min (8 × 15 s)
TRANSCRIPT_MAX_RETRIES = 8
TRANSCRIPT_RETRY_DELAY = 15  # seconds

# Recall.ai fires these when the meeting/recording is fully done
TERMINAL_EVENTS = {"bot.done", "recording.done"}

# In-memory dedup set — prevents double-processing when Recall.ai retries the webhook
_processed_bots: set[str] = set()


def _recall_headers() -> dict:
    key = os.getenv("RECALL_API_KEY")
    if not key:
        raise RuntimeError("RECALL_API_KEY is not configured")
    return {
        "Authorization": key,
        "accept": "application/json",
        "Content-Type": "application/json",
    }


# ── App ───────────────────────────────────────────────────────
app = FastAPI(title="SprintBot API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
os.makedirs(RESULTS_DIR, exist_ok=True)


# ═════════════════════════════════════════════════════════════
# BOT — create a Recall.ai bot to join a meeting
# ═════════════════════════════════════════════════════════════

class CreateBotRequest(BaseModel):
    meeting_url: str
    bot_name: str = "SprintBot"


@app.post("/bot", status_code=201)
def create_bot(body: CreateBotRequest):
    """Send a Recall.ai bot to join and transcribe a meeting."""
    payload = {
        "meeting_url": body.meeting_url,
        "bot_name": body.bot_name,
        "recording_config": {
            "transcript": {
                "provider": {"recallai_async": {}}
            }
        },
    }
    try:
        resp = requests.post(
            f"{RECALL_API_BASE}/bot/",
            json=payload,
            headers=_recall_headers(),
            timeout=15,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Failed to reach Recall.ai: {e}")

    if not resp.ok:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    data = resp.json()
    bot_id = data.get("id")
    log.info("Bot created: bot_id=%s meeting=%s", bot_id, body.meeting_url)
    return {"bot_id": bot_id, "meeting_url": body.meeting_url}


# ═════════════════════════════════════════════════════════════
# TRANSCRIPT — fetch + parse from Recall.ai → S3
# ═════════════════════════════════════════════════════════════

def _get_download_url(bot_id: str) -> str | None:
    """
    Call GET /bot/{bot_id}/ and extract the transcript download_url.
    Returns None if the transcript is still processing.
    Raises RuntimeError on hard failures (API error, transcript failed).
    """
    resp = requests.get(
        f"{RECALL_API_BASE}/bot/{bot_id}/",
        headers=_recall_headers(),
        timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"Bot fetch failed ({resp.status_code}): {resp.text}")

    bot_data = resp.json()
    recordings = bot_data.get("recordings") or []

    for recording in reversed(recordings):
        transcript_block = (
            recording
            .get("media_shortcuts", {})
            .get("transcript", {})
        )
        status_code = transcript_block.get("status", {}).get("code", "")
        download_url = transcript_block.get("data", {}).get("download_url")

        log.debug(
            "bot_id=%s recording=%s transcript_status=%s has_url=%s",
            bot_id,
            recording.get("id"),
            status_code,
            bool(download_url),
        )

        if download_url:
            return download_url

        # Hard failure — stop retrying immediately
        if status_code in ("failed", "error"):
            raise RuntimeError(
                f"Transcript processing failed for bot {bot_id}: status={status_code}"
            )

    return None  # still processing


def _fetch_speaker_names(bot_id: str) -> dict[str, str]:
    """Fetch participant names from Recall.ai bot data and build a speaker_id → name map."""
    try:
        resp = requests.get(
            f"{RECALL_API_BASE}/bot/{bot_id}/",
            headers=_recall_headers(),
            timeout=15,
        )
        if not resp.ok:
            return {}
        bot_data = resp.json()
        name_map = {}
        for participant in bot_data.get("meeting_participants", []):
            speaker_id = participant.get("id")
            name = participant.get("name", "").strip()
            if speaker_id is not None and name:
                name_map[str(speaker_id)] = name
                name_map[speaker_id] = name  # handle both int and str keys
        log.info("Speaker name map for bot_id=%s: %s", bot_id, name_map)
        return name_map
    except Exception as e:
        log.warning("Could not fetch speaker names for bot_id=%s: %s", bot_id, e)
        return {}


def _download_and_parse(download_url: str, speaker_names: dict[str, str] | None = None) -> str:
    """Download transcript JSON from S3 and convert to plain speaker: text lines."""
    resp = requests.get(download_url, timeout=30)
    if not resp.ok:
        raise RuntimeError(f"S3 download failed ({resp.status_code})")

    segments = resp.json()
    if not isinstance(segments, list):
        raise RuntimeError(f"Unexpected transcript format: {type(segments)}")

    speaker_names = speaker_names or {}
    lines = []
    for seg in segments:
        raw_speaker = seg.get("speaker", "Unknown")
        # Try to resolve speaker ID to real name
        speaker = speaker_names.get(str(raw_speaker), speaker_names.get(raw_speaker, raw_speaker))
        if speaker in (None, "", "Unknown", 0):
            speaker = "Unknown"
        words = seg.get("words", [])
        text = " ".join(w.get("text", "") for w in words).strip()
        if text:
            lines.append(f"{speaker}: {text}")

    return "\n".join(lines)


def _fetch_transcript_with_retry(bot_id: str) -> str:
    """
    Poll until the transcript download_url is available, then download and parse.
    Retries TRANSCRIPT_MAX_RETRIES times, sleeping TRANSCRIPT_RETRY_DELAY seconds between.
    """
    # Fetch speaker names from Recall.ai participant data
    speaker_names = _fetch_speaker_names(bot_id)

    for attempt in range(1, TRANSCRIPT_MAX_RETRIES + 1):
        log.info(
            "Fetching transcript: bot_id=%s attempt=%d/%d",
            bot_id, attempt, TRANSCRIPT_MAX_RETRIES,
        )

        download_url = _get_download_url(bot_id)

        if download_url:
            log.info("Transcript URL found on attempt %d, downloading...", attempt)
            return _download_and_parse(download_url, speaker_names)

        if attempt < TRANSCRIPT_MAX_RETRIES:
            log.info("Transcript not ready yet, retrying in %ds...", TRANSCRIPT_RETRY_DELAY)
            time.sleep(TRANSCRIPT_RETRY_DELAY)

    raise RuntimeError(
        f"Transcript not available after {TRANSCRIPT_MAX_RETRIES} attempts "
        f"({TRANSCRIPT_MAX_RETRIES * TRANSCRIPT_RETRY_DELAY}s total)"
    )


# ═════════════════════════════════════════════════════════════
# PIPELINE — transcript → SprintBot → save
# ═════════════════════════════════════════════════════════════

def _save_result(bot_id: str, transcript: str, result: dict) -> str:
    """Persist transcript + analysis to a JSON file. Returns the file path."""
    filename = os.path.join(
        RESULTS_DIR,
        f"{bot_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
    )
    data = {
        "bot_id": bot_id,
        "timestamp": datetime.now().isoformat(),
        "transcript": transcript,
        # result shape from run_sprintbot:
        # {"analysis": {...gemini...}, "jira_board": {...}, "tickets_created": [...], "slack_result": {...}}
        "analysis": result,
    }
    with open(filename, "w") as f:
        json.dump(data, f, indent=2)
    log.info("Result saved: %s", filename)
    return filename


def _run_pipeline(bot_id: str):
    """Background task: fetch transcript → run SprintBot → persist result."""
    log.info("Pipeline started: bot_id=%s", bot_id)
    try:
        transcript = _fetch_transcript_with_retry(bot_id)
        log.info("Transcript ready: %d chars, bot_id=%s", len(transcript), bot_id)

        result = run_sprintbot(transcript)

        if result is None:
            raise RuntimeError(
                "run_sprintbot returned None — Gemini likely failed to parse the response. "
                "Check GEMINI_API_KEY and model name."
            )

        saved_to = _save_result(bot_id, transcript, result)

        # Save to Firestore (primary persistent store)
        save_meeting(bot_id, transcript, result)

        # Update per-participant anti-pattern history for repeat offender detection
        analysis = result.get("analysis", {})
        anti_patterns = analysis.get("anti_patterns", [])
        for participant in analysis.get("participants", []):
            name = participant.get("name", "")
            if name:
                participant_aps = [
                    ap for ap in anti_patterns
                    if ap.get("participant") == name and ap.get("type") != "repeat_offender"
                ]
                if participant_aps:
                    update_participant_history(name, participant_aps, bot_id)

        # Generate TTS audio summary
        analysis_data = result.get("analysis", {})
        if analysis_data:
            audio_path = generate_audio_summary(analysis_data, bot_id, RESULTS_DIR)
            if audio_path:
                log.info("TTS audio generated: %s", audio_path)
            else:
                log.warning("TTS audio generation skipped or failed for bot_id=%s", bot_id)

        log.info("Pipeline complete: bot_id=%s saved_to=%s", bot_id, saved_to)

    except Exception as e:
        log.error("Pipeline FAILED: bot_id=%s error=%s", bot_id, e, exc_info=True)
        try:
            _save_result(bot_id, "", {"error": str(e)})
            save_meeting(bot_id, "", {"error": str(e)})
        except Exception as save_err:
            log.error("Could not save error record: %s", save_err)


# ═════════════════════════════════════════════════════════════
# WEBHOOK — receive Recall.ai event, kick off pipeline
# ═════════════════════════════════════════════════════════════

@app.post("/webhook", status_code=200)
def receive_webhook(payload: dict[str, Any], background_tasks: BackgroundTasks):
    """
    Receive Recall.ai webhook. Responds immediately (<1s) and processes in background.
    Deduplicates so Recall.ai retries don't double-process.
    """
    event = payload.get("event", "")
    data = payload.get("data") or {}
    bot_id = data.get("bot_id") or (data.get("bot") or {}).get("id")

    log.info("Webhook received: event=%s bot_id=%s", event, bot_id)

    if event not in TERMINAL_EVENTS:
        log.debug("Event ignored: %s", event)
        return {"status": "ignored", "event": event}

    if not bot_id:
        log.warning("Webhook missing bot_id: %s", payload)
        return {"status": "error", "detail": "No bot_id in payload"}

    if bot_id in _processed_bots:
        log.info("Duplicate webhook ignored: bot_id=%s", bot_id)
        return {"status": "duplicate", "bot_id": bot_id}

    _processed_bots.add(bot_id)
    background_tasks.add_task(_run_pipeline, bot_id)
    log.info("Pipeline queued: bot_id=%s event=%s", bot_id, event)
    return {"status": "accepted", "bot_id": bot_id, "event": event}


# ═════════════════════════════════════════════════════════════
# STATUS — inspect bot state and saved results
# ═════════════════════════════════════════════════════════════

@app.get("/bot/{bot_id}/status")
def get_bot_status(bot_id: str):
    """Check Recall.ai bot status and whether the transcript is ready."""
    try:
        resp = requests.get(
            f"{RECALL_API_BASE}/bot/{bot_id}/",
            headers=_recall_headers(),
            timeout=15,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=str(e))

    if not resp.ok:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    data = resp.json()
    recordings = data.get("recordings") or []

    transcript_status = None
    download_url_ready = False
    for recording in reversed(recordings):
        t = recording.get("media_shortcuts", {}).get("transcript", {})
        transcript_status = t.get("status", {}).get("code")
        download_url_ready = bool(t.get("data", {}).get("download_url"))
        if download_url_ready:
            break

    return {
        "bot_id": bot_id,
        "status_changes": data.get("status_changes", []),
        "transcript_status": transcript_status,
        "transcript_ready": download_url_ready,
        "pipeline_triggered": bot_id in _processed_bots,
    }


@app.get("/results")
def list_results():
    """List all saved SprintBot analysis results (newest first). Reads from Firestore with file fallback."""
    # Try Firestore first
    fs_results = list_meetings()
    if fs_results:
        return {"count": len(fs_results), "results": fs_results, "source": "firestore"}

    # Fallback: read from local files
    files = sorted(
        [f for f in os.listdir(RESULTS_DIR) if f.endswith(".json")],
        reverse=True,
    )
    results = []
    for fname in files:
        path = os.path.join(RESULTS_DIR, fname)
        with open(path) as f:
            data = json.load(f)
        analysis_block = data.get("analysis") or {}
        results.append({
            "bot_id": data.get("bot_id"),
            "timestamp": data.get("timestamp"),
            "health_score": analysis_block.get("analysis", {}).get("meeting_health_score"),
            "error": analysis_block.get("error"),
        })
    return {"count": len(results), "results": results, "source": "local"}


@app.get("/results/{bot_id}")
def get_result(bot_id: str):
    """Get the full SprintBot analysis for a specific bot_id. Reads from Firestore with file fallback."""
    # Try Firestore first
    data = get_meeting(bot_id)
    if data:
        return data

    # Fallback: read from local file
    files = sorted(
        [f for f in os.listdir(RESULTS_DIR) if f.startswith(bot_id) and f.endswith(".json")],
        reverse=True,
    )
    if not files:
        raise HTTPException(status_code=404, detail=f"No result found for bot_id={bot_id}")

    path = os.path.join(RESULTS_DIR, files[0])
    with open(path) as f:
        return json.load(f)


@app.get("/results/{bot_id}/audio")
def get_result_audio(bot_id: str):
    """Get the audio summary (WAV) for a specific bot_id. Generates on-the-fly if not cached."""
    audio_path = os.path.join(RESULTS_DIR, f"{bot_id}_audio.wav")

    # If audio already exists, serve it
    if os.path.exists(audio_path):
        return FileResponse(audio_path, media_type="audio/wav", filename=f"{bot_id}_summary.wav")

    # Otherwise, load analysis and generate on-the-fly
    data = get_meeting(bot_id)
    if not data:
        # Fallback to local file
        files = sorted(
            [f for f in os.listdir(RESULTS_DIR) if f.startswith(bot_id) and f.endswith(".json")],
            reverse=True,
        )
        if not files:
            raise HTTPException(status_code=404, detail=f"No result found for bot_id={bot_id}")
        with open(os.path.join(RESULTS_DIR, files[0])) as f:
            data = json.load(f)

    # Handle both Firestore shape (analysis at top level) and local file shape (nested under analysis.analysis)
    analysis = data.get("analysis", {})
    if isinstance(analysis, dict) and "participants" not in analysis:
        analysis = analysis.get("analysis", {})
    if not analysis or "participants" not in analysis:
        raise HTTPException(status_code=404, detail="No analysis data available to generate audio")

    result_path = generate_audio_summary(analysis, bot_id, RESULTS_DIR)
    if not result_path:
        raise HTTPException(status_code=500, detail="TTS audio generation failed")

    return FileResponse(result_path, media_type="audio/wav", filename=f"{bot_id}_summary.wav")


@app.get("/participants/{name}/history")
def get_participant_history_endpoint(name: str):
    """Get anti-pattern history for a participant across all meetings."""
    history = get_participant_history(name)
    if not history:
        raise HTTPException(status_code=404, detail=f"No history found for participant '{name}'")
    return history


# ═════════════════════════════════════════════════════════════
# WEBHOOKS — list registered Recall.ai webhooks
# ═════════════════════════════════════════════════════════════

@app.get("/webhooks")
def list_webhooks():
    """List all webhooks registered in the Recall.ai account."""
    try:
        resp = requests.get(
            f"{RECALL_API_BASE}/webhook/",
            headers=_recall_headers(),
            timeout=15,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Failed to reach Recall.ai: {e}")

    if not resp.ok:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    return resp.json()


# ═════════════════════════════════════════════════════════════
# TEST ENDPOINTS — verify pipeline without a real meeting
# ═════════════════════════════════════════════════════════════

SAMPLE_TRANSCRIPT = """
Scrum Master: Good morning everyone. Let's start our daily standup. Ahmed, you go first.

Ahmed: Yeah so yesterday I was working on the payment gateway integration. I'm almost done with it, should be finished today or tomorrow. Today I'll continue working on it. No blockers.

Scrum Master: Thanks Ahmed. Sara?

Sara: Yesterday I fixed the login bug, that's done. Today I'm going to start on the dashboard redesign. No blockers from my side.

Scrum Master: Great. Bilal?

Bilal: So I was trying to set up the CI/CD pipeline yesterday. I'm probably going to need some help with the Docker configuration. I'm not sure if the staging environment is ready. I'll try to finish it today but I might need to talk to DevOps.

Scrum Master: Okay. Fatima?

Fatima: I'm still waiting for the client to approve the design files. I've been blocked on this for almost a week now. Yesterday I worked on some documentation instead. Today I'll follow up with the client again but honestly I'm not confident they'll respond quickly.

Scrum Master: That's concerning. Let me escalate that. Ali?

Ali: Yesterday I was reviewing the API documentation and today I'll continue with that. I should be able to finish the review by end of day. No blockers.
"""


@app.post("/test/pipeline")
def test_pipeline():
    """
    Run the full SprintBot pipeline with a built-in sample transcript.
    Tests Gemini analysis, JIRA, and Slack — no Recall.ai or real meeting needed.
    """
    log.info("TEST: Running pipeline with sample transcript")
    result = run_sprintbot(SAMPLE_TRANSCRIPT)
    if result is None:
        raise HTTPException(
            status_code=500,
            detail="run_sprintbot returned None — check GEMINI_API_KEY and model name in main.py",
        )
    bot_id = f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    saved_to = _save_result(bot_id, SAMPLE_TRANSCRIPT, result)

    # Save to Firestore + update participant history (same as _run_pipeline)
    save_meeting(bot_id, SAMPLE_TRANSCRIPT, result)
    analysis = result.get("analysis", {})
    anti_patterns = analysis.get("anti_patterns", [])
    for participant in analysis.get("participants", []):
        name = participant.get("name", "")
        if name:
            participant_aps = [
                ap for ap in anti_patterns
                if ap.get("participant") == name and ap.get("type") != "repeat_offender"
            ]
            if participant_aps:
                update_participant_history(name, participant_aps, bot_id)

    # Generate TTS audio
    analysis_data = result.get("analysis", {})
    audio_path = None
    if analysis_data:
        audio_path = generate_audio_summary(analysis_data, bot_id, RESULTS_DIR)

    return {
        "status": "ok",
        "bot_id": bot_id,
        "saved_to": saved_to,
        "audio_path": audio_path,
        "audio_url": f"/results/{bot_id}/audio" if audio_path else None,
        "result": result,
    }


@app.post("/test/webhook")
def test_webhook(background_tasks: BackgroundTasks, bot_id: str):
    """
    Simulate a bot.done webhook for a real bot_id that already has a completed recording.
    Triggers the full transcript fetch + SprintBot pipeline in the background.
    Usage: POST /test/webhook?bot_id=<your-bot-id>
    Poll GET /results/{bot_id} to see the result when done.
    """
    log.info("TEST: Simulating bot.done webhook for bot_id=%s", bot_id)

    # Allow re-triggering in test mode
    _processed_bots.discard(bot_id)

    _processed_bots.add(bot_id)
    background_tasks.add_task(_run_pipeline, bot_id)
    return {
        "status": "accepted",
        "bot_id": bot_id,
        "note": "Pipeline running in background. Poll GET /results/{bot_id} for output.",
    }


# ═════════════════════════════════════════════════════════════
# LIVE Q&A — Gemini Live API voice chat via WebSocket
# ═════════════════════════════════════════════════════════════

@app.websocket("/live/{bot_id}")
async def live_qa(websocket: WebSocket, bot_id: str):
    """
    WebSocket endpoint for real-time voice Q&A with SprintBot.

    Protocol (JSON messages over WebSocket):
      Client → Server:
        {"type": "audio", "data": "<base64 PCM 16kHz 16-bit mono>"}
        {"type": "text", "text": "What's Ahmed's status?"}
        {"type": "close"}

      Server → Client:
        {"type": "audio", "data": "<base64 PCM 24kHz 16-bit mono>"}
        {"type": "transcript", "text": "Ahmed is..."}
        {"type": "turn_complete"}
        {"type": "error", "message": "..."}
        {"type": "connected", "bot_id": "..."}
    """
    await websocket.accept()
    log.info("Live Q&A: WebSocket connected for bot_id=%s", bot_id)

    # Load analysis from Firestore
    data = get_meeting(bot_id)
    if not data:
        await websocket.send_json({"type": "error", "message": f"No meeting found for bot_id={bot_id}"})
        await websocket.close()
        return

    analysis = data.get("analysis", {})
    if isinstance(analysis, dict) and "participants" not in analysis:
        analysis = analysis.get("analysis", {})
    if not analysis or "participants" not in analysis:
        await websocket.send_json({"type": "error", "message": "No analysis data available"})
        await websocket.close()
        return

    # Create and connect Gemini Live session
    session = LiveSession(analysis)
    try:
        await session.connect()
        await websocket.send_json({"type": "connected", "bot_id": bot_id})

        # Task to forward Gemini audio responses back to browser
        async def forward_responses():
            log.info("Live Q&A: Forward task started")
            audio_chunk_count = 0
            try:
                async for chunk in session.receive_audio():
                    chunk_type = chunk["type"]
                    if chunk_type == "audio":
                        audio_chunk_count += 1
                        await websocket.send_json({
                            "type": "audio",
                            "data": base64.b64encode(chunk["data"]).decode("ascii"),
                        })
                    elif chunk_type == "transcript":
                        await websocket.send_json(chunk)
                    elif chunk_type == "turn_complete":
                        log.info("Live Q&A: Turn complete, sent %d audio chunks", audio_chunk_count)
                        audio_chunk_count = 0
                        await websocket.send_json(chunk)
                    elif chunk_type == "error":
                        await websocket.send_json(chunk)
                log.info("Live Q&A: Forward task ended")
            except asyncio.CancelledError:
                log.info("Live Q&A: Forward task cancelled")
            except Exception as e:
                log.error("Live Q&A forward error: %s", e, exc_info=True)

        # Start forwarding responses in background
        forward_task = asyncio.create_task(forward_responses())

        # Main loop: receive from browser, send to Gemini
        try:
            while True:
                msg = await websocket.receive_json()
                msg_type = msg.get("type", "")

                if msg_type == "audio":
                    audio_bytes = base64.b64decode(msg["data"])
                    await session.send_audio(audio_bytes)

                elif msg_type == "text":
                    await session.send_text(msg["text"])

                elif msg_type == "close":
                    break

        except WebSocketDisconnect:
            log.info("Live Q&A: Client disconnected for bot_id=%s", bot_id)

        forward_task.cancel()

    except Exception as e:
        log.error("Live Q&A: Session error for bot_id=%s: %s", bot_id, e, exc_info=True)
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        await session.close()
        log.info("Live Q&A: Session ended for bot_id=%s", bot_id)


# ═════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    reload = os.getenv("ENV", "development") == "development"
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=reload)
