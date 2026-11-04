# ADR-0002: Rust, and what multi-tenancy actually costs
- Status: proposed
- Date: 2026-11-04
- Deciders: owner

## Context

The question was put plainly: since the replies now come from the Claude API,
and since Rust has a reputation for being light on a server, and since this is
going to be multi-tenant — should it be written in Rust?

The reputation is earned. Rust really does run in a fraction of the memory that
a Python or Node process needs, and this document measures exactly how much of
a fraction on this machine. But a rewrite only pays where the cost it removes
is actually spent, and on this system it is not spent on the language. It is
spent on a web browser and on two machine-learning models, and neither of those
cares what language starts it.

### What was already known

Two numbers from the server, both recorded in `docker-compose.yaml` and the
reason both containers are now capped there:

- the **bridge** container at **3,709 MiB and 200–280% sustained CPU** — that
  is Chromium, driven by puppeteer, rendering real WhatsApp Web;
- the **backend** container at **3,840 MiB**, of which roughly 1.9 GiB is
  accounted for by resident model weights and their import cost.

The host is 6 cores and 11.7 GB (Coolify's server page, 2026-11-04).

### What was measured for this ADR

Peak resident memory, taken with `getrusage(RUSAGE_SELF).ru_maxrss` in Python
and Rust and `process.memoryUsage().rss` in Node, on the development laptop
(macOS, arm64) with the repo's own `venv` (Python 3.12.16) and Node v24.16.0.
The figures are cumulative peaks, measured after each import in turn.

**This is a laptop, not the VPS, and arm64 rather than x86_64.** The absolute
numbers would shift on the server. What travels is the ratio between them,
which is what the question actually turns on.

The Python backend, import by import:

| After importing | Peak RSS | Added |
|---|---|---|
| bare interpreter | 11.7 MiB | — |
| fastapi, uvicorn, pydantic | 60.1 MiB | 48.4 |
| the anthropic SDK and httpx | 111.1 MiB | 51.0 |
| this project's own modules (`db`, `contacts`, `memory`, `lang`, `quiet_hours`) | 111.9 MiB | 0.8 |
| langgraph and langchain-core | 111.9 MiB | 0.0 |
| torch and transformers — the import alone, no weights | 237.5 MiB | 125.6 |
| faster_whisper — the import alone, no weights | 257.9 MiB | 20.4 |

So **everything this project owns, plus the web framework and the Claude SDK,
is 112 MiB.** The project's own 3,174 lines of Python are 0.8 MiB of it. The
next 146 MiB is two machine-learning libraries being imported, before a single
weight is read off disk.

For the comparison, a Rust program of the same shape was written and run on the
same laptop: axum 0.7.9 on tokio 1.41.1 for the HTTP side, rusqlite 0.32.1
(bundled SQLite) opened in WAL mode with 2,000 rows and the same
`(chat_id, id)` index this project creates, and reqwest 0.12.9 over rustls as
the stand-in for the Claude client.

| After | Peak RSS |
|---|---|
| tokio runtime started | 2.4 MiB |
| SQLite open, WAL, 2,000 rows inserted | 4.1 MiB |
| rustls HTTPS client built | 4.5 MiB |
| axum serving, after 200 requests handled | 6.0 MiB |

The release binary is 6.4 MB. **It is a floor, not a prediction** — it has none
of this project's logic in it, no dashboard, no agent graph, no prompt
assembly. A real port would be larger. But the gap between 6 MiB and 112 MiB is
not going to be closed by business logic: this project's own modules cost 0.8
MiB in Python.

And the Node bridge, with no browser launched:

| After | RSS |
|---|---|
| bare node | 43.4 MiB |
| axios and dotenv | 66.1 MiB |
| an HTTP server listening | 67.5 MiB |
| `whatsapp-web.js` required — **no browser started** | 97.8 MiB |

### Where the cost actually is

Those measurements divide the 7,549 MiB the two containers were seen using into
parts that answer to a language change and parts that do not.

| Layer | Size | Would a Rust rewrite reduce it? |
|---|---|---|
| Chromium and WhatsApp Web's own JavaScript heap | ~3,611 MiB (the bridge's 3,709 less the 98 MiB the Node process accounts for) | **No.** Chromium is a separate process tree. The thing that starts it is 98 MiB beside it |
| Model weights while resident | ~1.6 GiB (faster-whisper small at int8, 610 MiB; BLIP in fp32, ~1 GB) | **No.** These are arrays of floats |
| torch and transformers, import cost | 126 MiB measured | **No.** torch is C++ behind a thin Python binding; a Rust process calling the same library pays the same |
| faster-whisper, import cost | 53 MiB measured on its own | **No.** ctranslate2, also C++ |
| Everything else — the interpreter, the framework, the SDK, all of this project's code, on both sides | 112 MiB (backend) + 98 MiB (bridge) = 210 MiB | **Yes** |

