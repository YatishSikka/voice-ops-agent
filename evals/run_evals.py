"""Eval harness -- turns "it works" into numbers.

Measures four things the README makes claims about:

  * **tool selection** -- did the agent call the right tool, including the
    cases where the right answer is no tool at all;
  * **task success** -- did the reply contain what the tool actually returned;
  * **latency** -- p50/p95 per hop, since a mean hides the turns users notice;
  * **transcription** -- word error rate over spoken fixtures.

Runs serially on purpose. Groq's free tier allows ~30 requests a minute and a
parallel run would spend the whole budget on 429s, measuring the backoff rather
than the agent.

    python evals/run_evals.py                 # text scenarios
    python evals/run_evals.py --audio         # add STT/WER over fixtures
    python evals/run_evals.py --out card.md   # write the scorecard
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.loop import AgentLoop
from agent.stt import GroqSTT
from agent.timing import Trace
from tools.registry import ToolRegistry

SCENARIOS = Path(__file__).parent / "scenarios.yaml"
FIXTURES = Path(__file__).parent / "fixtures"

# Space between turns. This is not only about avoiding 429s: retry backoff
# happens *inside* the timed span, so a rate-limited run would report the
# backoff as latency. Pacing keeps the numbers measurements rather than
# artefacts of the free tier.
PACING_S = 4.0


@dataclass
class Outcome:
    id: str
    said: str
    reply: str = ""
    tools_called: list[str] = field(default_factory=list)
    tools_expected: list[str] = field(default_factory=list)
    tool_ok: bool = False
    content_ok: bool = True
    failures: list[str] = field(default_factory=list)
    latency_ms: dict[str, float] = field(default_factory=dict)
    total_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.tool_ok and self.content_ok and self.error is None


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile -- no interpolation, so p95 is a real sample."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered) + 0.5) - 1))
    return ordered[index]


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Standard WER: edit distance over words, normalised by reference length."""
    ref = [w.strip(".,!?'\"").lower() for w in reference.split()]
    hyp = [w.strip(".,!?'\"").lower() for w in hypothesis.split()]
    if not ref:
        return 0.0 if not hyp else 1.0

    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        current = [i]
        for j, h in enumerate(hyp, start=1):
            current.append(
                previous[j - 1] if r == h
                else 1 + min(previous[j - 1], previous[j], current[j - 1])
            )
        previous = current
    return previous[-1] / len(ref)


def check_content(scenario: dict[str, Any], reply: str) -> list[str]:
    """Assertions about the reply itself. Returns human-readable failures."""
    failures: list[str] = []
    lowered = reply.lower()

    mentions = scenario.get("must_mention") or []
    if mentions:
        hits = [m for m in mentions if m.lower() in lowered]
        # match: any -- the model has several valid ways to say the same thing.
        needed = 1 if scenario.get("match") == "any" else len(mentions)
        if len(hits) < needed:
            missing = [m for m in mentions if m not in hits]
            failures.append(f"missing {missing}")

    for token in scenario.get("forbid_in_reply") or []:
        if token in reply:
            failures.append(f"contains {token!r} (spoken output must be plain)")

    limit = scenario.get("max_reply_chars")
    if limit and len(reply) > limit:
        failures.append(f"reply {len(reply)} chars > {limit}")

    return failures


def run_scenario(scenario: dict[str, Any], agent: AgentLoop) -> Outcome:
    outcome = Outcome(
        id=scenario["id"],
        said=scenario["say"],
        tools_expected=list(scenario.get("expect_tools") or []),
    )
    trace = Trace(scenario["id"])

    try:
        turn = agent.run(scenario["say"], trace=trace)
    except Exception as exc:  # noqa: BLE001 -- an eval must record, not crash
        outcome.error = f"{type(exc).__name__}: {exc}"
        return outcome

    outcome.reply = turn.reply
    outcome.tools_called = turn.tools_used
    outcome.tool_ok = sorted(set(turn.tools_used)) == sorted(set(outcome.tools_expected))
    if not outcome.tool_ok:
        outcome.failures.append(
            f"tools {sorted(set(turn.tools_used))} != expected {sorted(set(outcome.tools_expected))}"
        )

    outcome.failures += check_content(scenario, turn.reply)
    outcome.content_ok = not check_content(scenario, turn.reply)

    if turn.error:
        outcome.error = turn.error

    # Argument checking is advisory: the model may legitimately normalise
    # "tomorrow afternoon" to "tomorrow", so a mismatch is reported, not failed.
    for key, value in (scenario.get("expect_args") or {}).items():
        actual = turn.invocations[0].arguments.get(key) if turn.invocations else None
        if actual is None or str(value).lower() not in str(actual).lower():
            outcome.failures.append(f"arg {key}={actual!r}, wanted ~{value!r} (advisory)")

    # A `then` block runs a second turn on the first one's history. Some
    # behaviour only exists across turns -- a confirmation gate is not a
    # confirmation gate unless the follow-up actually goes through.
    follow_up = scenario.get("then")
    if follow_up:
        history = turn.messages + [{"role": "assistant", "content": turn.reply}]
        try:
            second = agent.run(follow_up["say"], history=history, trace=trace)
        except Exception as exc:  # noqa: BLE001
            outcome.error = f"follow-up: {type(exc).__name__}: {exc}"
            return outcome

        outcome.reply = f"{turn.reply} || {second.reply}"
        outcome.tools_called += second.tools_used
        expected_second = list(follow_up.get("expect_tools") or [])
        if sorted(set(second.tools_used)) != sorted(set(expected_second)):
            outcome.tool_ok = False
            outcome.failures.append(
                f"follow-up tools {sorted(set(second.tools_used))} != {sorted(set(expected_second))}"
            )
        second_failures = check_content(follow_up, second.reply)
        outcome.failures += [f"follow-up: {f}" for f in second_failures]
        outcome.content_ok = outcome.content_ok and not second_failures

    outcome.latency_ms = trace.by_hop()
    outcome.total_ms = trace.total_ms()
    return outcome


