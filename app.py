"""Gradio entrypoint -- the voice loop.

Speak into the browser; the agent transcribes you, decides whether any of its
n8n-backed tools apply, runs them, and speaks the answer back. Every hop is
timed so the latency budget in the README comes from measurements.

The tool list is not defined here. It comes from whatever is tagged
`agent-tool` in n8n when the turn starts, which is the point of the project.

Hugging Face Spaces requires this file to be named app.py.
"""

from __future__ import annotations

import logging
import os

import gradio as gr

from agent.loop import AgentLoop
from agent.stt import GroqSTT, STTError
from agent.timing import Trace
from agent.tts import ResilientTTS, build_tts
from config import config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

# Voice-to-voice budget. Originally 1500 ms, set before the agent existed; a
# measured tool-calling turn runs ~2100 ms (stt ~840, llm ~480, tool ~65,
# tts ~700), so the target was revised to match reality rather than the other
# way round. Turns that miss it are flagged in the UI.
LATENCY_TARGET_MS = 2500

# Built on first request, not at import: a Space that boots with a missing key
# should show the problem in the UI rather than crash-looping on startup.
_stt: GroqSTT | None = None
_tts: ResilientTTS | None = None
_agent: AgentLoop | None = None


def engines() -> tuple[GroqSTT, ResilientTTS, AgentLoop]:
    global _stt, _tts, _agent
    if _stt is None:
        _stt = GroqSTT()
    if _tts is None:
        _tts = build_tts()
    if _agent is None:
        _agent = AgentLoop()
    return _stt, _tts, _agent


def format_latency(trace: Trace, engine: str, tools: list[str] | None = None) -> str:
    hops = trace.by_hop()
    if not hops:
        return ""
    rows = " · ".join(f"**{name}** {ms:.0f} ms" for name, ms in hops.items())
    if tools:
        rows += f"  \n<sub>tools: {', '.join(tools)}</sub>"
    total = trace.total_ms()
    verdict = (
        "within target" if total < LATENCY_TARGET_MS else f"OVER {LATENCY_TARGET_MS} ms target"
    )
    return (
        f"{rows} · **total** {total:.0f} ms — {verdict}  \n"
        f"<sub>TTS engine: {engine}</sub>"
    )


def respond(
    audio_path: str | None, history: list[dict] | None
) -> tuple[str, str, str | None, str, str, list[dict]]:
    """One turn: audio in -> transcript -> agent -> reply -> audio out.

    Returns (transcript, reply text, audio file, latency markdown, text for the
    browser to speak, updated history). The browser-speak value is non-empty
    only when server-side TTS is unavailable.
    """
    history = history or []
    if not audio_path:
        return "", "", None, "", "", history

    trace = Trace("turn")
    try:
        stt, tts, agent = engines()
    except (STTError, RuntimeError) as exc:
        raise gr.Error(f"Startup failed: {exc}") from exc

    try:
        transcript = stt.transcribe(audio_path, trace=trace)
    except STTError as exc:
        raise gr.Error(f"Transcription failed: {exc}") from exc

    if not transcript:
        return "", "", None, "_No speech detected — try again._", "", history

    turn = agent.run(transcript.text, history=history, trace=trace)
    speech = tts.synthesize(turn.reply, trace=trace)

    if speech.is_browser_fallback and speech.detail:
        log.info("Browser TTS this turn: %s", speech.detail)
    log.info("turn: %s | tools: %s", trace.summary(), turn.tools_used or "none")

    # Carry the full exchange forward, tool calls included, so follow-ups like
    # "what about Friday?" have something to refer back to.
    updated = turn.messages + [{"role": "assistant", "content": turn.reply}]

    return (
        transcript.text,
        turn.reply,
        speech.to_file(),
        format_latency(trace, speech.engine, turn.tools_used),
        turn.reply if speech.is_browser_fallback else "",
        updated,
    )


def describe_tools() -> str:
    """Show what the registry can see right now.

    Doubles as the demo: tag a workflow in n8n, press the button, watch the
    capability appear without anything being redeployed. Skipped workflows are
    listed with their reason, because a tool that silently fails to appear is
    the most confusing thing this system can do.
    """
    try:
        _, _, agent = engines()
        discovery = agent.registry.discover(force=True)
    except Exception as exc:  # noqa: BLE001 -- surfaced in the UI, not swallowed
        return f"**Could not reach n8n:** {exc}"

    lines = []
    if discovery.bindings:
        lines.append(f"**{len(discovery.bindings)} tool(s) available**\n")
        for name, binding in sorted(discovery.bindings.items()):
            args = ", ".join(binding.spec.parameters.get("properties", {})) or "no arguments"
            lines.append(f"- `{name}` — {binding.spec.description}  \n  <sub>{args}</sub>")
    else:
        lines.append(
            f"**No tools found.** Tag a workflow `{config.n8n_tool_tag}` in n8n "
            "and activate it."
        )

    if discovery.skipped:
        lines.append(f"\n**{len(discovery.skipped)} workflow(s) skipped**\n")
        lines += [f"- *{s.workflow_name}* — {s.reason}" for s in discovery.skipped]

    return "\n".join(lines)


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
            "Ask for something your n8n workflows can do. Tools are discovered "
            "at runtime from whatever is tagged `agent-tool` — tag a new "
            "workflow and it is usable on the next turn, with no redeploy."
        )

        with gr.Row():
            with gr.Column():
                mic = gr.Audio(
                    sources=["microphone"],
                    type="filepath",
                    label="Speak, then stop recording",
                )
                with gr.Row():
                    clear = gr.Button("New conversation", variant="secondary")
                    show_tools = gr.Button("What can you do?", variant="secondary")
                tools_md = gr.Markdown()
            with gr.Column():
                transcript = gr.Textbox(label="Heard", lines=2, interactive=False)
                reply_text = gr.Textbox(label="Replied", lines=3, interactive=False)
                reply_audio = gr.Audio(label="Spoken reply", autoplay=True, interactive=False)
                latency = gr.Markdown()

        # Not shown: carries the reply text to the browser's speech synthesiser
        # when server-side TTS is unavailable, and the running conversation.
        to_speak = gr.Textbox(visible=False)
        history = gr.State([])

        turn = mic.stop_recording(
            fn=respond,
            inputs=[mic, history],
            outputs=[transcript, reply_text, reply_audio, latency, to_speak, history],
        )
        turn.then(fn=None, inputs=to_speak, outputs=None, js=SPEAK_JS)

        show_tools.click(fn=describe_tools, outputs=tools_md)
        clear.click(
            fn=lambda: (None, "", "", None, "", "", []),
            outputs=[mic, transcript, reply_text, reply_audio, latency, to_speak, history],
        )

        gr.Markdown(
            f"<sub>STT `{config.stt_model}` · TTS `{config.tts_model}` · "
            f"LLM `{config.llm_model}` (unused in Phase 1)</sub>"
        )

    return demo


demo = build_ui()

if __name__ == "__main__":
    # PaaS hosts assign a port and expect the app on 0.0.0.0; locally this is
    # the usual http://127.0.0.1:7860.
    demo.launch(
        server_name="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1",
        server_port=int(os.environ.get("PORT", 7860)),
    )