A Rust rewrite of both halves, keeping Chromium and keeping both models, would
take that last row from roughly 210 MiB to roughly 12 MiB. **That is about 198
MiB of 7,549 — a little over 2.5% of the measured cost, for a rewrite of the
entire system.** The honest answer to the owner's premise is that Rust is as
light as its reputation says, and that it is light about the one part of this
system that was never heavy.

The remainder, between the roughly 1.8 GiB the backend can account for and the
3,840 MiB the host's monitor reported, is most likely glibc's per-thread malloc
arenas: ctranslate2 and torch size their thread pools from the host's core
count, and glibc hands each allocating thread a 64 MiB arena it never returns.
That is already addressed in `docker-compose.yaml` with `OMP_NUM_THREADS: 2`
and `MALLOC_ARENA_MAX: 2`. It is worth noting that this particular waste is
**not** something Rust fixes either: Rust's default allocator on Linux is the
same glibc malloc, and the threads in question belong to the C++ inference
libraries, not to the Python interpreter.

### The work in flight that proves the point

While this ADR was being written, `app/stt.py` and `app/vision.py` were being
changed — uncommitted at the time of writing, alongside a new
`tests/test_lazy_models.py` — to do two things: import `faster_whisper` and
`transformers` inside the functions that need them rather than at module scope,
and evict a loaded model after `MODEL_IDLE_MINUTES` (default 15) of no use,
returning the freed pages to the operating system rather than to Python's
allocator.

That is the largest memory win available to this system and it is being taken
in Python, by changing *when* things are resident rather than *what language*
loads them. A backend that never receives a photo now never imports
transformers at all — 126 MiB of the measurement above, gone, for a
`TYPE_CHECKING` guard and a moved import.

### What multi-tenancy requires, and how little of it is about language

ADR-0001 already decided the shape — a container per tenant, with a control
plane — and listed what assumes one owner. This is that list again with the
files and lines, because the point here is different: almost none of it gets
easier or harder in another language.

