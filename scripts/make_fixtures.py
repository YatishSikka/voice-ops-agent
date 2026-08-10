"""Generate spoken fixtures for the eval harness.

Uses the Windows built-in speech engine (SAPI) rather than Groq's TTS on
purpose: scoring Whisper against audio produced by a *different* engine is a
fairer test than feeding it speech from the same stack. It also costs nothing
and works offline.

Windows only. On other platforms, record the lines by hand or substitute
`say` (macOS) / `espeak` (Linux) -- the eval harness only needs a WAV per
scenario id.

    python scripts/make_fixtures.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "evals" / "scenarios.yaml"
FIXTURES = ROOT / "evals" / "fixtures"

PS_TEMPLATE = """
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Rate = 0
$s.SetOutputToWaveFile("{path}")
$s.Speak("{text}")
$s.Dispose()
"""


def main() -> int:
    if sys.platform != "win32":
        print("Windows only -- see the module docstring for alternatives.")
        return 1

    scenarios = [s for s in yaml.safe_load(SCENARIOS.read_text(encoding="utf-8")) if s.get("audio")]
    if not scenarios:
        print("No scenarios marked `audio: true`.")
        return 0

    FIXTURES.mkdir(parents=True, exist_ok=True)
    for scenario in scenarios:
        path = FIXTURES / f"{scenario['id']}.wav"
        # Quotes would terminate the PowerShell string literal.
        text = scenario["say"].replace('"', "").replace("'", "")
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", PS_TEMPLATE.format(path=path, text=text)],
            check=True,
            capture_output=True,
        )
        print(f"  {scenario['id']:<28} {path.stat().st_size / 1024:>6.1f} KiB  {text!r}")

    print(f"\n{len(scenarios)} fixture(s) in {FIXTURES.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
