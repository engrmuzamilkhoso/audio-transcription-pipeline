import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.errors import AppError

ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm"}
TARGET_SAMPLE_RATE = 16000


@dataclass
class AudioInfo:
    format: str
    codec: str
    duration_s: float
    sample_rate: int
    channels: int


def detect_format(first_bytes: bytes) -> str | None:
    """Look at the first bytes of the file (magic bytes) to find the real format."""
    if first_bytes[:4] == b"RIFF" and first_bytes[8:12] == b"WAVE":
        return "wav"
    if first_bytes[:3] == b"ID3":
        return "mp3"
    if len(first_bytes) > 1 and first_bytes[0] == 0xFF and (first_bytes[1] & 0xE0) == 0xE0:
        return "mp3"
    if first_bytes[:4] == b"fLaC":
        return "flac"
    if first_bytes[:4] == b"OggS":
        return "ogg"
    if first_bytes[4:8] == b"ftyp":
        return "m4a"
    if first_bytes[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    return None


def validate_file(path: Path, filename: str) -> str:
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise AppError("unsupported_extension", f"File type '{extension}' is not allowed", 415)

    size = path.stat().st_size
    if size == 0:
        raise AppError("empty_file", "The file is empty", 400)
    if size > settings.max_file_mb * 1024 * 1024:
        raise AppError("file_too_large", f"File is larger than {settings.max_file_mb} MB", 413)

    with path.open("rb") as f:
        detected = detect_format(f.read(16))
    if detected is None:
        raise AppError("not_audio", "File content is not a supported audio format", 415)
    return detected


def sha256_of_file(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            sha.update(chunk)
    return sha.hexdigest()


def probe(path: Path, detected_format: str) -> AudioInfo:
    """Use ffprobe to read duration, sample rate and channels."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,sample_rate,channels:format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise AppError("corrupt_audio", "Could not read the audio file", 422)

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise AppError("no_audio_stream", "The file has no audio in it", 422)

    stream = streams[0]
    return AudioInfo(
        format=detected_format,
        codec=stream.get("codec_name", "unknown"),
        duration_s=round(float(data.get("format", {}).get("duration", 0)), 2),
        sample_rate=int(stream.get("sample_rate", 0)),
        channels=int(stream.get("channels", 0)),
    )


def check_duration(info: AudioInfo) -> None:
    if info.duration_s < 0.1:
        raise AppError("audio_too_short", "Audio is shorter than 0.1 seconds", 422)
    if info.duration_s > settings.max_duration_s:
        raise AppError(
            "audio_too_long",
            f"Audio is {info.duration_s:.0f}s long; the limit is {settings.max_duration_s:.0f}s",
            413,
        )


def convert_to_wav(source: Path, output_dir: Path) -> Path:
    """Convert any audio format to 16 kHz, mono, 16-bit WAV."""
    output = output_dir / "normalised.wav"
    result = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source),
            "-vn",
            "-ac", "1",
            "-ar", str(TARGET_SAMPLE_RATE),
            "-c:a", "pcm_s16le",
            str(output),
        ],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise AppError("conversion_failed", "Could not convert the audio file", 422)
    return output