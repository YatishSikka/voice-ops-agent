# Voice-Ops Agent

A voice agent you talk to on Telegram, whose capabilities are **discovered at
runtime, not compiled in**.

Every n8n workflow tagged `agent-tool` is fetched over the n8n REST API, its
webhook input is translated into a JSON Schema, and the result is handed to the
LLM as a callable tool. Adding a skill to the agent means building a workflow in
the n8n UI — no code change, no redeploy, no restart.

The second idea is that slow work does not have to block the conversation. The
agent hands a long-running job to n8n, ends the turn, and messages you on
Telegram when it finishes — as a spoken voice note, so the interaction stays
voice-first past the end of the conversation.

> **Status.** Works end to end, locally. Send the bot a voice message asking
> *"what's on my calendar on Thursday"* and it transcribes you, resolves the
> date, queries the real Google Calendar, and replies with a spoken voice note.
> Slow jobs hand off and report back later; destructive tools are confirmed
> before they fire. Not yet done: hosting, and one of the three workflows is
> still a stub.

---

## Architecture

```
Telegram voice message
        │  long poll -- no inbound port, no public URL
        ▼
┌──────────────────────────────────────┐
│  bot.py                              │
│    Groq whisper-large-v3-turbo  STT  │
│    Groq llama-3.3-70b + tools  LLM   │
│    Groq Orpheus                TTS   │
└───────────────┬──────────────────────┘
                │ tool call
                ▼
┌──────────────────────────────────────┐
│  Tool Registry            ← the hook │
│    GET /api/v1/workflows             │
│    filter tag == "agent-tool"        │
│    webhook node → JSON Schema        │
│    emit provider tool definitions    │
│    cached 60s, refreshed per turn    │
└───────────────┬──────────────────────┘
                │ POST webhook
                ▼
┌──────────────────────────────────────┐
│  n8n   (self-hosted, community)      │
│    Google Calendar · and whatever    │
│    else you tag                      │
└───────────────┬──────────────────────┘
                │ on completion (background jobs)
                ▼
        voice note back to the same chat
```

**Why Telegram and not a web page.** It is already on the phone, it records and
plays voice natively, and `getUpdates` long polling means the bot reaches *out*
to Telegram — no inbound port, no public URL, no tunnel, and no hosting tier
that has to permit web apps. That last point is not incidental: Hugging Face
made Gradio Spaces paid mid-build, and this design makes the question moot.


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
terms-gated and were once decommissioned mid-build. A 403 or 404 will still be
true next turn, so the engine degrades for the life of the process and logs
once; a 429 degrades for that turn only. `synthesize()` never raises, and the
bot sends text when there is no audio — an agent that goes silent is worse than
one that writes.

**Dates are resolved by the model, not parsed by the workflow.** The tool takes
`YYYY-MM-DD` and the system prompt carries a small table of real dates —
today, tomorrow, day after tomorrow, and the coming week by weekday name. The
first version let the workflow match on words, which turned "Thursday" into
today and "day after tomorrow" into tomorrow. The second version gave the model
only today's date, and it still got "day after tomorrow" wrong about half the
time. Listing the dates turns arithmetic into lookup.

**Latency is instrumented from the first commit.** A p95 cannot be retrofitted.
Every network hop records a span in `agent/timing.py`, and the eval harness
aggregates them into the table below.

## Free-tier budget

The whole system runs at **$0**.

| Hop | Service | Free allowance |
|---|---|---|
| STT | Groq `whisper-large-v3-turbo` | 2,000 req/day, 28,800 audio-sec/day |
| LLM | Groq `llama-3.3-70b-versatile` | ~30 RPM, 1,000 RPD, 12K TPM |
| TTS | Groq Orpheus → text fallback | terms-gated, one-time accept |
| Orchestration | n8n community edition, self-hosted | unlimited |
| Interface | Telegram Bot API | unlimited, no inbound port needed |
| Tracing | Langfuse Cloud | 50K observations/mo |

The app needs no GPU of its own: every model call goes to Groq over HTTP, so
any host that can run a Python web process is enough.

## Getting started

