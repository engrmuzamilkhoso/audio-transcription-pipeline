from datetime import datetime, timezone
from pathlib import Path

from app.clients import get_supabase
from app.config import settings

CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- audio files (Supabase Storage) ----------

def upload_audio(local_path: Path, sha256: str, extension: str) -> str:
    """Save the original audio. Named by its fingerprint, so the same file is stored once."""
    storage_path = f"{sha256}{extension}"
    get_supabase().storage.from_(settings.supabase_bucket).upload(
        storage_path,
        local_path,
        {"content-type": CONTENT_TYPES.get(extension, "application/octet-stream"), "upsert": "true"},
    )
    return storage_path


def download_audio(audio_path: str, destination: Path) -> Path:
    data = get_supabase().storage.from_(settings.supabase_bucket).download(audio_path)
    destination.write_bytes(data)
    return destination


# ---------- transcripts (Supabase Postgres) ----------

def find_existing(sha256: str, requested_language: str | None) -> dict | None:
    """Find an earlier job for the same file that finished or is still running."""
    query = (
        get_supabase()
        .table("transcriptions")
        .select("id,status")
        .eq("audio_sha256", sha256)
        .in_("status", ["pending", "processing", "completed"])
    )
    if requested_language is None:
        query = query.is_("requested_language", "null")
    else:
        query = query.eq("requested_language", requested_language)

    rows = query.order("created_at", desc=True).limit(1).execute().data
    return rows[0] if rows else None


def create_job(filename: str, audio_path: str, sha256: str,
               requested_language: str | None, audio_info: dict) -> str:
    row = {
        "status": "pending",
        "filename": filename,
        "audio_path": audio_path,
        "audio_sha256": sha256,
        "requested_language": requested_language,
        "audio": audio_info,
        "duration_s": audio_info["duration_s"],
        "attempts": 0,
    }
    result = get_supabase().table("transcriptions").insert(row).execute()
    return result.data[0]["id"]


def get_job(transcription_id: str) -> dict | None:
    rows = (
        get_supabase()
        .table("transcriptions")
        .select("id,status,audio_path,requested_language,attempts")
        .eq("id", transcription_id)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def mark_processing(transcription_id: str, attempts: int) -> None:
    get_supabase().table("transcriptions").update({
        "status": "processing",
        "attempts": attempts,
        "updated_at": now(),
    }).eq("id", transcription_id).execute()


def save_result(transcription_id: str, *, language: str, language_probability: float,
                text: str, segments: list[dict], redactions: list[dict],
                quality: dict, timings: dict) -> None:
    db = get_supabase()

    # Remove any segments from an earlier attempt, so a re-run never duplicates them.
    db.table("segments").delete().eq("transcription_id", transcription_id).execute()

    if segments:
        db.table("segments").insert([
            {
                "transcription_id": transcription_id,
                "idx": s["id"],
                "start_s": s["start"],
                "end_s": s["end"],
                "text": s["text"],
                "confidence": s["confidence"],
                "needs_review": s["needs_review"],
            }
            for s in segments
        ]).execute()

    db.table("transcriptions").update({
        "status": "completed",
        "language": language,
        "language_probability": language_probability,
        "text": text,
        "redactions": redactions,
        "quality": quality,
        "timings": timings,
        "error_code": None,
        "error_message": None,
        "updated_at": now(),
    }).eq("id", transcription_id).execute()


def mark_retrying(transcription_id: str, code: str, message: str) -> None:
    """Back to the queue after a temporary error; keep the error so we can see what happened."""
    get_supabase().table("transcriptions").update({
        "status": "pending",
        "error_code": code,
        "error_message": message[:500],
        "updated_at": now(),
    }).eq("id", transcription_id).execute()


def mark_pending(transcription_id: str) -> None:
    get_supabase().table("transcriptions").update({
        "status": "pending",
        "updated_at": now(),
    }).eq("id", transcription_id).execute()


def find_stuck(older_than: datetime) -> list[dict]:
    """Jobs that say pending/processing but have not changed for a long time."""
    return (
        get_supabase()
        .table("transcriptions")
        .select("id,status,attempts,updated_at")
        .in_("status", ["pending", "processing"])
        .lt("updated_at", older_than.isoformat())
        .execute()
        .data
    )


def mark_failed(transcription_id: str, code: str, message: str) -> None:
    get_supabase().table("transcriptions").update({
        "status": "failed",
        "error_code": code,
        "error_message": message[:500],
        "updated_at": now(),
    }).eq("id", transcription_id).execute()


def get_transcription(transcription_id: str) -> dict | None:
    db = get_supabase()
    rows = db.table("transcriptions").select("*").eq("id", transcription_id).limit(1).execute().data
    if not rows:
        return None
    t = rows[0]

    segments = (
        db.table("segments")
        .select("idx,start_s,end_s,text,confidence,needs_review")
        .eq("transcription_id", transcription_id)
        .order("idx")
        .execute()
        .data
    )

    response = {
        "id": t["id"],
        "status": t["status"],
        "filename": t["filename"],
        "sha256": t["audio_sha256"],
        "audio": t["audio"],
        "language": t["language"],
        "language_probability": t["language_probability"],
        "text": t["text"],
        "segments": [
            {
                "id": s["idx"],
                "start": float(s["start_s"]),
                "end": float(s["end_s"]),
                "text": s["text"],
                "confidence": float(s["confidence"]) if s["confidence"] is not None else None,
                "needs_review": s["needs_review"],
            }
            for s in segments
        ],
        "redactions": t["redactions"] or [],
        "quality": t["quality"],
        "timings": t["timings"],
        "attempts": t["attempts"],
        "created_at": t["created_at"],
        "updated_at": t["updated_at"],
    }
    if t["status"] == "failed":
        response["error"] = {"code": t["error_code"], "message": t["error_message"]}
    elif t["error_code"] and t["status"] != "completed":
        response["last_error"] = {"code": t["error_code"], "message": t["error_message"]}
    return response