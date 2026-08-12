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

> Send it a voice message — *"what's on my calendar on Thursday"* — and it
> transcribes you, resolves the date, queries the real Google Calendar, and
> replies with a spoken voice note. Slow work hands off and reports back later,
> and anything irreversible is read back and confirmed before it happens.

**The tools, all backed by real Google Calendar workflows:**

| Ask it | Tool |
|---|---|
| *"what's on my calendar Thursday"* | `get_calendar_events` |
| *"when am I free tomorrow"* | `find_free_time` |
| *"how does my week look"* | `summarize_my_week` — runs in the background, reports back |
| *"block an hour for deep work at 2"* | `create_calendar_event` — asks before it writes |

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
to Telegram — so nothing has to accept an inbound connection.


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

**TTS separates permanent failure from temporary.** A 403 or 404 will still be
true next turn, so the engine degrades for the life of the process and logs
once; a 429 degrades for that turn only. `synthesize()` never raises, and the
bot sends text when there is no audio — an agent that goes silent is worse than
one that writes.

**Dates are resolved by the model, not parsed by the workflow.** Tools take an
explicit `YYYY-MM-DD`, and the system prompt carries a table of real dates —
today, tomorrow, day after tomorrow, and the coming week by weekday name. The
model has the conversation, so it knows what "what about Friday?" refers to;
listing the dates rather than only today's turns arithmetic into lookup, which
is the difference between reliable and mostly-right.

**Latency is instrumented from the first commit.** A p95 cannot be retrofitted.
Every network hop records a span in `agent/timing.py`, and the eval harness
aggregates them into the table below.

## Free-tier budget

The whole system runs at **$0**.

| Hop | Service | Free allowance |
|---|---|---|
| STT | Groq `whisper-large-v3-turbo` | 2,000 req/day, 28,800 audio-sec/day |
| LLM | Groq `llama-3.3-70b-versatile` | ~30 RPM, 1,000 RPD, 12K TPM, **100K tokens/day** |
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

`preflight.py` is a gate, not a formality. It verifies the services the agent
cannot work without, and prints a pass/fail table:

| Gate | What it proves | If it fails |
|---|---|---|
| n8n | `GET /api/v1/workflows` answers, and tag filtering works | The registry has nothing to read, so the agent has no tools |
| Groq | STT, LLM **and** TTS each accept a real call | A listed model is not a callable one; a gated TTS drops to text |

It exits non-zero on failure and takes `--json` for CI. A `SKIP` is not a pass —
an unverified gate is an unresolved one.

## Deploying

The agent needs a machine that keeps a process alive and nothing else — no
public URL, no tunnel and no inbound port, because Telegram is polled outbound.

```bash
docker compose up -d          # agent + n8n together
```

**[DEPLOY.md](DEPLOY.md)** covers a Docker-free systemd setup for small VMs, a
one-command bootstrap script, and `scripts/import_workflows.py`, which rebuilds
the whole tool set on a fresh n8n instance and rewires it to local credentials.

## Development

```bash
pip install -r requirements-dev.txt
bash scripts/check.sh     # exactly what CI runs, failing the way CI fails
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

## Eval scorecard

21 scenarios, run serially against live Groq, n8n and Google Calendar
(`python evals/run_evals.py`). Each is a real turn: no mocks, no fixtures
standing in for the services.

| Group | What it checks |
|---|---|
| Tool selection | that *"when am I free tomorrow"* reaches `find_free_time`, and a weekday name resolves to a date |
| Restraint | six scenarios expect **no** tool call — an agent that reaches for one on *"thanks, that's all"* is as broken as one that misses a request |
| Output shape | replies stay short and free of markdown, because they are spoken |
| Confirmation | a destructive tool asks first, proceeds on yes, and cancels on no |
| Transcription | word error rate over spoken `.wav` fixtures |

Scenarios that change real state are marked `side_effects: true` and skipped
unless `--with-side-effects` is passed, so a routine run cannot fill a calendar
with test meetings.

Runs are serial and paced. Retry backoff happens inside the timed span, so a
rate-limited run would report the backoff as latency rather than measuring the
agent.

## Honest limitations

- **Latency is ~2.1s voice-to-voice.** A measured tool-calling turn breaks down
  as STT ~840 ms · LLM ~480 ms · tool ~65 ms · TTS ~700 ms. Browser and mobile
  capture plus HTTP round-trip STT is most of the gap from the ~900 ms a
  telephony agent manages; STT is the hop to attack first.

- **Groq's daily token ceiling bites before the per-minute one.** 100K tokens a
  day, which a day of development and two full eval runs will exhaust. The
  agent says so and recovers on its own; the transport distinguishes a daily
  quota from a per-minute limit, because retrying the former cannot succeed.

- **Session state is in-memory.** A restart loses pending background tasks —
  the workflow still completes in n8n, but the notification is lost. n8n holds
  the durable record by design.

- **The bot is allowlisted to one chat.** A Telegram bot username is public and
  these tools read and write a real calendar, so it refuses everyone else. It
  fails closed: unconfigured means nobody, not everybody.

- **`ClaudeProvider` is written but unverified against the live API.** It is the
  swap path, not the shipping path.

- **Whisper is scored on a small fixture set.** 0% WER over four clips of clear,
  synthesised speech in a quiet room. Real accents, noise and crosstalk are not
  represented, and that number would not survive them.
