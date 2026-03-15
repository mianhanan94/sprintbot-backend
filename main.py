# ══════════════════════════════════════════════════════════
# SprintBot — AI Scrum Meeting Copilot
# Gemini Live Agent Challenge 2026
# ══════════════════════════════════════════════════════════

import os
import json
import wave
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.cloud import texttospeech as tts
from firestore_client import enrich_with_repeat_offenders

load_dotenv()

# ── Config ──
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
RECALL_API_KEY = os.getenv("RECALL_API_KEY")
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "sprintbot-488512")

client = genai.Client(api_key=GEMINI_API_KEY)
log = logging.getLogger("sprintbot")


# ══════════════════════════════════════════════════════════
# TTS: Generate Audio Summary using Gemini TTS
# ══════════════════════════════════════════════════════════

def _build_narration_text(analysis: dict) -> str:
    """Use Gemini to generate a natural spoken narration from analysis."""
    prompt = f"""You are SprintBot, an AI Scrum Master. Generate a concise 30-second spoken briefing
from this standup analysis. Speak directly to the Scrum Master.

Include: health score, key blockers, anti-patterns detected, and top recommendation.
Keep it under 150 words. No bullet points, no markdown — just natural conversational speech.

Analysis:
{json.dumps(analysis, indent=2)}"""

    response = client.models.generate_content(
        model="gemini-3.1-flash-lite-preview",
        contents=prompt,
    )
    return response.text.strip()


