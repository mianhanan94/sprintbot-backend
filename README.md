# SprintBot — AI Scrum Meeting Copilot

**Gemini Live Agent Challenge 2026 | Category: Live Agents**

SprintBot is an AI-powered Scrum Master assistant that joins standup meetings, transcribes and analyzes conversations using Gemini, detects behavioral anti-patterns, and provides real-time voice Q&A via the Gemini Live API.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      Scrum Master (Browser)                     │
│                     https://devfte.com                          │
└──────────────────────────┬──────────────────────────────────────┘
                           │
              REST API + WebSocket (WSS)
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                  Google Cloud Run (Backend)                      │
│              FastAPI + Python 3.12 + uvicorn                    │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    SprintBot Pipeline                    │    │
│  │                                                         │    │
│  │  1. Recall.ai bot joins meeting & transcribes           │    │
│  │  2. Gemini 3.1 Flash Lite analyzes transcript           │    │
│  │     → participants, blockers, anti-patterns, scoring    │    │
│  │  3. Firestore stores history & detects repeat offenders │    │
│  │  4. Gemini TTS generates audio summary (Kore voice)     │    │
│  │  5. JIRA tickets auto-created from action items         │    │
│  │  6. Slack summary posted to channel                     │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              Gemini Live API (Voice Q&A)                 │    │
│  │                                                         │    │
│  │  Browser ←→ WebSocket ←→ Gemini Live Session            │    │
│  │  • Bidirectional audio (16kHz in / 24kHz out)           │    │
│  │  • Text fallback for typed questions                    │    │
│  │  • Context-aware (full standup analysis as system prompt)│    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
├──────────────────────────────────────────────────────────────────┤
│  Google Cloud Services Used:                                     │
│  • Cloud Run (backend hosting)                                   │
│  • Firestore (meeting & participant history)                     │
│  • Cloud Text-to-Speech (Gemini TTS audio summaries)             │
│  • Secret Manager (API key storage)                              │
│  • Cloud Logging (structured logging)                            │
└──────────────────────────────────────────────────────────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ Recall.ai│ │ JIRA API │ │ Slack API│
        │ (meeting │ │ (ticket  │ │ (summary │
        │  bot &   │ │ creation)│ │  posting)│
        │ transcr.)│ │          │ │          │
        └──────────┘ └──────────┘ └──────────┘
```

## Gemini Models Used

| Model | Purpose |
|-------|---------|
| `gemini-3.1-flash-lite-preview` | Transcript analysis & TTS narration script |
| `gemini-2.5-flash-tts` (via Cloud TTS) | Audio summary voice synthesis (Kore voice) |
| `gemini-2.5-flash-native-audio-preview` | Live API real-time voice Q&A |

## Google Cloud Services

- **Cloud Run** — Backend hosting (FastAPI)
- **Firestore** — Persistent meeting & participant history storage
- **Cloud Text-to-Speech** — Gemini TTS audio meeting summaries
- **Secret Manager** — Secure API key storage
- **Cloud Logging** — Structured application logging

## Third-Party Integrations

- **Recall.ai** — Meeting bot that joins Google Meet/Zoom/Teams, records and transcribes
- **JIRA REST API** — Auto-creates tickets from standup action items (simulated if credentials not configured)
- **Slack Bot API** — Posts formatted standup summaries to channels (simulated if credentials not configured)

## Features

- **Automated Meeting Analysis** — Bot joins standup, transcribes, and analyzes with Gemini
- **Anti-Pattern Detection** — Detects eternal almost-done, silent blockers, standup drift, repeat offenders
- **Health Scoring** — 0-100 meeting health score with risk levels per participant
- **Hedge Word Detection** — Flags uncertainty language ("probably", "might", "I think")
- **Repeat Offender Tracking** — Cross-meeting pattern history via Firestore
- **Live Voice Q&A** — Real-time bidirectional audio chat with SprintBot about meeting analysis
- **Audio Summaries** — TTS-generated spoken briefings for Scrum Masters
- **JIRA Integration** — Auto-creates tickets from action items
- **Slack Integration** — Posts formatted summaries with blockers and anti-patterns

## Project Structure

```
├── main.py              # Core pipeline: Gemini analysis, JIRA, Slack, TTS
├── api.py               # FastAPI server: REST, webhooks, WebSocket Live Q&A
├── live_session.py       # Gemini Live API session manager
├── firestore_client.py   # Firestore CRUD: meetings, participants, repeat offenders
├── Dockerfile            # Container config for Cloud Run
├── pyproject.toml        # Python dependencies (uv)
└── README.md
```

## Prerequisites

- Python 3.12+
- A Google Cloud project with billing enabled
- Gemini API key
- Recall.ai API key (for meeting bot functionality)
- (Optional) JIRA and Slack credentials for full integration

## Local Setup

```bash
# 1. Clone the repository
git clone https://github.com/mianhanan94/sprintbot-backend.git
cd sprintbot-backend

