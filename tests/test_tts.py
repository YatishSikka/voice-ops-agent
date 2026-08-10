"""The fallback behaviour is the whole point of tts.py, so it is what gets tested.

Groq TTS is preview-status and terms-gated; the agent must stay audible either
way, and must not pay for a doomed round trip on every turn once it knows the
model is unavailable.
"""

from pathlib import Path

from agent.tts import BrowserTTS, ResilientTTS, Speech, TTSError


class FailingTTS:
    """Stand-in for GroqTTS that always fails with a given status."""

    name = "groq"

    def __init__(self, status: int) -> None:
        self.status = status
        self.calls = 0

    def synthesize(self, text, *, trace=None):
        self.calls += 1
        raise TTSError(f"tts: HTTP {self.status}", self.status)

    def close(self):
        pass


def test_permanent_failure_degrades_for_the_whole_process():
    """A 403 terms gate will still be a 403 next turn -- stop calling it."""
    primary = FailingTTS(403)
    tts = ResilientTTS(primary=primary)

    first, second = tts.synthesize("one"), tts.synthesize("two")

    assert primary.calls == 1
    assert tts.degraded
    assert first.is_browser_fallback and second.is_browser_fallback
    assert "403" in second.detail


def test_temporary_failure_degrades_only_that_turn():
    """A 429 is transient -- the next turn should try the good engine again."""
    primary = FailingTTS(429)
    tts = ResilientTTS(primary=primary)

    tts.synthesize("one")
    tts.synthesize("two")

    assert primary.calls == 2
    assert not tts.degraded


def test_missing_primary_falls_straight_through():
    tts = ResilientTTS(primary=None)
    speech = tts.synthesize("hello")

    assert speech.is_browser_fallback
    assert speech.text == "hello"


def test_speech_writes_audio_for_gradio(tmp_path):
    path = Speech(text="hi", audio=b"RIFF....WAVE", engine="groq").to_file(tmp_path)

    assert path is not None and path.endswith(".wav")
    assert Path(path).read_bytes().startswith(b"RIFF")


def test_browser_fallback_has_no_file_to_play():
    assert BrowserTTS().synthesize("hi").to_file() is None


def _wav(data_size: int, riff_size: int, payload: bytes = b"\x00" * 100) -> bytes:
    """Minimal WAV: RIFF header, fmt chunk, then a data chunk."""
    import struct

    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 24000, 48000, 2, 16)
    return (
        b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
        + fmt
        + b"data" + struct.pack("<I", data_size) + payload
    )


def test_streaming_wav_placeholders_are_repaired():
    """Groq streams WAV with 0xFFFFFFFF sizes; browsers then show Infinity."""
    import struct

    from agent.tts import WAV_PLACEHOLDER_SIZE, repair_streaming_wav

    raw = _wav(WAV_PLACEHOLDER_SIZE, WAV_PLACEHOLDER_SIZE)
    fixed = repair_streaming_wav(raw)

    assert struct.unpack_from("<I", fixed, 4)[0] == len(raw) - 8
    data_at = fixed.index(b"data")
    assert struct.unpack_from("<I", fixed, data_at + 4)[0] == 100
    assert len(fixed) == len(raw)  # audio itself untouched


def test_valid_wav_is_left_alone():
    from agent.tts import repair_streaming_wav

    raw = _wav(data_size=100, riff_size=100 + 44 - 8)
    assert repair_streaming_wav(raw) is raw


def test_non_wav_bytes_pass_through():
    from agent.tts import repair_streaming_wav

    assert repair_streaming_wav(b"ID3\x04not a wav") == b"ID3\x04not a wav"