```bash
git clone https://github.com/YatishSikka/voice-ops-agent.git
cd voice-ops-agent
pip install -r requirements.txt

cp .env.example .env      # then fill it in
python scripts/preflight.py     # gate checks
python scripts/check_telegram.py  # verifies the bot, finds your chat id
python bot.py                   # start talking to it
```

Then send the bot a voice message. `/tools` lists what it can currently do —
including any workflow it skipped and why.

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

Hosting the app elsewhere means giving n8n a public address — a Cloudflare
Tunnel is enough, and only `N8N_BASE_URL` changes.

`preflight.py` is a gate, not a formality. Three things can invalidate the whole
design, so it verifies all three and prints a pass/fail table:

| Gate | What it proves | If it fails |
|---|---|---|
| Hugging Face | Token valid, email verified, account >30 days | Kept as a gate because the account checks still gate ZeroGPU; hosting itself has since moved off Spaces (see Limitations) |
| n8n | `GET /api/v1/workflows` answers, and tag filtering works | The registry has nothing to read — switch to self-hosted `npx n8n` immediately |
| Groq | STT, LLM **and** TTS each accept a real call | A listed model is not a callable one; a gated TTS drops to text |

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
- `PUBLIC_BASE_URL` — where n8n POSTs when a background workflow finishes. Note
  it defaults to `http://127.0.0.1:7860`, not `localhost`: n8n is Node, Node
  resolves `localhost` to IPv6 `::1` first, and a server bound to IPv4 only
  refuses that connection. That mistake costs you a silent `ECONNREFUSED`
  visible nowhere except n8n's execution history.

### Declaring a tool

A workflow describes itself with fields the n8n editor already has:

| Where | What |
|---|---|
| Workflow name | the tool name, slugified (`Get Calendar Events` → `get_calendar_events`) |
| Workflow description | the prose the model reads to decide if the tool applies |
| Webhook node → Notes | a JSON Schema for the arguments |

Notes accepts a bare JSON Schema, or an envelope carrying more:

```json
{
  "description": "Generate the monthly usage report.",
  "async": true,
  "parameters": {
    "type": "object",
    "properties": {"month": {"type": "string"}},
    "required": ["month"]
  }
}
```

`"destructive": true` marks an action that cannot be taken back. The agent
calls the tool, gets a confirmation prompt instead of an effect, reads the
action back to the user, and only proceeds once they agree — the confirmation
token is bound to the exact arguments it was issued for, so a yes for one
action cannot execute a different one, and it is single use.

`"async": true` marks work too slow to keep a conversation waiting. The agent
fires it with a task id and a callback URL, tells the user it will report back,
and ends the turn; when n8n calls back, the result arrives over Telegram. The
workflow must also be **tagged** `agent-tool` and **active** — an inactive
workflow's production webhook 404s, so the registry skips it rather than offer
the model a tool that cannot work.

## Roadmap

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Preflight gate checks | **Done** — all gates green |
| 1 | Voice loop: speech in, speech out | **Done** |
| 2 | **Tool registry** — n8n workflows as runtime-discovered tools | **Works locally** — end to end, voice to n8n and back |
| 3 | Async handoff — long jobs report back later | **Done** — verified against live Telegram |
| 4 | Confirmation gate; first real integration | **Done** — Google Calendar is live via OAuth |
| 5 | Eval harness, CI, measured latency table | **Done** (Langfuse skipped) |

| 6 | Telegram as the interface, replacing the web UI | **Done** |

The Gradio app in `app.py` still runs (`python app.py`) and is useful for
debugging a turn without a phone, but Telegram is the product.

## Eval scorecard

17 scenarios, run serially against live Groq and n8n
(`python evals/run_evals.py --audio`). Three are two-turn: a confirmation gate
is not a gate unless the first turn stops *and* the second one goes through.

| Metric | Result |
|---|---|
| Task success | **17/17** |
| Tool selection | **17/17** |
| Restraint — no tool when none is needed | **6/6** |
| Word error rate | **0%** over 4 spoken fixtures |

