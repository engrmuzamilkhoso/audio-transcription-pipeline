import math
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.tokenizer import _LANGUAGE_CODES

from app.config import settings
from app.errors import AppError


@dataclass
class Segment:
    start: float
    end: float
    text: str
    confidence: float
    no_speech_prob: float


@dataclass
class TranscriptionResult:
    language: str
    language_probability: float
    segments: list[Segment]


@lru_cache
def get_model() -> WhisperModel:
    """Load the Whisper model once and reuse it for every request."""
    return WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")


def load_wav(wav_path: Path) -> np.ndarray:
    """Read our 16 kHz mono 16-bit WAV into the number array Whisper works with."""
    with wave.open(str(wav_path), "rb") as w:
        raw = w.readframes(w.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16)
    return samples.astype(np.float32) / 32768.0


def clean_language(language: str | None) -> str | None:
    """Empty means auto-detect. Anything else must be a language code Whisper knows."""
    if language is None or language.strip() == "":
        return None
    language = language.strip().lower()
    if language not in _LANGUAGE_CODES:
        raise AppError("invalid_language", f"'{language}' is not a supported language code, e.g. en or ur", 400)
    return language


def transcribe(wav_path: Path, language: str | None = None) -> TranscriptionResult:
    language = clean_language(language)
    audio = load_wav(wav_path)
    segments, info = get_model().transcribe(
        audio,
        language=language,
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
    )

    result = []
    for s in segments:
        result.append(
            Segment(
                start=round(s.start, 2),
                end=round(s.end, 2),
                text=s.text.strip(),
                confidence=round(min(1.0, math.exp(s.avg_logprob)), 3),
                no_speech_prob=round(s.no_speech_prob, 3),
            )
        )

    return TranscriptionResult(
        language=info.language,
        language_probability=round(info.language_probability, 3),
        segments=result,
    )