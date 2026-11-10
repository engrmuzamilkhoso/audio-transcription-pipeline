"""Work done by the background worker, not by the API."""
import logging
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from rq import get_current_job

from app import audio, postprocess, storage, transcriber
from app.errors import AppError

log = logging.getLogger(__name__)


def process_transcription(transcription_id: str) -> None:
    job = storage.get_job(transcription_id)
    if job is None:
        log.warning("job %s not found, skipping", transcription_id)
        return
    if job["status"] == "completed":
        log.info("job %s already completed, skipping", transcription_id)
        return

    storage.mark_processing(transcription_id, job["attempts"] + 1)
    started = time.perf_counter()

    try:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            extension = Path(job["audio_path"]).suffix

            # 1. Get the original audio from storage
            source = storage.download_audio(job["audio_path"], work_dir / f"source{extension}")

            # 2. Convert to 16 kHz mono WAV
            wav_path = audio.convert_to_wav(source, work_dir)

            # 3. Transcribe
            transcribe_started = time.perf_counter()
            result = transcriber.transcribe(wav_path, language=job["requested_language"])
            transcribe_seconds = time.perf_counter() - transcribe_started

        # 4. Clean up, flag and redact
        clean = postprocess.process(result.segments)

        # 5. Save the result
        storage.save_result(
            transcription_id,
            language=result.language,
            language_probability=result.language_probability,
            text=clean.text,
            segments=[asdict(s) for s in clean.segments],
            redactions=clean.redactions,
            quality=clean.quality,
            timings={
                "transcribe_s": round(transcribe_seconds, 2),
                "total_s": round(time.perf_counter() - started, 2),
            },
        )
        log.info("job %s completed in %.1fs", transcription_id, time.perf_counter() - started)

    except AppError as e:
        # Expected problem with this file (e.g. cannot convert). Retrying will not help.
        storage.mark_failed(transcription_id, e.code, e.message)
        log.warning("job %s failed: %s", transcription_id, e.code)

    except Exception as e:
        # Unexpected problem (crash, network blip). Probably temporary, so the queue retries it.
        if retries_left() > 0:
            storage.mark_retrying(transcription_id, "internal_error", str(e))
            log.warning("job %s failed, will retry (%d left): %s", transcription_id, retries_left(), e)
        else:
            storage.mark_failed(transcription_id, "internal_error", str(e))
            log.error("job %s failed, no retries left: %s", transcription_id, e)
        raise  # tells RQ this attempt failed, so it schedules the next retry


def retries_left() -> int:
    current = get_current_job()
    if current is None or current.retries_left is None:
        return 0
    return current.retries_left