# 2. Create a .env file with your credentials
cat > .env << 'EOF'
GEMINI_API_KEY=your-gemini-api-key
RECALL_API_KEY=your-recall-api-key
GOOGLE_CLOUD_PROJECT=your-gcp-project-id

# Optional integrations
JIRA_URL=https://your-domain.atlassian.net
JIRA_EMAIL=your-email@example.com
JIRA_API_TOKEN=your-jira-token
JIRA_PROJECT_KEY=SPRINT
SLACK_BOT_TOKEN=xoxb-your-slack-token
SLACK_CHANNEL=#standup-summaries
EOF

# 3. Install dependencies (using uv or pip)
# With uv:
uv sync

# Or with pip:
pip install -r <(sed -n 's/.*"\(.*\)".*/\1/p' pyproject.toml | grep -v "^$")

# Or install directly:
pip install fastapi uvicorn requests python-dotenv google-genai google-adk \
    google-cloud-aiplatform google-cloud-firestore google-cloud-logging \
    google-cloud-secret-manager google-cloud-texttospeech

# 4. Run the server locally
python api.py
# Server starts at http://localhost:8000

# 5. Test the pipeline (no real meeting needed)
curl -X POST http://localhost:8000/test/pipeline
```

## Deploy to Google Cloud Run

```bash
# Authenticate with Google Cloud
gcloud auth login

# Set your project
gcloud config set project your-gcp-project-id

# Deploy from source (uses Dockerfile)
gcloud run deploy sprintbot \
  --source . \
  --region us-central1 \
  --allow-unauthenticated

# Set environment variables (secrets)
gcloud run services update sprintbot \
  --region us-central1 \
  --set-env-vars GEMINI_API_KEY=your-key,RECALL_API_KEY=your-key
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/bot` | Send Recall.ai bot to join a meeting |
| `POST` | `/webhook` | Receive Recall.ai webhook events |
| `GET` | `/bot/{bot_id}/status` | Check bot & transcript status |
| `GET` | `/results` | List all meeting analyses |
| `GET` | `/results/{bot_id}` | Full meeting detail |
| `GET` | `/results/{bot_id}/audio` | Audio summary WAV |
| `GET` | `/participants/{name}/history` | Participant anti-pattern history |
| `WSS` | `/live/{bot_id}` | Real-time voice Q&A (Gemini Live API) |
| `POST` | `/test/pipeline` | Test with sample transcript |
| `POST` | `/test/webhook?bot_id=X` | Simulate webhook for existing bot |

## Live URLs

| Service | URL |
|---------|-----|
| Backend API | https://sprintbot-99940368064.us-central1.run.app |
| Frontend Dashboard | https://devfte.com |
| Frontend (Firebase) | https://gen-lang-client-0777571242.web.app |

## Frontend Repository

The Command Center dashboard is in a separate repository:
https://github.com/mianhanan94/sprintbot-command-center

## License

This project was created for the Gemini Live Agent Challenge 2026.