def generate_audio_summary(analysis: dict, bot_id: str, results_dir: str) -> str | None:
    """
    Generate a spoken audio summary of the standup analysis using Gemini TTS.

    Flow: analysis dict → Gemini narration script → Gemini TTS → WAV file
    Returns the file path on success, None on failure.
    """
    try:
        # Step 1: Generate narration text from analysis
        log.info("TTS: Generating narration script for bot_id=%s", bot_id)
        narration = _build_narration_text(analysis)
        log.info("TTS: Narration ready (%d chars): %s...", len(narration), narration[:80])

        # Step 2: Synthesize speech with Gemini TTS (streaming API)
        tts_client = tts.TextToSpeechClient()

        config_request = tts.StreamingSynthesizeRequest(
            streaming_config=tts.StreamingSynthesizeConfig(
                voice=tts.VoiceSelectionParams(
                    name="Kore",
                    language_code="en-US",
                    model_name="gemini-2.5-flash-tts",
                ),
            ),
        )

        style_prompt = (
            "Speak as a professional, confident Scrum Master delivering a standup summary. "
            "Be clear, concise, and authoritative with a warm but business-like tone."
        )

        def request_generator():
            yield config_request
            yield tts.StreamingSynthesizeRequest(
                input=tts.StreamingSynthesisInput(
                    text=narration,
                    prompt=style_prompt,
                ),
            )

        log.info("TTS: Streaming synthesis started...")
        audio_data = b""
        for response in tts_client.streaming_synthesize(request_generator()):
            audio_data += response.audio_content

        if not audio_data:
            log.warning("TTS: No audio data received for bot_id=%s", bot_id)
            return None

        # Step 3: Save as WAV (PCM 24kHz, 16-bit, mono)
        audio_path = os.path.join(results_dir, f"{bot_id}_audio.wav")
        with wave.open(audio_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(24000)
            wf.writeframes(audio_data)

        log.info("TTS: Audio saved (%d bytes) -> %s", len(audio_data), audio_path)
        return audio_path

    except Exception as e:
        log.error("TTS: Failed for bot_id=%s: %s", bot_id, e, exc_info=True)
        return None


# ══════════════════════════════════════════════════════════
# TOOL 1: Analyze Standup Transcript
# ══════════════════════════════════════════════════════════
def analyze_standup(transcript: str) -> dict:
    """
    Analyzes a daily standup transcript using Gemini.
    Extracts participants, action items, blockers, hedge words,
    and detects anti-patterns.

    Args:
        transcript: The full standup meeting transcript text.

    Returns:
        dict: Structured analysis with participants, blockers,
              anti-patterns, and recommendations.
    """
    prompt = f"""You are SprintBot — an expert Scrum Master AI.
Analyze this daily standup transcript. Return ONLY valid JSON.

DETECTION RULES:
- Hedge words: "should", "probably", "trying to", "hoping to", "might", "maybe", "I think", "not sure"
- Eternal almost-done: If someone says "almost done", "nearly there", "just finishing up" — flag it
- Blocker signals: "waiting for", "blocked by", "can't proceed", "dependent on", "need access"
- Standup drift: Off-topic discussion, problem-solving in standup, lengthy explanations

JSON STRUCTURE:
{{
  "participants": [
    {{
      "name": "string",
      "yesterday": "what they completed",
      "today": "what they plan to do",
      "blockers": "string or null",
      "hedge_detected": true/false,
      "hedge_words_found": ["list"],
      "risk_level": "low|medium|high",
      "confidence_score": 0.0-1.0
    }}
  ],
  "meeting_summary": "2-3 sentence summary",
  "total_blockers": 0,
  "total_action_items": 0,
  "anti_patterns": [
    {{
      "type": "eternal_almost_done|silent_blocker|repeat_offender|standup_drift",
      "description": "what was detected",
      "participant": "who triggered it",
      "severity": "low|medium|high",
      "recommendation": "what SM should do"
    }}
  ],
  "sm_recommendations": ["list of actions for Scrum Master"],
  "meeting_health_score": 0-100,
  "meeting_duration_concern": true/false
}}

TRANSCRIPT:
{transcript}

Return ONLY the JSON. No markdown. No explanation."""

    response = client.models.generate_content(
        model="gemini-3.1-flash-lite-preview",
        contents=prompt
    )

    raw = response.text.strip()
    # Clean markdown code blocks if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "Failed to parse Gemini response", "raw": raw}


# ══════════════════════════════════════════════════════════
# TOOL 2: Create JIRA Ticket
# ══════════════════════════════════════════════════════════
def create_jira_ticket(
    summary: str,
    description: str,
    assignee: str,
    priority: str = "Medium",
    ticket_type: str = "Task"
) -> dict:
    """
    Creates a JIRA ticket from a standup action item.

    Args:
        summary: Short title for the ticket.
        description: Detailed description of the task.
        assignee: Name of the person responsible.
        priority: Priority level (Low, Medium, High, Critical).
        ticket_type: Type of ticket (Task, Bug, Story).

    Returns:
        dict: Result with ticket key or error message.
    """
    jira_url = os.getenv("JIRA_URL")
    jira_email = os.getenv("JIRA_EMAIL")
    jira_token = os.getenv("JIRA_API_TOKEN")
    jira_project = os.getenv("JIRA_PROJECT_KEY")

    if not all([jira_url, jira_email, jira_token, jira_project]):
        return {
            "status": "simulated",
            "ticket_key": f"{jira_project or 'SPRINT'}-{datetime.now().strftime('%H%M%S')}",
            "summary": summary,
            "assignee": assignee,
            "priority": priority,
            "message": "JIRA credentials not configured. Ticket simulated for demo."
        }

    payload = {
        "fields": {
            "project": {"key": jira_project},
            "summary": summary,
            "description": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}]
            },
            "issuetype": {"name": ticket_type},
            "priority": {"name": priority}
        }
    }

    try:
        resp = requests.post(
            f"{jira_url}/rest/api/3/issue",
            json=payload,
            auth=(jira_email, jira_token),
            headers={"Content-Type": "application/json"}
        )
        if resp.status_code == 201:
            data = resp.json()
            return {"status": "created", "ticket_key": data["key"], "summary": summary, "assignee": assignee}
        else:
            return {"status": "error", "code": resp.status_code, "message": resp.text}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ══════════════════════════════════════════════════════════
# TOOL 3: Post Slack Summary
# ══════════════════════════════════════════════════════════
def post_slack_summary(
    channel: str,
    summary: str,
    blockers: str,
    anti_patterns: str,
    health_score: int
) -> dict:
    """
    Posts a formatted standup summary to a Slack channel.

    Args:
        channel: Slack channel name or ID.
        summary: Meeting summary text.
        blockers: Comma-separated list of blockers.
        anti_patterns: Comma-separated list of detected anti-patterns.
        health_score: Meeting health score 0-100.

    Returns:
        dict: Result with success or error message.
    """
    slack_token = os.getenv("SLACK_BOT_TOKEN")

    if not slack_token:
        return {
            "status": "simulated",
            "channel": channel,
            "message": "Slack token not configured. Summary simulated for demo.",
            "preview": f"*SprintBot Standup Summary*\n{summary}\n\n*Blockers:* {blockers}\n*Anti-Patterns:* {anti_patterns}\n*Health Score:* {health_score}/100"
        }

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "SprintBot Standup Summary"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "divider"},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Blockers:*\n{blockers or 'None'}"},
            {"type": "mrkdwn", "text": f"*Health Score:*\n{health_score}/100"}
        ]},
    ]

    if anti_patterns and anti_patterns != "None":
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn",
            "text": f":warning: *Anti-Patterns Detected:*\n{anti_patterns}"
        }})

    try:
        resp = requests.post(
            "https://slack.com/api/chat.postMessage",
            json={"channel": channel, "blocks": blocks, "text": summary},
            headers={"Authorization": f"Bearer {slack_token}", "Content-Type": "application/json"}
        )
        data = resp.json()
        if data.get("ok"):
            return {"status": "posted", "channel": channel, "ts": data.get("ts")}
        else:
            return {"status": "error", "message": data.get("error")}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ══════════════════════════════════════════════════════════