| What is single-tenant | Where | What it would have to become |
|---|---|---|
| The database path | `app/db.py:9` — `DB_PATH` is a module constant with no environment variable, unlike `CONTACTS_FILE` and `APP_LOG_FILE` | Per tenant: a volume mounted at that path. Shared process: configurable, plus a tenant column |
| The schema | `app/db.py:43–60` — six tables (`held`, `audit`, `messages`, `contact_modes`, `settings`, `message_log`), no tenant column anywhere | A tenant column on all six, leading both indexes (`db.py:54`, `db.py:60`), and on every one of the ~25 queries in that file |
| The settings table | `app/db.py:56`, read by `auto_reply_enabled()` (`:223`), `self_log_enabled()` (`:227`), `direct_send_enabled()` (`:233`), `default_mode()` (`:179`) | Four installation-wide switches that are per-person questions |
| Per-chat memory | `app/db.py:130–143`, keyed on `chat_id` alone | **This is the sharpest hazard in a shared process.** It is not a missing `WHERE` clause but a key collision: two tenants who both know the same person share one `chat_id`, so their private histories merge silently and correctly-written queries return the wrong one |
| `contacts.json` | `app/contacts.py:11` (env-configurable), written by both the backend (`contacts.py:132–137`) and the bridge (`writeContacts()` in `bridge_api.js`, a file under active edit, so cited by name) onto a shared volume | One file per tenant. The re-read-before-write at `contacts.py:120–123` is already load-bearing for two processes and would stay so |
| The notes and the style examples | `app/llm.py:34–35` — `KB_PATH` and `STYLE_EXAMPLES_PATH`, module constants, no environment variable | Per-tenant paths, or rows |
| The assembled system prompt | `app/llm.py:286–293` — `_prompt_cache` is a single module-level slot keyed on the notes files' mtime signature (`llm.py:233–245`) | Keyed by tenant. As written, a shared process would serve one person's notes inside another person's prompt |
| Who the assistant *is* | `app/llm.py:56` `USER_NAME = os.getenv("USER_NAME", "Shahd")`, then `llm.py:81` `BASE_SYSTEM_PROMPT = f"""You are {USER_NAME}…"""` — an f-string evaluated at import | The owner's name is baked into a module constant at process start. Also `ASSISTANT_NAME`/`ASSISTANT_NICKNAMES` (`app/assistant.py:27,30`) and `LOCAL_TZ` (`app/llm.py:14`, `app/controls.py:18`, `app/quiet_hours.py:14`) |
| Who gets answered | `app/main.py:37` `ALLOW_GROUPS` at module scope; `_load_allowed_contacts()` (`:41`) reads per call, which is already right; `whatsapp-bridge/index.js:44–45` reads both at module scope | Per tenant, from their own storage |
| The dashboard login | `app/dashboard.py:82,97,104` — one password or one argon2id hash from the environment; `dashboard.py:123–128` derives the session signing key from that single credential | Real accounts. Note the coupling: because the cookie is signed with the one credential, changing a password signs out everybody — correct today, wrong the moment there are two tenants |
| The live notice bus | `app/live.py:18` — one module-level subscriber set; `publish()` (`:32–49`) fans every chat id out to every open dashboard. Its own docstring (`:8–9`) says it covers one process | Per-tenant topics. In a shared process every tenant's dashboard would be told every other tenant's chat ids |
| The model key | `app/llm.py:62–64` — one module-level `anthropic.Anthropic()` reading `ANTHROPIC_API_KEY` from the environment | Per-tenant attribution and a per-tenant cap. There is no seam for either today |
| The send-rate ceilings | `whatsapp-bridge/send_queue.js:18–26` (3 s minimum gap plus up to 9 s jitter; 8/minute, 60/hour, 300/day), instantiated once at `index.js:192`, with `sentAt` and `lastSend` as instance state (`send_queue.js:38–39`) | **One queue per linked account.** These ceilings exist to protect *an account*, not a server. Shared across tenants they would either throttle ten people to one person's budget, or — if widened to compensate — let ten accounts' traffic hide inside one allowance, which is the opposite of what the file is for. The constructor already takes overrides (`send_queue.js:34–35`) and nothing passes any, so this is the cheapest item on the list |
| The WhatsApp session | `whatsapp-bridge/index.js:155` — `dataPath: path.join(__dirname, ".wwebjs_auth")`, hardcoded, no environment variable, with the `wa-session` volume mounted onto exactly that path | One profile, one linked phone, and it cannot be pooled. This is the constraint ADR-0003 is about |
| The backend-to-bridge link | `bridge_api.js:125–126` one token and one port; `app/bridge.py:15,19` one URL | One pairing per tenant |

Read that list as a bill of work. Every line of it is schema design, process
boundaries, path configuration and authentication. Not one line of it is
cheaper to do in Rust, and several — the dashboard, the agent graph, the prompt
assembly — would have to be rewritten from scratch first in order to then be
changed.

## Decision

Proposed: **stay on Python and Node. Do not rewrite this system in Rust.**

The reasoning is the measurement: a full rewrite of both halves addresses about
2.5% of the resident cost, while the two levers that address roughly 70% of it
— not loading models that are not in use, and not running a browser — are both
available without changing language, and one of them is being implemented right
now.

Two narrower statements, because "don't rewrite it" is not the same as "Rust
has no place here".

