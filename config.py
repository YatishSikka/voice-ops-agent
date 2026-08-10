"""Central config. Everything the app needs comes from env vars.

The n8n settings are deliberately just a base URL + API key so that moving from
n8n Cloud to a self-hosted instance is a two-variable change (see README,
"Day-15 migration").
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is not None:
        value = value.strip()
    return value or None


@dataclass(frozen=True)
class Config:
    # Which LLMProvider agent/providers.py builds: "groq" or "claude".
    # Swapping this is the whole provider migration -- see providers.build_llm().
    llm_provider: str

    groq_api_key: str | None
    groq_base_url: str

    anthropic_api_key: str | None
    anthropic_model: str

    stt_model: str
    llm_model: str
    tts_model: str
    tts_voice: str

    n8n_base_url: str | None
    n8n_api_key: str | None
    n8n_tool_tag: str

    telegram_bot_token: str | None
    telegram_chat_id: str | None

    hf_token: str | None

    registry_cache_ttl: int
    max_tool_iterations: int

    @classmethod
    def load(cls) -> "Config":
        return cls(
            llm_provider=(_env("LLM_PROVIDER", "groq") or "groq").lower(),
            groq_api_key=_env("GROQ_API_KEY"),
            groq_base_url=_env("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            anthropic_model=_env("ANTHROPIC_MODEL", "claude-sonnet-5"),
            stt_model=_env("STT_MODEL", "whisper-large-v3-turbo"),
            llm_model=_env("LLM_MODEL", "llama-3.3-70b-versatile"),
            # playai-tts was decommissioned; Orpheus is the current Groq TTS and
            # needs one-time terms acceptance in the console before it answers.
            tts_model=_env("TTS_MODEL", "canopylabs/orpheus-v1-english"),
            # Orpheus voices: autumn diana hannah austin daniel troy
            tts_voice=_env("TTS_VOICE", "autumn"),
            # rstrip("/") so callers can always join with a leading-slash path
            n8n_base_url=(_env("N8N_BASE_URL") or "").rstrip("/") or None,
            n8n_api_key=_env("N8N_API_KEY"),
            n8n_tool_tag=_env("N8N_TOOL_TAG", "agent-tool"),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            hf_token=_env("HF_TOKEN"),
            registry_cache_ttl=int(_env("REGISTRY_CACHE_TTL", "60")),
            max_tool_iterations=int(_env("MAX_TOOL_ITERATIONS", "6")),
        )


config = Config.load()