# TOOL 4: Get JIRA Board Status
# ══════════════════════════════════════════════════════════
def get_jira_board_status(project_key: str) -> dict:
    """
    Fetches current sprint board status from JIRA to cross-reference
    with verbal standup updates.

    Args:
        project_key: The JIRA project key (e.g., "SPRINT").

    Returns:
        dict: Current tickets with status, assignee, and days in status.
    """
    jira_url = os.getenv("JIRA_URL")
    jira_email = os.getenv("JIRA_EMAIL")
    jira_token = os.getenv("JIRA_API_TOKEN")

    if not all([jira_url, jira_email, jira_token]):
        return {
            "status": "simulated",
            "project": project_key,
            "tickets": [
                {"key": f"{project_key}-101", "summary": "Payment gateway integration", "status": "In Progress", "assignee": "Ahmed", "days_in_status": 4},
                {"key": f"{project_key}-102", "summary": "Fix login bug", "status": "In Progress", "assignee": "Sara", "days_in_status": 1},
                {"key": f"{project_key}-103", "summary": "Dashboard redesign", "status": "To Do", "assignee": "Bilal", "days_in_status": 0},
                {"key": f"{project_key}-104", "summary": "API documentation", "status": "In Review", "assignee": "Ahmed", "days_in_status": 2},
                {"key": f"{project_key}-105", "summary": "Client approval for designs", "status": "Blocked", "assignee": "Fatima", "days_in_status": 5},
            ],
            "message": "JIRA credentials not configured. Using simulated board for demo."
        }

    jql = f"project = {project_key} AND sprint in openSprints() ORDER BY status ASC"
    try:
        resp = requests.get(
            f"{jira_url}/rest/api/3/search",
            params={"jql": jql, "maxResults": 50, "fields": "summary,status,assignee,created,updated"},
            auth=(jira_email, jira_token),
            headers={"Content-Type": "application/json"}
        )
        if resp.status_code == 200:
            data = resp.json()
            tickets = []
            for issue in data.get("issues", []):
                fields = issue["fields"]
                updated = datetime.fromisoformat(fields["updated"].replace("Z", "+00:00"))
                days = (datetime.now(updated.tzinfo) - updated).days
                tickets.append({
                    "key": issue["key"],
                    "summary": fields["summary"],
                    "status": fields["status"]["name"],
                    "assignee": fields.get("assignee", {}).get("displayName", "Unassigned"),
                    "days_in_status": days
                })
            return {"status": "success", "project": project_key, "tickets": tickets}
        else:
            return {"status": "error", "code": resp.status_code, "message": resp.text}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ══════════════════════════════════════════════════════════
