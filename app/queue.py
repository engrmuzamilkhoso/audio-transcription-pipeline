from rq import Queue, Retry
from rq.job import Job, JobStatus
from rq.exceptions import NoSuchJobError

from app.clients import get_redis

QUEUE_NAME = "transcriptions"

# Temporary failures are retried automatically: after 30 s, then 2 min, then 10 min.
RETRY_DELAYS_S = [30, 120, 600]

WAITING_STATUSES = {JobStatus.QUEUED, JobStatus.SCHEDULED, JobStatus.DEFERRED}


def get_queue() -> Queue:
    return Queue(QUEUE_NAME, connection=get_redis())


def enqueue_transcription(transcription_id: str) -> None:
    get_queue().enqueue(
        "app.jobs.process_transcription",
        transcription_id,
        job_id=transcription_id,       # one queue job per transcription, never duplicates
        job_timeout=60 * 60,           # give up on one attempt after 1 hour
        retry=Retry(max=len(RETRY_DELAYS_S), interval=RETRY_DELAYS_S),
        result_ttl=24 * 60 * 60,       # keep RQ's own job record for a day
        failure_ttl=7 * 24 * 60 * 60,  # keep failed jobs for a week
    )


def is_waiting_in_queue(transcription_id: str) -> bool:
    """True if the queue still holds this job and will run it (now or after a retry delay)."""
    try:
        job = Job.fetch(transcription_id, connection=get_redis())
    except NoSuchJobError:
        return False
    return job.get_status() in WAITING_STATUSES