def run_audio(stt: GroqSTT, scenarios: list[dict[str, Any]]) -> list[tuple[str, float, str, float]]:
    """Transcribe the spoken fixtures and score them. Returns per-clip rows."""
    rows = []
    for scenario in scenarios:
        path = FIXTURES / f"{scenario['id']}.wav"
        if not path.is_file():
            print(f"  {scenario['id']}: no fixture, skipped")
            continue
        trace = Trace("stt")
        transcript = stt.transcribe(str(path), trace=trace)
        wer = word_error_rate(scenario["say"], transcript.text)
        rows.append((scenario["id"], wer, transcript.text, trace.total_ms()))
        print(f"  {scenario['id']}: WER {wer:.0%} in {trace.total_ms():.0f} ms -> {transcript.text!r}")
        time.sleep(PACING_S)
    return rows


def scorecard(outcomes: list[Outcome], audio_rows: list, elapsed_s: float) -> str:
    passed = [o for o in outcomes if o.passed]
    tool_ok = [o for o in outcomes if o.tool_ok]
    no_tool_expected = [o for o in outcomes if not o.tools_expected]
    restraint = [o for o in no_tool_expected if not o.tools_called]

    totals = [o.total_ms for o in outcomes if o.total_ms]
    hops: dict[str, list[float]] = {}
    for outcome in outcomes:
        for hop, ms in outcome.latency_ms.items():
            hops.setdefault(hop, []).append(ms)

    prompt_tokens = sum(o.prompt_tokens for o in outcomes)
    completion_tokens = sum(o.completion_tokens for o in outcomes)

    lines = [
        "# Eval scorecard",
        "",
        f"{len(outcomes)} scenarios, run serially in {elapsed_s:.0f}s.",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Task success | **{len(passed)}/{len(outcomes)}** ({len(passed)/max(1,len(outcomes)):.0%}) |",
        f"| Tool selection | **{len(tool_ok)}/{len(outcomes)}** ({len(tool_ok)/max(1,len(outcomes)):.0%}) |",
        f"| Restraint (no tool when none needed) | **{len(restraint)}/{len(no_tool_expected)}** |",
    ]
    if audio_rows:
        wers = [row[1] for row in audio_rows]
        lines.append(
            f"| Word error rate | **{statistics.mean(wers):.1%}** mean over {len(wers)} clips |"
        )
    if prompt_tokens or completion_tokens:
        lines.append(f"| Tokens | {prompt_tokens} in / {completion_tokens} out |")

    lines += ["", "## Latency", "", "| Hop | p50 | p95 | max |", "|---|---|---|---|"]
    for hop, values in sorted(hops.items()):
        lines.append(
            f"| {hop} | {percentile(values, 50):.0f} ms | "
            f"{percentile(values, 95):.0f} ms | {max(values):.0f} ms |"
        )
    if totals:
        lines.append(
            f"| **agent turn** | **{percentile(totals, 50):.0f} ms** | "
            f"**{percentile(totals, 95):.0f} ms** | {max(totals):.0f} ms |"
        )
    lines += [
        "",
        (
            "<sub>Agent turn excludes speech in and out. Add measured STT and "
            "TTS for the voice-to-voice figure.</sub>"
        ),
        "",
    ]

    failures = [o for o in outcomes if not o.passed]
    if failures:
        lines += ["## Failures", ""]
        for outcome in failures:
            lines.append(f"**{outcome.id}** — said *{outcome.said!r}*")
            for failure in outcome.failures or ([outcome.error] if outcome.error else []):
                lines.append(f"  - {failure}")
            lines.append(f"  - replied: *{outcome.reply[:160]!r}*")
            lines.append("")
    else:
        lines += ["All scenarios passed.", ""]

    if audio_rows:
        lines += ["## Transcription", "", "| Clip | WER | Heard |", "|---|---|---|"]
        for clip, wer, heard, _ in audio_rows:
            lines.append(f"| {clip} | {wer:.0%} | {heard} |")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--audio", action="store_true", help="also score STT over fixtures")
    parser.add_argument("--out", default="evals/results/scorecard.md", help="scorecard path")
    parser.add_argument("--only", default=None, help="run one scenario by id")
    args = parser.parse_args()

    scenarios = yaml.safe_load(SCENARIOS.read_text(encoding="utf-8"))
    if args.only:
        scenarios = [s for s in scenarios if s["id"] == args.only]
        if not scenarios:
            print(f"No scenario {args.only!r}")
            return 1

    registry = ToolRegistry()
    print(f"Tools discovered: {[s.name for s in registry.specs()] or 'none'}\n")

    agent = AgentLoop(registry=registry)
    started = time.time()
    outcomes = []
    for index, scenario in enumerate(scenarios, start=1):
        outcome = run_scenario(scenario, agent)
        outcomes.append(outcome)
        mark = "pass" if outcome.passed else "FAIL"
        print(f"[{index:>2}/{len(scenarios)}] {mark}  {outcome.id} ({outcome.total_ms:.0f} ms)")
        for failure in outcome.failures:
            print(f"          {failure}")
        time.sleep(PACING_S)

    audio_rows = []
    if args.audio:
        print("\nTranscription:")
        audio_rows = run_audio(GroqSTT(), [s for s in scenarios if s.get("audio")])

    card = scorecard(outcomes, audio_rows, time.time() - started)
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(card, encoding="utf-8")

    print("\n" + card.split("## Latency")[0])
    print(f"Scorecard written to {out.relative_to(ROOT)}")

    return 0 if all(o.passed for o in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
