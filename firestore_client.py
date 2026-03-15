import os
import logging
from datetime import datetime, timezone

log = logging.getLogger("sprintbot")

try:
    from google.cloud import firestore as _firestore
    _FIRESTORE_AVAILABLE = True
except ImportError:
    _FIRESTORE_AVAILABLE = False
    log.warning("google-cloud-firestore not installed — Firestore disabled")

_db = None


def _get_db():
    global _db
    if not _FIRESTORE_AVAILABLE:
        return None
    if _db is None:
        try:
            project = os.getenv("GOOGLE_CLOUD_PROJECT", "sprintbot-488512")
            _db = _firestore.Client(project=project)
        except Exception as e:
            log.error("Firestore init failed: %s", e)
            return None
    return _db


# ══════════════════════════════════════════════════════════
# WRITE — save meeting result
# ══════════════════════════════════════════════════════════

def save_meeting(bot_id: str, transcript: str, analysis_output: dict) -> bool:
    """Save full meeting result to Firestore. Returns True on success."""
    db = _get_db()
    if not db:
        log.warning("Firestore unavailable — meeting not saved: bot_id=%s", bot_id)
        return False
    try:
        analysis = analysis_output.get("analysis", {})
        doc = {
            "bot_id": bot_id,
            "timestamp": datetime.now(timezone.utc),
            "transcript": transcript,
            "health_score": analysis.get("meeting_health_score"),
            "participants": [p["name"] for p in analysis.get("participants", [])],
            "anti_patterns": analysis.get("anti_patterns", []),
            "meeting_summary": analysis.get("meeting_summary", ""),
            "full_result": analysis_output,
        }
        db.collection("meetings").document(bot_id).set(doc)
        log.info("Meeting saved to Firestore: bot_id=%s", bot_id)
        return True
    except Exception as e:
        log.error("Firestore save_meeting failed: %s", e)
        return False


def update_participant_history(participant: str, anti_patterns: list, bot_id: str) -> None:
    """Increment anti-pattern counts for a participant across meetings."""
    db = _get_db()
    if not db:
        return
    try:
        name_key = participant.lower().replace(" ", "_")
        ref = db.collection("participants").document(name_key)
        doc = ref.get()
        if doc.exists:
            data = doc.to_dict()
        else:
            data = {
                "name": participant,
                "anti_pattern_counts": {},
                "meeting_ids": [],
                "total_meetings": 0,
            }

        counts = data.get("anti_pattern_counts", {})
        for ap in anti_patterns:
            ap_type = ap.get("type", "unknown")
            if ap_type != "repeat_offender":  # don't count the meta-pattern itself
                counts[ap_type] = counts.get(ap_type, 0) + 1

        meeting_ids = data.get("meeting_ids", [])
        if bot_id not in meeting_ids:
            meeting_ids.append(bot_id)

        ref.set({
            "name": participant,
            "anti_pattern_counts": counts,
            "meeting_ids": meeting_ids,
            "total_meetings": len(meeting_ids),
            "last_seen": datetime.now(timezone.utc),
        })
        log.info("Participant history updated: %s counts=%s", participant, counts)
    except Exception as e:
        log.error("Firestore update_participant_history failed: %s", e)


# ══════════════════════════════════════════════════════════
# READ — enrich analysis with repeat offender flags
# ══════════════════════════════════════════════════════════

def get_participant_history(participant: str) -> dict:
    """Return anti-pattern history for a participant, or {} if not found."""
    db = _get_db()
    if not db:
        return {}
    try:
        name_key = participant.lower().replace(" ", "_")
        doc = db.collection("participants").document(name_key).get()
        if doc.exists:
            data = doc.to_dict()
            last_seen = data.get("last_seen")
            if hasattr(last_seen, "isoformat"):
                data["last_seen"] = last_seen.isoformat()
            return data
        return {}
    except Exception as e:
        log.error("Firestore get_participant_history failed: %s", e)
        return {}


def enrich_with_repeat_offenders(analysis: dict) -> dict:
    """
    Check Firestore history and inject repeat_offender anti-patterns
    for anyone who hit the same pattern in a previous meeting.
    Called BEFORE saving the current meeting, so we only look at past data.
    """
    try:
        anti_patterns = analysis.get("anti_patterns", [])

        # Group current meeting's anti-patterns by participant
        participant_patterns: dict[str, list] = {}
        for ap in anti_patterns:
            name = ap.get("participant", "")
            if name:
                participant_patterns.setdefault(name, []).append(ap)

        new_repeats = []
        for name, patterns in participant_patterns.items():
            history = get_participant_history(name)
            if not history:
                continue
            counts = history.get("anti_pattern_counts", {})
            total_meetings = history.get("total_meetings", 0)

            for ap in patterns:
                ap_type = ap.get("type", "")
                if ap_type == "repeat_offender":
                    continue
                prev_count = counts.get(ap_type, 0)
                if prev_count >= 1:
                    new_repeats.append({
                        "type": "repeat_offender",
                        "description": (
                            f"{name} has shown '{ap_type}' in {prev_count} previous "
                            f"sprint(s). This is a confirmed recurring pattern."
                        ),
                        "participant": name,
                        "severity": "high",
                        "recommendation": (
                            f"Schedule a 1:1 coaching session with {name} to address "
                            f"the recurring '{ap_type}' pattern before it becomes a team-level risk."
                        ),
                        "pattern_ref": ap_type,
                        "previous_occurrences": prev_count,
                        "total_meetings_tracked": total_meetings,
                    })

        if new_repeats:
            analysis["anti_patterns"] = anti_patterns + new_repeats
            log.info("Repeat offender flags added: %d", len(new_repeats))

        return analysis
    except Exception as e:
        log.error("enrich_with_repeat_offenders failed: %s", e)
        return analysis


# ══════════════════════════════════════════════════════════
# READ — query meetings and participants
# ══════════════════════════════════════════════════════════

def get_meeting(bot_id: str) -> dict | None:
    """Get full meeting result from Firestore."""
    db = _get_db()
    if not db:
        return None
    try:
        doc = db.collection("meetings").document(bot_id).get()
        if doc.exists:
            data = doc.to_dict()
            ts = data.get("timestamp")
            if hasattr(ts, "isoformat"):
                data["timestamp"] = ts.isoformat()
            # Return full_result (same shape as the file-based result)
            return data.get("full_result") or data
        return None
    except Exception as e:
        log.error("Firestore get_meeting failed: %s", e)
        return None


def list_meetings(limit: int = 50) -> list:
    """List recent meetings from Firestore, newest first."""
    db = _get_db()
    if not db:
        return []
    try:
        docs = (
            db.collection("meetings")
            .order_by("timestamp", direction=_firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        results = []
        for doc in docs:
            data = doc.to_dict()
            ts = data.get("timestamp")
            results.append({
                "bot_id": data.get("bot_id"),
                "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "health_score": data.get("health_score"),
                "participants": data.get("participants", []),
                "anti_pattern_count": len(data.get("anti_patterns", [])),
                "error": (data.get("full_result") or {}).get("error"),
            })
        return results
    except Exception as e:
        log.error("Firestore list_meetings failed: %s", e)
        return []