**The one component where Rust would genuinely pay is the bridge — but only if
the bridge stops being a browser, and today the Rust library for that is the
least mature of the three.** Once the connection to WhatsApp is a protocol
client rather than a rendered page, per-session memory stops being an overhead
and becomes the product's unit cost: it is the number multiplied by the tenant
count, and the thing a control plane schedules against. That is exactly the
kind of long-lived, many-sessions, no-ML process where Rust's floor of 6 MiB
is worth having. But the mature implementations of that protocol are Baileys in
Node (11.5k stars) and whatsmeow in Go (7.8k); the Rust one, `whatsapp-rust`,
is at 0.7.0 with six of its ten releases breaking (see ADR-0003 for the
sourcing). Choosing Rust today would mean choosing the least-proven
implementation of the single component that must not break, in exchange for
memory that is not the binding constraint. **Revisit if `whatsapp-rust` reaches
1.0 and holds an API for a few releases.**

**If the control plane ADR-0001 proposes gets written, Rust is a reasonable
choice for it, and the honest reason is not memory.** It is new code, so there
is nothing to rewrite; it has no machine-learning dependency, so it does not
drag a C++ library behind a binding; and its job — hold connections, start and
stop containers, route requests — is one Rust is good at and one where a single
binary with no runtime to install is genuinely easier to deploy. The memory
saving over the same service in Python would be about 106 MiB, which is roughly
3% of one tenant. That is a preference, not an argument, and it should be stated
as a preference.

## Consequences

**Positive**

- No rewrite. The 3,174 lines of Python and 2,606 lines of JavaScript keep
  working, along with the 242 pytest and 93 `node --test` cases that hold them
  in place.
- The memory work proceeds where the memory is: lazy imports and idle eviction
  for the models, and the transport decision in ADR-0003 for Chromium.
- The multi-tenancy bill in the table above is paid once, in the language it is
  already written in, instead of twice.
- Nothing is foreclosed. The two places Rust would fit — a protocol bridge, a
  control plane — are both *new* components, reachable later without undoing
  anything decided here.

**Negative / trade-offs**

- The backend keeps an interpreter and a 112 MiB floor. Multiplied by tenants
  in the container-per-tenant design, that is about 112 MiB per tenant that a
  Rust backend would not spend. On an 11.7 GB box that is real but small
  beside the alternatives, and it should be re-checked against whatever the
  per-tenant floor turns out to be after ADR-0003.
- Python's packaging stays a deployment concern: `torch==2.4.1` has no wheels
  past Python 3.12 (see `docs/CONTEXT.md`), which already pins the interpreter.
  Removing BLIP would remove that pin as a side effect.
- Declining to use Rust now means the decision gets made again later, when
  there is more code to move.

**Follow-ups, in order**

1. **Finish and commit the lazy-import and idle-eviction work** already in
   flight in `app/stt.py` and `app/vision.py`. Measured: it takes 126 MiB of
   torch-and-transformers import cost off a backend that has not been sent a
   photo, and returns ~1 GB of BLIP weights 15 minutes after one.
2. **Delete BLIP.** `app/vision.py` loads a ~1 GB fp32 captioner to produce one
   English sentence, which `app/llm.py:230` then injects as text with the
   instruction to "treat that as what you are looking at". The Claude Messages
   API takes images natively as `image` content blocks, base64 or URL, on the
   model this project already calls (`ANTHROPIC_MODEL` defaults to
   `claude-opus-5-5` at `app/llm.py:16`). Passing the photo itself would be both
   cheaper in memory and better in quality than captioning it with a 2022
   model first. Two measured bonuses: `torch` and `transformers` are imported
   by nothing but `app/vision.py` and `app/download_blip.py`, and
   `faster-whisper` does not depend on torch at all — it requires `av`,
   `ctranslate2`, `huggingface-hub`, `onnxruntime` and `tokenizers` — so
   removing BLIP removes torch from the project entirely, along with the
   `model-cache` volume that exists to hold its weights.
