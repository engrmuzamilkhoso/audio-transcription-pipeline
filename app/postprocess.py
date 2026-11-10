import re
from dataclasses import dataclass

from app.transcriber import Segment

# A segment is probably invented by Whisper (e.g. "Thanks for watching!" on silence)
# when it thinks there is likely no speech AND it is not confident in the words.
NO_SPEECH_THRESHOLD = 0.6
HALLUCINATION_CONFIDENCE = 0.4

# Segments below this confidence are kept but marked for a human to check.
LOW_CONFIDENCE = 0.5

PII_PATTERNS = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("CNIC", re.compile(r"\b\d{5}-?\d{7}-?\d\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+92|0092|0)[ -]?3\d{2}[ -]?\d{7}(?!\d)")),
    ("CARD_NUMBER", re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")),
]


@dataclass
class CleanSegment:
    id: int
    start: float
    end: float
    text: str
    confidence: float
    needs_review: bool


@dataclass
class CleanTranscript:
    text: str
    segments: list[CleanSegment]
    redactions: list[dict]
    dropped_segments: int
    quality: dict


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()        # many spaces -> one space
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)    # "word ," -> "word,"
    return text


def passes_luhn(digits: str) -> bool:
    """The checksum every real card number passes. Stops us hiding order IDs."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def redact(text: str) -> tuple[str, list[str]]:
    found = []

    for label, pattern in PII_PATTERNS:
        def replace(match: re.Match, label=label) -> str:
            if label == "CARD_NUMBER":
                digits = re.sub(r"\D", "", match.group())
                if not passes_luhn(digits):
                    return match.group()   # long number, but not a card: keep it
            found.append(label)
            return f"[{label}]"

        text = pattern.sub(replace, text)

    return text, found


def process(segments: list[Segment]) -> CleanTranscript:
    kept: list[CleanSegment] = []
    redactions: list[dict] = []
    dropped = 0
    previous = None

    for seg in segments:
        text = clean_text(seg.text)
        comparable = text.lower().strip(" .!?")

        is_empty = text == ""
        is_hallucination = (
            seg.no_speech_prob > NO_SPEECH_THRESHOLD and seg.confidence < HALLUCINATION_CONFIDENCE
        )
        is_repeat = comparable == previous

        if is_empty or is_hallucination or is_repeat:
            dropped += 1
            continue
        previous = comparable

        segment_id = len(kept)
        text, found = redact(text)
        for pii_type in found:
            redactions.append({"type": pii_type, "segment_id": segment_id})

        kept.append(
            CleanSegment(
                id=segment_id,
                start=seg.start,
                end=seg.end,
                text=text,
                confidence=seg.confidence,
                needs_review=seg.confidence < LOW_CONFIDENCE,
            )
        )

    review_count = sum(1 for s in kept if s.needs_review)
    average = round(sum(s.confidence for s in kept) / len(kept), 3) if kept else 0.0

    return CleanTranscript(
        text=" ".join(s.text for s in kept),
        segments=kept,
        redactions=redactions,
        dropped_segments=dropped,
        quality={
            "avg_confidence": average,
            "low_confidence_segments": review_count,
            "dropped_segments": dropped,
            "needs_review": len(kept) == 0 or review_count > 0,
        },
    )