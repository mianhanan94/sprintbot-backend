# ══════════════════════════════════════════════════════════
# SprintBot — Gemini Live API Voice Q&A
# Bidirectional audio streaming for real-time Scrum Master Q&A
# ══════════════════════════════════════════════════════════

import os
import json
import asyncio
import logging
from google import genai
from google.genai import types

log = logging.getLogger("sprintbot")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


def _build_system_instruction(analysis: dict) -> str:
    """Build system instruction that gives SprintBot full context of the standup analysis."""
    return f"""You are SprintBot, an expert AI Scrum Master assistant. You have just analyzed a daily standup meeting.

ROLE: Answer the Scrum Master's questions about this standup analysis. Be concise, direct, and actionable.

RULES:
- Keep answers under 30 seconds of speech (roughly 75 words)
- Reference specific participant names and data from the analysis
- If asked about someone not in the analysis, say so clearly
- Suggest concrete actions, not vague advice
- If the Scrum Master asks something outside the standup context, redirect politely
- NEVER show your reasoning process, thinking steps, or internal notes
- NEVER use markdown formatting like ** or ## in your responses
- Speak naturally as if in a conversation — no headers, no bullet points
- If asked for the transcript, summarize what each participant said

STANDUP ANALYSIS DATA:
{json.dumps(analysis, indent=2)}"""


class LiveSession:
    """Manages a Gemini Live API session for voice Q&A about a standup analysis."""

    def __init__(self, analysis: dict):
        self.analysis = analysis
        self.client = genai.Client(api_key=GEMINI_API_KEY)
        self.session = None
        self._ctx = None
        self._closed = False

    async def connect(self):
        """Open a Gemini Live session with the standup analysis as context."""
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription=types.AudioTranscriptionConfig(),
            speech_config={
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": "Kore"}
                }
            },
            system_instruction=_build_system_instruction(self.analysis),
        )
        self._ctx = self.client.aio.live.connect(
            model="gemini-2.5-flash-native-audio-preview-12-2025",
            config=config,
        )
        self.session = await self._ctx.__aenter__()
        log.info("Live session connected")
        return self

    async def send_audio(self, audio_bytes: bytes):
        """Send raw PCM audio chunk from the user's microphone (16kHz, 16-bit, mono)."""
        if self._closed or not self.session:
            return
        await self.session.send_realtime_input(
            audio=types.Blob(data=audio_bytes, mime_type="audio/pcm;rate=16000")
        )

    async def send_text(self, text: str):
        """Send a text message (fallback for non-mic usage)."""
        if self._closed or not self.session:
            return
        await self.session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]),
            turn_complete=True,
        )

    async def receive_audio(self):
        """
        Async generator that yields audio bytes from Gemini's response.
        Keeps listening across multiple turns (multi-turn conversation).
        Each yield is a chunk of PCM audio (24kHz, 16-bit, mono).
        """
        while not self._closed and self.session:
            try:
                async for response in self.session.receive():
                    if self._closed:
                        return
                    sc = response.server_content
                    if not sc:
                        continue

                    # Audio chunks from model
                    if sc.model_turn:
                        for part in sc.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                yield {"type": "audio", "data": part.inline_data.data}
                            # part.text from native audio model is thinking/reasoning — skip it

                    # Output audio transcription (real text of what was spoken)
                    if hasattr(sc, 'output_transcription') and sc.output_transcription:
                        text = getattr(sc.output_transcription, 'text', '')
                        if text:
                            yield {"type": "transcript", "text": text}

                    # Turn complete — yield but DON'T break, keep listening for next turn
                    if sc.turn_complete:
                        yield {"type": "turn_complete"}

            except Exception as e:
                if not self._closed:
                    log.error("Live session receive error: %s", e)
                    yield {"type": "error", "message": str(e)}
                    break

    async def close(self):
        """Close the Live session."""
        self._closed = True
        if self._ctx:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self.session = None
            self._ctx = None
        log.info("Live session closed")
