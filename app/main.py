import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from app import audio, storage, transcriber
from app.clients import get_redis, get_supabase
from app.config import settings
from app.errors import AppError
from app.queue import enqueue_transcription

app = FastAPI(title="Transcription Pipeline")


@app.exception_handler(AppError)
def handle_app_error(request: Request, error: AppError):
    return JSONResponse(
        status_code=error.status,
        content={"status": "failed", "error": {"code": error.code, "message": error.message}},
    )


@app.get("/health")
def health():
    checks = {}

    try:
        get_redis().ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    try:
        get_supabase().table("transcriptions").select("id").limit(1).execute()
        checks["supabase_db"] = "ok"
    except Exception as e:
        checks["supabase_db"] = f"error: {e}"

    try:
        get_supabase().storage.get_bucket(settings.supabase_bucket)
        checks["supabase_storage"] = "ok"
    except Exception as e:
        checks["supabase_storage"] = f"error: {e}"

    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "ok" if all_ok else "degraded", "checks": checks},
    )


@app.post("/v1/transcriptions")
def create_transcription(
    file: UploadFile = File(...),
    language: str | None = Form(None),
):
    filename = file.filename or "upload"
    extension = Path(filename).suffix.lower()
    language = transcriber.clean_language(language)

    with tempfile.TemporaryDirectory() as tmp:
        upload_path = Path(tmp) / f"upload{extension}"

        # 1. Save the upload to disk
        with upload_path.open("wb") as out:
            shutil.copyfileobj(file.file, out, length=1024 * 1024)

        # 2. Validate: extension, size, real format
        detected_format = audio.validate_file(upload_path, filename)

        # 3. Same file already done or in progress? Return that job instead of a new one.
        sha256 = audio.sha256_of_file(upload_path)
        existing = storage.find_existing(sha256, language)
        if existing:
            body = {**storage.get_transcription(existing["id"]), "cached": True}
            return JSONResponse(status_code=200 if existing["status"] == "completed" else 202, content=body)

        # 4. Read audio details and check duration (before storing anything)
        info = audio.probe(upload_path, detected_format)
        audio.check_duration(info)

        # 5. Store the original audio and create the job row
        audio_path = storage.upload_audio(upload_path, sha256, extension)
        job_id = storage.create_job(filename, audio_path, sha256, language, asdict(info))

    # 6. Hand the job to the queue; a worker will pick it up
    try:
        enqueue_transcription(job_id)
    except Exception as e:
        storage.mark_failed(job_id, "queue_unavailable", str(e))
        raise AppError("queue_unavailable", "Could not queue the job, please retry", 503)

    return JSONResponse(
        status_code=202,
        content={
            "id": job_id,
            "status": "pending",
            "status_url": f"/v1/transcriptions/{job_id}",
            "cached": False,
        },
    )


@app.post("/v1/transcriptions/{transcription_id}/retry")
def retry_transcription(transcription_id: UUID):
    """Manually re-run a failed job, e.g. after fixing the cause."""
    job_id = str(transcription_id)
    job = storage.get_job(job_id)
    if job is None:
        raise AppError("not_found", "No transcription with this id", 404)
    if job["status"] != "failed":
        raise AppError("not_failed", f"Only failed jobs can be retried; this one is '{job['status']}'", 409)

    storage.mark_pending(job_id)
    try:
        enqueue_transcription(job_id)
    except Exception as e:
        storage.mark_failed(job_id, "queue_unavailable", str(e))
        raise AppError("queue_unavailable", "Could not queue the job, please retry", 503)

    return JSONResponse(
        status_code=202,
        content={"id": job_id, "status": "pending", "status_url": f"/v1/transcriptions/{job_id}"},
    )


@app.get("/v1/transcriptions/{transcription_id}")
def read_transcription(transcription_id: UUID):
    result = storage.get_transcription(str(transcription_id))
    if result is None:
        raise AppError("not_found", "No transcription with this id", 404)
    return result