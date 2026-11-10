"""Find jobs that got stuck (e.g. a worker died mid-job, or Redis lost the queue) and requeue them.

Run with:  docker compose exec worker python -m app.recover
"""
import logging
from datetime import datetime, timedelta, timezone

from app import storage
from app.queue import enqueue_transcription, is_waiting_in_queue

# Longer than one attempt may run (1 hour), so a job this old cannot still be running.
STUCK_AFTER = timedelta(minutes=70)

# Stop re-queueing a job that keeps failing.
MAX_ATTEMPTS = 6

log = logging.getLogger(__name__)


def recover_stuck_jobs() -> dict:
    cutoff = datetime.now(timezone.utc) - STUCK_AFTER
    summary = {"requeued": [], "still_waiting": [], "gave_up": []}

    for job in storage.find_stuck(cutoff):
        job_id = job["id"]

        if is_waiting_in_queue(job_id):
            summary["still_waiting"].append(job_id)   # just a long queue, leave it alone
            continue

        if job["attempts"] >= MAX_ATTEMPTS:
            storage.mark_failed(job_id, "too_many_attempts", f"Gave up after {job['attempts']} attempts")
            summary["gave_up"].append(job_id)
            continue

        storage.mark_pending(job_id)
        enqueue_transcription(job_id)
        summary["requeued"].append(job_id)

    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = recover_stuck_jobs()
    log.info("requeued=%d still_waiting=%d gave_up=%d",
             len(result["requeued"]), len(result["still_waiting"]), len(result["gave_up"]))
    for key, ids in result.items():
        for job_id in ids:
            print(f"{key}: {job_id}")