# Audio Transcription Pipeline

A service that accepts an audio file, transcribes it with Whisper, and returns a cleaned,
PII-redacted transcript with timestamps per segment, ready for downstream use (support
tickets, search, analytics, summarisation).

- **API:** FastAPI (Python 3.12)
- **Speech-to-text:** faster-whisper (open-source Whisper, runs locally on CPU)
- **Queue:** Redis + RQ, with a separate background worker
- **Storage:** Supabase Storage (audio files) and Supabase Postgres (jobs, transcripts, segments)
- **Runs with:** Docker Compose

---

## Architecture

```
                    POST /v1/transcriptions (file)
 Client ───────────────────────────────────────────────► API (FastAPI)
   ▲                                                      │ 1. validate (extension, size, magic bytes)
   │  202 { id, status: "pending" }  (returns instantly)  │ 2. SHA-256 fingerprint, reuse earlier result
   │◄─────────────────────────────────────────────────────┤ 3. ffprobe: check duration
   │                                                      │ 4. store audio  ─────► Supabase Storage
   │                                                      │ 5. create job row ───► Supabase Postgres
   │                                                      │ 6. enqueue job id ───► Redis queue
   │                                                                                   │
   │                                                       Worker (RQ) ◄───────────────┘
   │                                                      │ download audio from storage
   │                                                      │ ffmpeg → 16 kHz mono WAV
   │                                                      │ Whisper → segments + timestamps
   │                                                      │ clean, drop hallucinations,
   │                                                      │ flag low confidence, redact PII
   │                                                      │ save transcript + segments ──► Postgres
   │
   │  GET /v1/transcriptions/{id}
   └──────────────────────────────► pending → processing → completed / failed
```

Job lifecycle:

```
pending ──► processing ──► completed
   ▲             │
   │      temporary error, retries left ──► back to pending (retry after 30 s / 2 min / 10 min)
   │             │
   │      permanent error, or no retries left
   │             ▼
   └──────── failed  ◄── manual retry: POST /v1/transcriptions/{id}/retry
```

---

## Project structure

```
app/
├── main.py         API endpoints
├── config.py       settings from environment variables
├── clients.py      Supabase and Redis connections (created once, reused)
├── errors.py       AppError: expected errors with a stable code and HTTP status
├── audio.py        validation, magic-byte detection, ffprobe, ffmpeg conversion
├── transcriber.py  Whisper model loading and transcription
├── postprocess.py  text cleanup, hallucination filter, confidence flags, PII redaction
├── storage.py      all Supabase reads and writes
├── queue.py        enqueueing jobs with retry settings
├── jobs.py         the work the background worker does
└── recover.py      re-queues jobs that got stuck
Dockerfile          one image, used by both the API and the worker
docker-compose.yml  redis + api + worker
```

---

## Running it

### 1. Supabase

Create a Supabase project, then in **SQL Editor** run:

```sql
create table public.transcriptions (
  id                   uuid primary key default gen_random_uuid(),
  status               text not null default 'pending'
                       check (status in ('pending', 'processing', 'completed', 'failed')),
  filename             text not null,
  audio_path           text not null,
  audio_sha256         text not null,
  audio                jsonb,
  requested_language   text,
  language             text,
  language_probability numeric,
  duration_s           numeric,
  text                 text,
  redactions           jsonb,
  quality              jsonb,
  timings              jsonb,
  error_code           text,
  error_message        text,
  attempts             int not null default 0,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);
create index on public.transcriptions (audio_sha256);
create index on public.transcriptions (status);

create table public.segments (
  id               bigint generated always as identity primary key,
  transcription_id uuid not null references public.transcriptions(id) on delete cascade,
  idx              int not null,
  start_s          numeric not null,
  end_s            numeric not null,
  text             text not null,
  confidence       numeric,
  needs_review     boolean not null default false
);
create index on public.segments (transcription_id);

alter table public.transcriptions enable row level security;
alter table public.segments enable row level security;
```

Then create a **private** Storage bucket (for example `audio`).

### 2. Environment

Copy `.env.example` to `.env` and fill in:

```
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_SECRET_KEY=sb_secret_...
SUPABASE_BUCKET=audio
REDIS_URL=redis://redis:6379/0
# optional
WHISPER_MODEL=base        # tiny | base | small | medium | large-v3
MAX_FILE_MB=50
MAX_DURATION_S=1800
```

### 3. Start

```bash
docker compose up -d --build
```

- API and interactive docs: http://localhost:8000/docs
- Health check: http://localhost:8000/health
- The Whisper model downloads on the first job and is cached in a Docker volume.

Scale workers for more parallel transcriptions:

```bash
docker compose up -d --scale worker=3
```

Re-queue stuck jobs:

```bash
docker compose exec worker python -m app.recover
```

