"""Deploy the app to a Hugging Face Space.

The Space needs a different README from the repo -- Spaces read YAML
frontmatter for their SDK and entrypoint -- and only a subset of the tree, so
the upload is scripted rather than a git remote. Running it twice is safe.

    python scripts/deploy_space.py                 # create/update the Space
    python scripts/deploy_space.py --secrets       # also push .env values
    python scripts/deploy_space.py --n8n-url URL   # point the Space at n8n

Secrets are only sent when asked for explicitly: they leave this machine for
Hugging Face's secret store, which is a decision worth making on purpose.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huggingface_hub import HfApi  # noqa: E402

from config import config  # noqa: E402

SPACE_SDK = "gradio"
# Pin the SDK the app was developed against; Spaces will otherwise pick its own.
SDK_VERSION = "6.22.0"

# Everything the app imports at runtime. Tests, evals and scripts stay out --
# a Space should carry what it runs and nothing else.
UPLOAD = [
    "app.py",
    "config.py",
    "requirements.txt",
    "agent/__init__.py",
    "agent/_transport.py",
    "agent/loop.py",
    "agent/providers.py",
    "agent/stt.py",
    "agent/timing.py",
    "agent/tts.py",
    "tools/__init__.py",
    "tools/n8n_client.py",
    "tools/registry.py",
    "tools/schema.py",
]

SPACE_README = """---
title: Voice-Ops Agent
emoji: 🎙️
colorFrom: indigo
colorTo: purple
sdk: {sdk}
sdk_version: {sdk_version}
app_file: app.py
pinned: false
short_description: A voice agent whose tools are discovered from n8n at runtime
---

# Voice-Ops Agent

Speak a request; the agent transcribes it, decides which of its tools apply,
runs the matching n8n workflow, and speaks the answer back.

**The tools are not in the code.** Every n8n workflow tagged `agent-tool` is
read over the n8n API when a turn starts, its webhook's JSON Schema is turned
into a tool definition, and the model is offered it. Adding a capability means
building a workflow — no code change, no redeploy.

Press **What can you do?** to see what the registry can currently reach,
including any workflows it skipped and why.

All inference runs on Groq: `whisper-large-v3-turbo` for speech,
`llama-3.3-70b-versatile` for reasoning, Orpheus for the voice. When Groq's
TTS is unavailable the browser's own speech synthesis takes over, so the agent
never goes silent.

Source and design notes: <https://github.com/YatishSikka/voice-ops-agent>

> **Note:** this Space needs a reachable n8n instance to have any tools. With
> none configured it still converses, but will tell you it has nothing to work
> with.
"""

SECRET_KEYS = ("GROQ_API_KEY", "N8N_API_KEY", "N8N_BASE_URL", "TTS_MODEL", "TTS_VOICE")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=None, help="Space id, default <user>/voice-ops-agent")
    parser.add_argument("--private", action="store_true", help="create the Space private")
    parser.add_argument("--secrets", action="store_true", help="push .env values as secrets")
    parser.add_argument("--n8n-url", default=None, help="override N8N_BASE_URL for the Space")
    args = parser.parse_args()

    if not config.hf_token:
        print("HF_TOKEN is unset -- a write token is required.")
        return 1

    api = HfApi(token=config.hf_token)
    user = api.whoami()["name"]
    repo_id = args.repo or f"{user}/voice-ops-agent"

    print(f"Space: {repo_id}")
    api.create_repo(
        repo_id=repo_id,
        repo_type="space",
        space_sdk=SPACE_SDK,
        private=args.private,
        exist_ok=True,
    )

    readme = ROOT / ".space_README.md"
    readme.write_text(
        SPACE_README.format(sdk=SPACE_SDK, sdk_version=SDK_VERSION), encoding="utf-8"
    )
    try:
        api.upload_file(
            path_or_fileobj=str(readme),
            path_in_repo="README.md",
            repo_id=repo_id,
            repo_type="space",
        )
        for relative in UPLOAD:
            source = ROOT / relative
            if not source.is_file():
                print(f"  MISSING {relative}")
                continue
            api.upload_file(
                path_or_fileobj=str(source),
                path_in_repo=relative,
                repo_id=repo_id,
                repo_type="space",
            )
            print(f"  uploaded {relative}")
    finally:
        readme.unlink(missing_ok=True)

    if args.secrets:
        print("\nSecrets:")
        values = {
            "GROQ_API_KEY": config.groq_api_key,
            "N8N_API_KEY": config.n8n_api_key,
            "N8N_BASE_URL": args.n8n_url or config.n8n_base_url,
            "TTS_MODEL": config.tts_model,
            "TTS_VOICE": config.tts_voice,
        }
        for key in SECRET_KEYS:
            value = values.get(key)
            if not value:
                print(f"  skipped {key} (unset)")
                continue
            api.add_space_secret(repo_id=repo_id, key=key, value=value)
            # Never print the value itself.
            print(f"  set {key}")
        if values["N8N_BASE_URL"] and "localhost" in values["N8N_BASE_URL"]:
            print(
                "\n  WARNING: N8N_BASE_URL points at localhost, which the Space "
                "cannot reach.\n  Start a tunnel and rerun with --n8n-url <public url>."
            )
    elif args.n8n_url:
        api.add_space_secret(repo_id=repo_id, key="N8N_BASE_URL", value=args.n8n_url)
        print(f"\nSet N8N_BASE_URL to {args.n8n_url}")

    print(f"\nhttps://huggingface.co/spaces/{repo_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
