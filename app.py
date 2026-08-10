"""Gradio entrypoint -- the Phase 1 voice loop.

Speak into the browser, get the transcript back as speech. There is no agent
intelligence here yet: the reply is an echo. What this phase proves is that the
audio round trip works and that every hop is being timed, so the latency budget
in the README comes from measurements rather than hope.

Phase 2 replaces `compose_reply()` with the tool-calling agent loop. That
function is the seam; nothing else in this file should need to change.

Hugging Face Spaces requires this file to be named app.py.
"""

from __future__ import annotations

import logging

import gradio as gr

from agent.stt import GroqSTT, STTError
from agent.timing import Trace
from agent.tts import ResilientTTS, build_tts
from config import config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

# Built on first request, not at import: a Space that boots with a missing key
# should show the problem in the UI rather than crash-looping on startup.
_stt: GroqSTT | None = None
_tts: ResilientTTS | None = None


def engines() -> tuple[GroqSTT, ResilientTTS]:
    global _stt, _tts
    if _stt is None:
        _stt = GroqSTT()
    if _tts is None:
        _tts = build_tts()
    return _stt, _tts


def compose_reply(text: str) -> str:
    """Phase 1: echo. Phase 2: the agentic tool-calling loop lands here."""
    return f"You said: {text}"


def format_latency(trace: Trace, engine: str) -> str:
    hops = trace.by_hop()
    if not hops:
        return ""
    rows = " · ".join(f"**{name}** {ms:.0f} ms" for name, ms in hops.items())
    total = trace.total_ms()
    # The target is voice-to-voice p95 < 1500 ms; flag the turns that miss it
    # so the number stays honest while it is still cheap to fix. Plain ASCII
    # markers, because this string is also logged to Windows consoles that
    # cannot encode emoji.
    verdict = "within target" if total < 1500 else "OVER 1500 ms target"
    return (
        f"{rows} · **total** {total:.0f} ms — {verdict}  \n"
        f"<sub>TTS engine: {engine}</sub>"
    )


def respond(audio_path: str | None) -> tuple[str, str | None, str, str]:
    """One turn: audio in -> transcript -> reply -> audio out.

    Returns (transcript, audio file, latency markdown, text for the browser to
    speak). The last value is non-empty only when server-side TTS is
    unavailable, and drives the speechSynthesis fallback in the browser.
    """
    if not audio_path:
        return "", None, "", ""

    trace = Trace("turn")
    try:
        stt, tts = engines()
    except (STTError, RuntimeError) as exc:
        raise gr.Error(f"Startup failed: {exc}") from exc

    try:
        transcript = stt.transcribe(audio_path, trace=trace)
    except STTError as exc:
        raise gr.Error(f"Transcription failed: {exc}") from exc

    if not transcript:
        return "", None, "_No speech detected — try again._", ""

    reply = compose_reply(transcript.text)
    speech = tts.synthesize(reply, trace=trace)

    if speech.is_browser_fallback and speech.detail:
        log.info("Browser TTS this turn: %s", speech.detail)

    log.info("turn: %s", trace.summary())
    return (
        transcript.text,
        speech.to_file(),
        format_latency(trace, speech.engine),
        reply if speech.is_browser_fallback else "",
    )


# The browser fallback: Gradio has no audio to play, so the client speaks the
# text itself. Cancel first, or a fast second turn overlaps the first.
SPEAK_JS = """
(text) => {
  if (text && text.trim() && window.speechSynthesis) {
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
  }
  return [];
}
"""


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="Voice-Ops Agent", fill_height=True) as demo:
        gr.Markdown(
            "# Voice-Ops Agent\n"
            "**Phase 1 — voice loop.** Speak and it repeats you back. "
            "No tools are wired yet; this exists to prove the audio round trip "
            "and to measure every hop."
        )

        with gr.Row():
            with gr.Column():
                mic = gr.Audio(
                    sources=["microphone"],
                    type="filepath",
                    label="Speak, then stop recording",
                )
                clear = gr.Button("Clear", variant="secondary")
            with gr.Column():
                transcript = gr.Textbox(label="Heard", lines=3, interactive=False)
                reply_audio = gr.Audio(label="Reply", autoplay=True, interactive=False)
                latency = gr.Markdown()

        # Not shown: carries the reply text to the browser's speech synthesiser
        # when server-side TTS is unavailable.
        to_speak = gr.Textbox(visible=False)

        turn = mic.stop_recording(
            fn=respond,
            inputs=mic,
            outputs=[transcript, reply_audio, latency, to_speak],
        )
        turn.then(fn=None, inputs=to_speak, outputs=None, js=SPEAK_JS)

        clear.click(
            fn=lambda: (None, "", None, "", ""),
            outputs=[mic, transcript, reply_audio, latency, to_speak],
        )

        gr.Markdown(
            f"<sub>STT `{config.stt_model}` · TTS `{config.tts_model}` · "
            f"LLM `{config.llm_model}` (unused in Phase 1)</sub>"
        )

    return demo


demo = build_ui()

if __name__ == "__main__":
    demo.launch()