---

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/transcriptions` | Upload `file` (multipart), optional `language` (`en`, `ur`, ... or empty for auto-detect). Returns **202** with a job id. |
| `GET` | `/v1/transcriptions/{id}` | Job status, and the transcript once completed. |
| `POST` | `/v1/transcriptions/{id}/retry` | Re-run a failed job. **409** if the job is not failed. |
| `GET` | `/health` | Checks Redis, Supabase database and Supabase Storage. **503** if any is down. |

Upload:

```bash
curl -F "file=@call.m4a" -F "language=en" http://localhost:8000/v1/transcriptions
```

```json
{ "id": "c31fc3c8-...", "status": "pending", "status_url": "/v1/transcriptions/c31fc3c8-...", "cached": false }
```

Completed result:

```json
{
  "id": "0f1f846e-...",
  "status": "completed",
  "filename": "Recording.m4a",
  "audio": { "format": "m4a", "codec": "aac", "duration_s": 16.87, "sample_rate": 48000, "channels": 2 },
  "language": "en",
  "language_probability": 1,
  "text": "My card number is [CARD_NUMBER] My phone number is [PHONE]",
  "segments": [
    { "id": 0, "start": 3.95, "end": 10.95, "text": "My card number is [CARD_NUMBER]", "confidence": 0.732, "needs_review": false },
    { "id": 1, "start": 10.95, "end": 15.95, "text": "My phone number is [PHONE]", "confidence": 0.732, "needs_review": false }
  ],
  "redactions": [ { "type": "CARD_NUMBER", "segment_id": 0 }, { "type": "PHONE", "segment_id": 1 } ],
  "quality": { "avg_confidence": 0.732, "low_confidence_segments": 0, "dropped_segments": 0, "needs_review": false },
  "timings": { "transcribe_s": 10.5, "total_s": 14.73 },
  "attempts": 1
}
```

Errors always have the same shape and a stable code:

```json
{ "status": "failed", "error": { "code": "not_audio", "message": "File content is not a supported audio format" } }
```

| Code | HTTP | Meaning |
|---|---|---|
| `unsupported_extension` | 415 | Extension not in the allowed list |
| `not_audio` | 415 | File content is not real audio (magic-byte check) |
| `empty_file` | 400 | Zero bytes |
| `file_too_large` | 413 | Over `MAX_FILE_MB` |
| `audio_too_long` / `audio_too_short` | 413 / 422 | Duration outside limits |
| `corrupt_audio`, `no_audio_stream`, `conversion_failed` | 422 | ffprobe/ffmpeg could not read the file |
| `invalid_language` | 400 | Language code Whisper does not support |
| `not_found` | 404 | Unknown transcription id |
| `not_failed` | 409 | Retry requested on a job that is not failed |
| `queue_unavailable` | 503 | Redis unreachable; retry the request |
| `internal_error` | job field | Unexpected failure inside the worker (retried automatically) |

---

## Design decisions

### Different audio formats
Accepts WAV, MP3, M4A, FLAC, OGG and WebM. The real format is detected from the file's
**magic bytes**, not trusted from the extension, so a text file renamed `.mp3` is rejected
before it reaches ffmpeg. Every input is converted with ffmpeg to **16 kHz, mono, 16-bit WAV**,
the format Whisper expects. The transcription code therefore only ever sees one format.

The converted WAV is read into a NumPy array and passed to Whisper directly. This decodes
the audio once (ffmpeg) instead of twice, and avoids a version conflict between
faster-whisper and the PyAV library it uses for decoding files.

### Transcription
**faster-whisper** runs the open-source Whisper model with CTranslate2 and int8 quantisation,
which is much faster on CPU than the reference implementation. It runs locally, so no
customer audio is sent to a third-party API. The model size is a setting (`WHISPER_MODEL`):
`base` is fast, `small` was noticeably better on names and accents in testing. The model is
loaded once per worker and reused.

- `vad_filter=True` skips silence: faster, and fewer invented words.
- `condition_on_previous_text=False` prevents one mistake repeating through the transcript.
- `beam_size=5` compares several candidate word sequences for better accuracy.
- Whisper's average log-probability is converted to a 0–1 **confidence** per segment.

### Post-processing for downstream use
- **Cleanup:** collapse whitespace, remove spaces before punctuation.
- **Hallucination filter:** Whisper sometimes invents text on silence ("Thanks for watching!").
  A segment is dropped only when *both* the no-speech probability is high *and* confidence is low,
  so quiet real speech is kept. Consecutive duplicate segments (repetition loops) are dropped too.
- **Confidence flags:** segments below 0.5 confidence get `needs_review: true`, and a transcript-level
  `quality.needs_review` tells consumers whether a human should check it.
- **PII redaction** before anything is stored or returned: card numbers (Luhn-validated, so order and
  reference numbers stay readable), Pakistani mobile numbers, CNICs and emails. `redactions` records
  the type and segment, never the value.

### How would you handle concurrent uploads?
- The API never transcribes. It validates, stores the audio, creates a job row and puts the job id on a
  **Redis queue**, then returns **202** immediately. Uploads stay fast no matter how busy transcription is.
- **Workers** pull jobs from the queue one at a time. Throughput scales by adding workers
  (`--scale worker=N`) without changing the API. API and workers scale independently.
- Uploads are written to disk in 1 MB chunks, so many simultaneous large uploads don't exhaust memory.
- **Duplicate detection:** the SHA-256 of the file (plus requested language) is checked before any work.
  The same file uploaded twice, even while the first is still processing, returns the existing job instead
  of transcribing again. The queue job id equals the transcription id, so one transcription maps to one queue job.
- Workers use RQ's `SimpleWorker`, which runs jobs in one long-lived process, so the Whisper model is
  loaded once per worker instead of once per job.

### How would you store audio and transcripts?
| Data | Where | Why |
|---|---|---|
| Original audio | Supabase Storage, private bucket, key `{sha256}.{ext}` | Blob storage for large files; content-addressed, so identical files are stored once; original kept so jobs can be re-run with a better model |
| Job + transcript | Postgres `transcriptions` | Status, attempts, errors, language, text, quality, timings; queryable |
| Segments | Postgres `segments` (one row per segment) | Timestamp queries, joins to other data |

- Only the **redacted** text is stored; raw card numbers never reach the database.
- Row Level Security is enabled with no public policies, so only the backend (secret key) can read the data.
- Files that fail validation are rejected **before** anything is stored.
- Saving is idempotent: a re-run deletes the job's old segments before inserting new ones.

### How do you retry or recover failed transcriptions?
Errors are split into three kinds:

1. **Permanent** (bad file, too long, cannot convert): marked `failed` with a clear code immediately. No retry, because it would fail the same way.
2. **Temporary** (network blip, Supabase timeout, crash): retried automatically by RQ with **exponential backoff**, after 30 s, 2 min and 10 min. While waiting, the job shows `status: pending` with `last_error`. After the last retry it is marked `failed`, and RQ keeps it in its failed-job registry (dead-letter queue) for a week.
3. **Lost** (worker killed mid-job, Redis restarted): `python -m app.recover` finds jobs stuck in pending/processing for over 70 minutes (longer than the 1-hour job timeout), re-queues them unless they are still legitimately waiting, and gives up after 6 attempts. In production this would run on a schedule.

Plus:
- `POST /v1/transcriptions/{id}/retry` re-runs a failed job manually (e.g. after a fix).
- The worker skips jobs that are already completed, so a duplicate delivery never redoes work or overwrites a result.
- Redis runs with append-only persistence on a volume, so queued jobs survive restarts.
- `attempts` is recorded on every job.

### How would you expose this as an API?
- REST with FastAPI, versioned under `/v1`, auto-generated OpenAPI docs at `/docs`.
- **Asynchronous job pattern:** `POST` returns `202 Accepted` with an id and `status_url`; the client polls
  `GET /v1/transcriptions/{id}`. This works for any audio length, because no HTTP request waits on inference.
- Consistent error format with stable machine-readable codes and correct HTTP statuses.
- `/health` reports each dependency, for load balancers and monitoring.

### Long audio files
- Size checked while uploading, duration checked with ffprobe **before** storing or transcribing.
- Processing happens in the worker, so long files never cause HTTP timeouts.
- VAD skips silence (hold music, pauses), reducing the audio Whisper has to process.
- Each attempt has a 1-hour timeout so one stuck job can't block a worker forever.

---

## Limitations and next steps

- **Authentication and rate limiting** are not implemented; next would be API keys per client and per-client limits.
- **Webhooks:** add an optional `callback_url` so clients don't need to poll.
- **Redaction is regex-based.** It misses numbers spoken as words ("four two four two") and names. Next: normalise spoken numbers before redaction, and add an NER model (e.g. Microsoft Presidio).
- **Accuracy on Pakistani-accented English and Urdu/English mixing** is limited with small models. Measure word error rate on a labelled sample of real calls and choose the model size from that (`small`/`medium`, or a GPU with `large-v3`).
- **Speaker diarisation** (who said what) for agent/customer calls: record each speaker on a separate stereo channel, or add pyannote.
- **Very long files** could be split into chunks processed in parallel by several workers.
- **Recovery** runs by hand; in production it would run on a schedule (cron), with alerts on failed jobs.
- **Audio retention:** a lifecycle rule to delete raw audio after a set period while keeping transcripts.
- **Automated tests:** add unit tests for validation, redaction and the job state machine, plus a
  nightly run of the real model against labelled clips to track accuracy and speed.