# MAIN: Run SprintBot Pipeline
# ══════════════════════════════════════════════════════════
def run_sprintbot(transcript: str):
    """
    Runs the full SprintBot pipeline:
    1. Analyze transcript with Gemini
    2. Cross-reference with JIRA board
    3. Create tickets for action items
    4. Post summary to Slack
    """
    print("\n" + "=" * 60)
    print("  SPRINTBOT — AI Scrum Meeting Copilot")
    print("=" * 60)

    # Step 1: Analyze transcript
    print("\n[1/4] Analyzing standup transcript with Gemini...")
    analysis = analyze_standup(transcript)

    if "error" in analysis:
        print(f"  ERROR: {analysis['error']}")
        if "raw" in analysis:
            print(f"  Raw response: {analysis['raw'][:500]}")
        return

    # Enrich with repeat offender flags from Firestore history (read-only)
    analysis = enrich_with_repeat_offenders(analysis)

    print(f"  Participants: {len(analysis.get('participants', []))}")
    print(f"  Blockers: {analysis.get('total_blockers', 0)}")
    print(f"  Anti-patterns: {len(analysis.get('anti_patterns', []))}")
    print(f"  Health Score: {analysis.get('meeting_health_score', 'N/A')}/100")

    # Print participant details
    print("\n  -- Participant Details --")
    for p in analysis.get("participants", []):
        hedge_flag = " [!] HEDGE" if p.get("hedge_detected") else ""
        risk = p.get("risk_level", "low").upper()
        print(f"  {p['name']} [{risk}]{hedge_flag}")
        print(f"    Yesterday: {p.get('yesterday', 'N/A')}")
        print(f"    Today:     {p.get('today', 'N/A')}")
        print(f"    Blockers:  {p.get('blockers', 'None')}")
        if p.get("hedge_words_found"):
            print(f"    Hedge words: {', '.join(p['hedge_words_found'])}")

    # Print anti-patterns
    if analysis.get("anti_patterns"):
        print("\n  -- Anti-Patterns Detected --")
        for ap in analysis["anti_patterns"]:
            severity = ap.get("severity", "low").upper()
            print(f"  [!] [{severity}] {ap['type']}")
            print(f"     {ap['description']}")
            print(f"     Participant: {ap.get('participant', 'N/A')}")
            print(f"     Action: {ap.get('recommendation', 'N/A')}")

    # Step 2: Get JIRA board status
    print("\n[2/4] Fetching JIRA board status...")
    jira_project = os.getenv("JIRA_PROJECT_KEY", "SPRINT")
    board = get_jira_board_status(jira_project)
    print(f"  Tickets in sprint: {len(board.get('tickets', []))}")
    for t in board.get("tickets", []):
        stale = " [!] STALE" if t["days_in_status"] > 3 else ""
        print(f"  {t['key']}: {t['summary']} [{t['status']}] -> {t['assignee']} ({t['days_in_status']}d){stale}")

    # Step 3: Create JIRA tickets for action items
    print("\n[3/4] Creating JIRA tickets for action items...")
    tickets_created = []
    for p in analysis.get("participants", []):
        if p.get("today") and p.get("confidence_score", 0.5) >= 0.5:
            result = create_jira_ticket(
                summary=p["today"][:100],
                description=f"Auto-created by SprintBot from standup.\nParticipant: {p['name']}\nRisk: {p.get('risk_level', 'low')}",
                assignee=p["name"],
                priority="High" if p.get("risk_level") == "high" else "Medium"
            )
            tickets_created.append(result)
            print(f"  [OK] {result.get('ticket_key', 'N/A')}: {result.get('summary', 'N/A')[:60]}")

    # Step 4: Post Slack summary
    print("\n[4/4] Posting summary to Slack...")
    blockers_text = ", ".join([
        f"{p['name']}: {p['blockers']}"
        for p in analysis.get("participants", [])
        if p.get("blockers") and p["blockers"] != "null"
    ]) or "None"

    anti_pattern_text = ", ".join([
        f"{ap['type']} ({ap.get('participant', 'N/A')})"
        for ap in analysis.get("anti_patterns", [])
    ]) or "None"

    slack_result = post_slack_summary(
        channel=os.getenv("SLACK_CHANNEL", "#standup-summaries"),
        summary=analysis.get("meeting_summary", "No summary available"),
        blockers=blockers_text,
        anti_patterns=anti_pattern_text,
        health_score=analysis.get("meeting_health_score", 0)
    )
    print(f"  Slack: {slack_result.get('status', 'unknown')}")
    if slack_result.get("preview"):
        print(f"\n  -- Slack Preview --\n{slack_result['preview']}")

    # Final summary
    print("\n" + "=" * 60)
    print("  SPRINTBOT RUN COMPLETE")
    print(f"  Participants analyzed: {len(analysis.get('participants', []))}")
    print(f"  Anti-patterns found:  {len(analysis.get('anti_patterns', []))}")
    print(f"  JIRA tickets created: {len(tickets_created)}")
    print(f"  Health Score:         {analysis.get('meeting_health_score', 'N/A')}/100")
    print("=" * 60)

    # Save full analysis to file
    output = {
        "timestamp": datetime.now().isoformat(),
        "analysis": analysis,
        "jira_board": board,
        "tickets_created": tickets_created,
        "slack_result": slack_result
    }
    with open("sprintbot_output.json", "w") as f:
        json.dump(output, f, indent=2)
    print("\nFull analysis saved to sprintbot_output.json")

    return output


# ══════════════════════════════════════════════════════════
# TEST: Run with a sample standup transcript
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":

    # Sample standup transcript with anti-patterns baked in
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

    run_sprintbot(SAMPLE_TRANSCRIPT)