3. **Then re-measure the backend**, and record the number. After (1) and (2) an
   idle backend should be close to the measured 112 MiB plus faster-whisper's
   own 53 MiB, with whisper's 610 MiB arriving only on a voice note and leaving
   again afterwards. That is the figure the per-tenant arithmetic should use,
   not 3,840 MiB.
4. **Decide the transport** (ADR-0003). Until that is settled, per-tenant cost
   is dominated by a number nothing in this ADR can move.
5. **Make the send-rate queue per account** before there is a second account —
   `send_queue.js` already accepts overrides and nothing passes any. It is the
   one item on the multi-tenancy list that is both cheap and unsafe to defer,
   because the ceilings protect the accounts, not the server.
6. **Meter model spend per tenant.** Anthropic's own structure is the natural
   seam here rather than a counter in this codebase: separate workspaces with
   their own API keys give per-tenant attribution and rate limits directly, and
   the Admin API reports usage and cost per workspace. That replaces the single
   `ANTHROPIC_API_KEY` at `app/llm.py:62–64`.
7. **Leave the Rust question open for the control plane**, when there is one to
   write, and for the bridge, if and when the Rust protocol library matures.

## Alternatives considered

| Option | Why not |
|---|---|
| Rewrite the backend in Rust | Measured at 112 MiB → 6 MiB, which is 1.4% of what the two containers were seen using. It would also mean reimplementing the dashboard, the agent graph, the prompt assembly, the approvals flow and 242 tests, and giving up the Anthropic Python SDK, for a saving smaller than the models it would still be loading |
| Rewrite the bridge in Rust, keeping the browser | The Node process is 97.8 MiB with `whatsapp-web.js` loaded and no browser running, beside a container measured at 3,709 MiB. A Rust wrapper would save perhaps 92 MiB and would still have to drive Chromium over the DevTools protocol, which is what puppeteer already does. There is also no Rust equivalent of `whatsapp-web.js` — the Rust libraries speak the protocol instead, which is a different decision (ADR-0003) |
| Rewrite in Go instead | Go's floor sits between Node's and Rust's, and `whatsmeow` is the most proven protocol library of the three, which is a genuine argument for Go that Rust does not have. It is still a rewrite of working code to save a fraction of what the models and the browser cost, and ADR-0003 can use `whatsmeow` behind a Node or Python process without the rest of the system changing language |
| Keep Python, move the models to a Rust sidecar (whisper.cpp, Candle) | The inference is already native code — faster-whisper is ctranslate2 in C++, running int8. A Rust sidecar would be a different wrapper around comparable native code. The measured win is in *when* a model is resident, and that was achievable in Python |
| Port only the hot path to Rust | There is no hot path. The one CPU-bound operation is transcription, already in C++; the 200–280% sustained CPU is Chromium rendering WhatsApp Web, in a process neither language owns |
| One shared process with a tenant column, in any language | Rejected in ADR-0001 and the file-level list above makes it worse rather than better: `_prompt_cache` is one slot, `live.py` has one subscriber set, `BASE_SYSTEM_PROMPT` is an f-string fixed at import, and per-chat memory is keyed on `chat_id` with no tenant, so two tenants who know the same person collide on the key rather than merely failing to filter |

## Honest summary

Rust is as light as the owner heard. It measured at 6 MiB against Python's 112
MiB for the same shape of service, on the same laptop, on this project's own
dependency set — a factor of nearly nineteen, and the sort of number that makes
a rewrite feel obvious.

It is the wrong nineteen. The system's cost is a browser (~3.6 GiB) and two
model files (~1.6 GiB), and a rewrite touches neither; it addresses about 2.5%
of what was measured. Meanwhile both of the big levers are available without
changing language, and the model one is half-built already.

Two things are genuinely uncertain. The per-tenant floor after ADR-0003 is
unknown, because nothing here measured a Chromium-free WhatsApp session —
whether that is 150 MiB or 400 MiB per tenant decides whether the 112 MiB
Python floor matters at all. And the figures in this document are from an arm64
laptop, not the x86_64 VPS; the ratios should hold, the absolutes will move.
Both are worth measuring before anyone writes a line of Rust, and neither is a
reason to write it.
