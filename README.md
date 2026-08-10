# Voice-Ops Agent

A voice agent whose capabilities are **discovered at runtime, not compiled in**.

Every n8n workflow tagged `agent-tool` is fetched over the n8n REST API, its
webhook input is translated into a JSON Schema, and the result is handed to the
LLM as a callable tool. Adding a skill to the agent means building a workflow in
the n8n UI — no code change, no redeploy, no restart.

The second idea is that slow work does not have to block the conversation. The
agent hands a long-running job to n8n, ends the turn, and messages you on
Telegram when it finishes — including as a voice note.

> **Status: early.** Phase 0 is closed — all three preflight gates pass against
> live services. Phase 1 runs locally: speak into the browser and the agent
> transcribes and speaks back, with per-hop timings on screen. Not yet built:
> the tool registry (Phase 2), which is the reason the project exists, and
> deployment to a Space.

---

## Architecture

```
Browser mic (Gradio, HF Space)
        │  audio in
        ▼
┌──────────────────────────────────────┐
│  Voice loop  (app.py)                │
│    Groq whisper-large-v3-turbo  STT  │
│    Groq llama-3.3-70b + tools  LLM   │
│    Groq TTS / browser speech   TTS   │
└───────────────┬──────────────────────┘
                │ tool call
                ▼
┌──────────────────────────────────────┐
│  Tool Registry            ← the hook │
│    GET /api/v1/workflows             │
│    filter tag == "agent-tool"        │
│    webhook node → JSON Schema        │
│    emit provider tool definitions    │
│    cached 60s, refreshed per session │
└───────────────┬──────────────────────┘
                │ POST webhook
                ▼
┌──────────────────────────────────────┐
│  n8n   (Cloud trial → self-hosted)   │
│    Gmail · Calendar · Notion · GitHub │
└───────────────┬──────────────────────┘
                │ on completion
                ▼
        Telegram bot   ← the async callback
```

`tools/registry.py` and `tools/schema.py` are the project. Everything else is
plumbing around them.

## Design notes

Three decisions that are load-bearing rather than incidental:

**The LLM provider owns conversation history, not just the request.** Vendors
diverge most in how tool results re-enter the transcript — OpenAI-style APIs use
a `role: "tool"` message keyed by `tool_call_id`, Anthropic uses `tool_result`
blocks inside a *user* message. So `LLMProvider` exposes `assistant_message()`
and `tool_result_message()` alongside `chat()`, which keeps the agent loop
vendor-neutral and makes `LLM_PROVIDER` a one-line swap.

**TTS separates permanent failure from temporary.** Groq's speech models are
preview-status and terms-gated. A 403 or 404 will still be true next turn, so
the engine degrades to browser `speechSynthesis` for the life of the process and
logs once; a 429 degrades for that turn only. `synthesize()` never raises — an
agent that goes silent is a worse outcome than one that speaks through the
browser.

**Latency is instrumented from the first commit.** A p95 cannot be retrofitted.
Every network hop records a span in `agent/timing.py`, and the eval harness
aggregates them into the table below.

## Free-tier budget

The whole system runs at **$0**.

| Hop | Service | Free allowance |
|---|---|---|
| STT | Groq `whisper-large-v3-turbo` | 2,000 req/day, 28,800 audio-sec/day |
| LLM | Groq `llama-3.3-70b-versatile` | ~30 RPM, 1,000 RPD, 12K TPM |
| TTS | Groq TTS → browser `speechSynthesis` fallback | preview-gated |
| Orchestration | n8n community edition, self-hosted | unlimited |
| Hosting | Hugging Face Space, Gradio on ZeroGPU | 2 Spaces, sleeps when idle |
| Callback | Telegram Bot API | unlimited |
| Tracing | Langfuse Cloud | 50K observations/mo |

The ZeroGPU daily quota is untouched: it only burns inside `@spaces.GPU`
functions, and this app has none — every model call goes to Groq over HTTP.

## Getting started

```bash
git clone https://github.com/YatishSikka/voice-ops-agent.git
cd voice-ops-agent
pip install -r requirements.txt

cp .env.example .env      # then fill it in
python scripts/preflight.py
```

### Running n8n

n8n runs locally on SQLite — no Docker, ~400 MB:

```bash
npx n8n start             # http://localhost:5678
```

Create the owner account on first launch, then **Settings → n8n API → Create an
API key** and put it in `.env` alongside `N8N_BASE_URL=http://localhost:5678`.
That screen is only present on self-hosted instances.

To confirm the public API is live before you have a key:

```bash
curl -i http://localhost:5678/api/v1/workflows
# 401 "'X-N8N-API-KEY' header required"  -> API is enabled, good
# 404                                     -> API is disabled (this is what Cloud's trial does)
```

Verified on n8n 2.33.7 with Node 24, despite n8n officially targeting Node
20/22. A `Failed to refresh MCP registry` line on startup is harmless — it is
n8n fetching its own public server catalogue, unrelated to this project.

Deploying to a Space later means giving n8n a public address — a Cloudflare
Tunnel is enough, and only `N8N_BASE_URL` changes.

`preflight.py` is a gate, not a formality. Three things can invalidate the whole
design, so it verifies all three and prints a pass/fail table:

| Gate | What it proves | If it fails |
|---|---|---|
| Hugging Face | Token valid, email verified, account >30 days | ZeroGPU hosting is blocked — fall back to a Static Space with the backend behind a Cloudflare Tunnel |
| n8n | `GET /api/v1/workflows` answers, and tag filtering works | The registry has nothing to read — switch to self-hosted `npx n8n` immediately |
| Groq | STT, LLM **and** TTS each accept a real call | A listed model is not a callable one; a gated TTS drops to the browser fallback |

It exits non-zero on failure and takes `--json` for CI. A `SKIP` is not a pass —
an unverified gate is an unresolved one.

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
ruff check .
```

## Configuration

All configuration is environment variables, read once in `config.py`. See
[.env.example](.env.example) for the annotated list. Two are worth calling out:

- `N8N_BASE_URL` / `N8N_API_KEY` — the registry reaches n8n through nothing
  else, so migrating from n8n Cloud to a self-hosted instance is a two-variable
  change.
- `LLM_PROVIDER` — `groq` (default) or `claude`. STT and TTS stay on Groq either
  way.

## Roadmap

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Preflight gate checks | **Done** — all gates green |
| 1 | Voice loop: mic → STT → echo → TTS, deployed | **Works locally**; Space deployment pending |
| 2 | **Tool registry** — n8n workflows as runtime-discovered tools | Pending |
| 3 | Async callback — long jobs return via Telegram voice note | Pending |
| 4 | ~8 workflows, rate-limit queue, confirmation gate on destructive tools | Pending |
| 5 | Eval harness, Langfuse tracing, CI, measured latency table | Pending |

The tool list freezes at the end of Phase 4. That is the scope-creep guard.

## Honest limitations

- **Latency target is ~1.5s voice-to-voice, not the ~900ms of a telephony
  agent.** Browser capture plus HTTP round-trip STT costs the difference. Early
  single-turn measurements on the echo loop land at **STT 320–920 ms, TTS
  ~800 ms, ~1.1–1.2 s end to end** — but these are individual runs on one
  fixture, not a distribution, and they predate the LLM hop that Phase 2 adds.
  The p50/p95 table here stays empty until the eval harness produces one,
  because estimates in a README are guesses with formatting.
- **Free-tier ceilings are real.** Groq's ~30 RPM is comfortable for one
  speaker and not for a demo audience. Evals run serially for this reason.
- **The Space sleeps when idle**, so the first request after a quiet period pays
  a cold start.
- **No persistent disk on Spaces.** Durable state lives in n8n; session state is
  in-memory by design.
- **`ClaudeProvider` is written but unverified against the live API.** It is the
  swap path, not the shipping path.
- **n8n runs self-hosted, and not by preference.** n8n Cloud has no free tier
  any more, and its 14-day trial [disables the public
  API](https://docs.n8n.io/connect/n8n-api/) — the `Settings → n8n API` screen
  is hidden and `/api/v1/*` is switched off server-side. Since runtime workflow
  discovery *is* this project, Cloud is unusable below the paid Starter tier.
  The community edition has the same API for free, so the agent talks to a
  self-hosted instance. Preflight's gate 2 exists to catch precisely this.

## License

MIT