| Hop | p50 | p95 |
|---|---|---|
| LLM | 584 ms | 763 ms |
| Tool (n8n webhook) | 30 ms | 46 ms |
| **Agent turn** | **584 ms** | **807 ms** |

Add measured STT (~840 ms) and TTS (~700 ms) for the voice-to-voice figure of
roughly 2.1 s.

Restraint is scored deliberately: a third of the suite expects **no** tool call,
because an agent that reaches for a tool on "thanks, that's all" is as broken as
one that misses a real request.

A third defect came from wiring the real calendar in, and is the kind only live
data exposes: n8n's Google Calendar node moved `timeMin`/`timeMax` out of
`options` and into top-level node parameters at typeVersion 1.3. n8n resolves
collection parameters against the node's declared schema, so the old keys were
**silently dropped** — no error, no warning, just an unfiltered query returning
the entire calendar. Every day looked identical, and with an empty calendar it
had looked perfect. Fixed by reading the node's source rather than guessing at
the parameter shape.

Two defects came out of the first eval run, both invisible to unit tests:

- **`tool_use_failed`** — Llama on Groq intermittently emits a tool call in
  Llama's text format (`<function=name{...}</function>`) rather than structured
  JSON, and Groq rejects it with a 400. A 400 is otherwise never retried, so the
  turn silently lost its tool call. Resampling that specific error fixed 2 of
  the 14 scenarios and took tool selection from 86% to 100%.
- **An over-broad system prompt** — "if you lack a tool, say so" was meant for
  actions, but the model applied it to knowledge and refused to name the capital
  of France. The prompt now separates *doing* from *answering*.

The suite paces itself at 2.5 s per turn to stay inside Groq's ~30 RPM, so a
full run costs about a minute.

## Honest limitations

- **Latency is ~2.1s voice-to-voice, against an original target of 1.5s.** The
  1.5s figure was written before the agent existed. A measured tool-calling
  turn breaks down as **STT ~840 ms · LLM ~480 ms · tool ~65 ms · TTS ~700 ms
  = ~2.1 s**, so the target moved to 2.5s to match what the architecture
  actually costs. Browser capture plus HTTP round-trip STT is most of the gap
  from a ~900ms telephony agent, and STT is the hop to attack first. These are
  single runs on one fixture, not a distribution; the p50/p95 table stays empty
  until the eval harness produces one.
- **Free-tier ceilings are real.** Groq's ~30 RPM is comfortable for one
  speaker and not for a demo audience. Evals run serially for this reason.
- **The host spins down when idle**, so the first request after a quiet period
  pays roughly a minute of cold start.
- **No persistent disk on the free host.** Durable state lives in n8n; session
  state is in-memory by design.

- **Two free tiers disappeared underneath this plan, mid-build.** n8n Cloud's
  trial turned out to disable the public API, and Hugging Face made Gradio
  Spaces PRO-only — only Static Spaces remain free, and a Gradio app is not
  static. Both were verified assumptions when the plan was written. The lesson
  worth keeping: check that a free tier still exists *and* still includes the
  specific feature you need, then design so the host is swappable. Here it
  cost two `.env` values and a `render.yaml`, because nothing above the
  transport layer knew where anything ran.
- **`ClaudeProvider` is written but unverified against the live API.** It is the
  swap path, not the shipping path.
- **Only one of the three workflows is a real integration.** `get_calendar_events`
  reads a real Google Calendar over OAuth, filtered to the requested day; `send_email` and `generate_monthly_report`
  are still stubs that prove the confirmation gate and the async handoff without
  sending anything. The machinery around them is real either way — swapping the
  calendar fixture for the live API changed no application code, only the
  workflow.

- **n8n runs self-hosted, and not by preference.** n8n Cloud has no free tier
  any more, and its 14-day trial [disables the public
  API](https://docs.n8n.io/connect/n8n-api/) — the `Settings → n8n API` screen
  is hidden and `/api/v1/*` is switched off server-side. Since runtime workflow
  discovery *is* this project, Cloud is unusable below the paid Starter tier.
  The community edition has the same API for free, so the agent talks to a
  self-hosted instance. Preflight's gate 2 exists to catch precisely this.

## License

MIT
