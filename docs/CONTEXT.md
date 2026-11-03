# CONTEXT — WhatsApp Assistant Agent
_Last updated: 2026-11-03_

## Current goal
Get the system running on this machine, with replies coming from the Claude API
instead of OpenAI.

## Status at a glance
| Area | State | Notes |
|---|---|---|
| Python env | ✅ | 3.12.16 in `venv/`; `torch==2.4.1` has no wheels past 3.12 |
| Bridge deps | ✅ | installed with `PUPPETEER_SKIP_DOWNLOAD=true`, uses system Chrome |
| Claude port | ✅ | `llm.py`, `assistant.py`, `tts.py` |
| Backend boots | ✅ | `/health` 200, `/dashboard` 200, auth gates 401 |
| Test suite | ✅ | 218 pytest + 54 `node --test` pass, 0 fail |
| Dashboard UI | ✅ | rebuilt on design tokens; live (deployment `98ce06d`) |
| Live model call | ✅ | real key in `.env`; replies, Arianna's tools and the blank-input guard all verified live |
| Eval set run | ✅ | 27/32, **0 unsafe sends**; the 5 misses are the empty notes, see below |
| Deploy (Coolify) | ✅ | live at https://arie.benardkimani.co.ke/dashboard, Let's Encrypt |
| WhatsApp linked | ❌ | **the session is gone** — the bridge is printing pairing codes (2026-11-03) |
| Who gets answered | ✅ | `ALLOWED_CONTACTS=anyone` live on the server; unchosen senders reply automatically |
| Replies going out | ⬜ | **the notes are still empty — this is the one blocker** |

## Done
- Ported replies and Arianna from OpenAI to the Claude Messages API:
  - `app/llm.py` — `anthropic.Anthropic()`; the three-field answer is now enforced
    by structured outputs (`output_config.format` + `ANSWER_SCHEMA`) instead of
    being asked for in the prompt. `_parse_answer` kept as the fail-closed gate.
  - `app/assistant.py` — Arianna's 4 tools converted to Anthropic's flat
    `input_schema`; reads `tool_use` blocks, whose `input` is already a dict.
  - `app/tts.py` — the optional OpenAI voice no longer borrows the chat client;
    Claude has no speech endpoint, so it keeps its own lazily-imported client.
- Removed `temperature` (current Claude models return 400 on it).
- Raised `max_tokens` 400 → 4096: thinking cannot be disabled on this model
  family and draws from the same budget, so 400 truncated the answer, which
  fails closed into a permanent hold.
- Added `_normalize_history()`: the API requires the first turn to be `user`, and
  neither condition held here — the rolling window is just the last N rows, and a
  held message stores the incoming text with no reply after it. Tests in
  `tests/test_llm_history.py`.
- Added a blank-message guard: an empty turn is rejected by the API, so a voice
  note Whisper heard nothing in now fails closed without spending a call.
- Cached the long system prefix (notes + schedule + style examples) with the
  per-message parts after the breakpoint, since every message re-sends it.
- Fixed `tests/conftest.py`: it stubbed `app.tts` with one name, which broke
  collection for the whole suite (`app.dashboard` imports `synthesize_bytes`,
  `test_tts.py` imports three more). `app/tts.py` needs no stub — `edge_tts` and
  `openai` are both imported inside the functions that use them.
- Created the missing setup files: `.env.example` (the README referenced a file
  that wasn't in the repo), `contacts.json`, `data/kb/about_me.md`,
  `data/style_examples.md`, `requirements-dev.txt`, `scripts/dev.sh`.
- Gitignored `data/style_examples.md` — personal voice data, removed from the
  repo deliberately in 875f8b1.

## Next (ordered)
All of these are the owner's to do, on the live dashboard, and nothing useful
happens until the first one is done.

1. **Fill the Notes tab** — About you, Schedule, Things to redirect. Everything
   in it is given to the model as true, so it is left empty rather than
   guessed; empty means the assistant can only deflect. This is why the eval
   set's five misses are holds.
2. Replace the placeholder lines in **style examples** with real messages, so
   the voice is the owner's rather than an impression of it.
3. ~~Pick people on the **Contacts** tab.~~ Superseded: the assistant now
   answers anyone (see *Done — answering anyone*). Per-person modes still
   override it, so this is where to put someone back on "ask me first".
4. Watch the held queue for a while, then turn **auto-reply** on when the
   drafts read right. Deliberately still paused.
5. Re-run `python -m evals.run_eval` once the notes are filled — the five
   needless holds should turn into passes. Use `--runs 3` to match the
   pre-port baseline in git history.
6. Multi-tenancy: measure RAM for two stacks before anything else
   (ADR-0001, proposed and not started).

## Done — deployment (2026-10-30)
- Implemented the location feature `tests/test_phone_location.py` had always
  described and never had: `/api/phone-location` records a reading from the
  device, `_phone_fix()` serves it while fresh and deletes it once stale, and
  `/api/send-location` now takes **only** a contact. Coordinates in its request
  body are ignored, so a session cookie no longer lets a caller send an
  arbitrary place as the owner. `app/dashboard.html` posts the fix first, then
  sends. All 7 of those tests pass; they were the repo's only red ones.
- argon2id for the dashboard login (`DASHBOARD_PASSWORD_HASH`, generated by
  `scripts/hash_password.py`). Plaintext `DASHBOARD_PASSWORD` still works for a
  loopback-only dashboard and is ignored when a hash is present. Sessions are
  signed with whichever is configured, so changing the password signs everyone
  out. 5 new tests.
- `Dockerfile`, `whatsapp-bridge/Dockerfile`, `docker-compose.yaml`,
  `.dockerignore` for Coolify.
- `CONTACTS_FILE` and `BRIDGE_API_HOST` are now environment-configurable, both
  defaulting to today's behaviour. They are what let the two containers share
  one `contacts.json` and reach each other across the compose network.

## Done — localisation (2026-10-30)
Rewrote the persona from Egyptian Arabic to Kenya: Kenyan English, Sheng and
Kiswahili, replying in whichever the sender used and mirroring a code-switched
mix rather than tidying it into one language.
- `app/llm.py` — persona, language rules, flag examples, image hint. The
  Arabic feminine-forms section is gone (neither English nor Kiswahili has
  grammatical gender). The punctuation rule is unchanged — the stripper
  already kept "?" and only removed `. , ، ! ; :`, and that casual
  no-punctuation style carries over to Sheng and Swahili texting.
- `app/lang.py` — now English/Kiswahili, plus `looks_swahili()`. This is the
  real structural change: Arabic and English were told apart by **script**,
  but Swahili and English share the Latin alphabet, so detection had to move
  to vocabulary. Short ambiguous words ("na", "ya", "si") are deliberately
  excluded — they occur in English text and would misroute it.
- `app/tts.py` — `sw-KE-ZuriNeural` / `en-KE-AsiliaNeural` (verified against
  `edge_tts.list_voices()`, not guessed), selected by vocabulary.
- `app/assistant.py` — Arianna's canned replies are Swahili/English, and her
  prompt no longer assumes the owner's gender.
- `app/stt.py` — `STT_LANGUAGE`, default `en`.
- Timezone is `Africa/Nairobi` via `TZ`, and `CAIRO`/`CAIRO_TZ` were renamed
  `LOCAL_TZ`. **`app/llm.py` was telling the model "(Cairo time)" while
  stamping the local clock** — the label is now derived from the zone so it
  cannot drift again.
- `evals/cases.jsonl` — all 32 cases rewritten, same categories and
  expectations. The two name-only cases now use a `{USER_NAME}` placeholder
  that `run_eval.py` substitutes, instead of a hardcoded name.
- `data/style_examples.md` — Kenyan placeholders, still to be replaced with
  real messages.

## Done — deployment, live (2026-10-30)
Running on Contabo via Coolify: project `Arianna`, resource `arie`, domain
`arie.benardkimani.co.ke`. Two bugs only a real deployment could surface:

- **The bridge crash-looped after every redeploy.** Chromium stamps
  `SingletonLock`/`Cookie`/`Socket` in its profile with the hostname that
  created them. The `wa-session` volume survives a redeploy on purpose, so the
  QR need not be rescanned — but the new container has a new hostname, so
  Chromium saw a lock owned by "another computer" and refused to start. The
  bridge now clears those three names at startup; only one process ever uses
  that profile, so a lock found there is always stale. Session credentials
  (`Cookies`, `Local State`) are untouched. Commit 1051fc5.
- **Linking by QR is impractical on a server.** The only place a QR renders is
  Coolify's log viewer, it rotates about every 20 seconds, and WhatsApp reports
  a stale scan as "Couldn't link device — try again later", which reads like a
  fault rather than a timeout. `PAIRING_NUMBER` now makes the bridge call
  `requestPairingCode()` instead: eight characters, about three minutes, typed
  in rather than photographed. Falls back to the QR if the request fails.
  Commit 1d76420.

Note for the next person: the bare domain used to return 404, because there is
no route at `/`. It now redirects to `/dashboard` (302) — see *Known issues*
for why it is not a 301.

## Done — linking (2026-10-31)
The account is linked and the server survives redeploys without a QR. Getting
there needed three separate fixes, each hiding the next:

1. **Stale Chromium locks** (1051fc5) — see above. Without this the bridge
   crash-looped on every redeploy, and a transplanted profile *always* carries
   locks, so nothing else would have worked either.
2. **A WhatsApp Web version pin ~30 million builds old** (78ad325). The library
   drives WhatsApp Web's internal modules, so it pins the build it was written
   against. WhatsApp keeps serving an old build to linked sessions but refuses
   to link a NEW device on one, reporting it as "Couldn't link device — try
   again later". `WA_WEB_VERSION` overrides it; **this will go stale again**,
   and that message is the symptom to look for.
3. **A passkey gate WhatsApp added around 2026-06-30.** The link handshake now
   wants a WebAuthn ceremony, which a headless container cannot perform. It
   broke Baileys, whatsmeow and whatsapp-rust the same day and whatsapp-web.js
   has no fix for it.

**How the gate was passed:** linked on a laptop with `HEADLESS=false`
(5bec04a), where a real browser and Touch ID can complete the ceremony, then
the resulting profile was moved into the server's `wa-session` volume. The gate
applies only at link time, so the server never faces it. The transplant was
macOS/ARM → Linux/x86 and worked; the profile is ~345 MB because the WhatsApp
IndexedDB carries message history, not just keys.

**If the link is ever lost**, that is the procedure: link on a laptop, copy
`.wwebjs_auth/session` into the volume, `chown -R 10001:10001`, restart. A
pre-transplant copy is kept on the server as `session.replaced`.

Also fixed here: the bridge was logging senders' phone numbers and message text
(6447f31). The Python side already hashed chat ids via `anon()`; the Node side
never did. It mattered little on a laptop and matters on a host that keeps
logs, especially for senders who are not on the allowlist.

## Done — contact sync (2026-10-31)
`contacts.json` was hand-written and, on the server, absent — so the dashboard
had nobody it was allowed to message. **Sync from WhatsApp** on the Contacts
tab now copies the phone's own address book into it:

    dashboard button -> POST /api/contacts/sync -> bridge POST /sync-contacts
    -> client.getContacts() -> mergeContacts() -> contacts.json (shared volume)

Only the bridge can read the address book, because it is the process holding
the WhatsApp session. The rules live in `whatsapp-bridge/contacts_sync.js`,
away from the client and the filesystem, with 12 tests:

- **Adds only.** An entry already in the file is never renamed, repointed or
  removed. `contacts.json` is the list the dashboard may send *to*, so a sync
  can only ever widen it, and only when asked — never on a timer.
- **Saved contacts only** (`isMyContact`, not blocked, not a group, not self).
  Without that filter every stranger who ever messaged the account becomes
  someone the dashboard may message back.
- **One person, one entry.** A number already in the file under a name the
  owner chose ("Mom") is not added again as "Jane Doe", matched on the last 9
  digits so `254712345678` and `0712345678` count as the same line.
- A contact with no phone number keeps its `@lid` privacy id, which is a valid
  recipient; a nameless one is skipped rather than named after its own number;
  two people under one name are disambiguated by last-4-digits.
- `CONTACTS_SYNC_LIMIT` (500) bounds one sync. Written temp-then-rename, so an
  interrupted sync cannot leave a truncated recipient list.
- Counts in the logs, never a name or a number — the address book belongs in
  the file and on the owner's screen, not in the host's logs.

Verified by driving the real route against a stub client: a manual "Mom" entry
survived a sync that had her under her WhatsApp name, a non-contact was
skipped, an `@lid` contact came through, a second sync produced an identical
file, and an untokened request got 401.

## Done — your WhatsApp on the dashboard (2026-10-31)
A **WhatsApp** tab that lists the owner's conversations, opens any of them and
replies to one person. Above 1024px it sits beside Arianna rather than
replacing her, which is what was asked for: the chats and the assistant
reading them, side by side.

    pane  ──GET /api/wa/chats ─┐
          ──GET /api/wa/thread─┼─> bridge /chats /thread -> client.getChats()
          ──POST /api/wa/reply─┘                            chat.fetchMessages()

**Live, not polled.** The bridge's `message_create` handler posts the chat id
(and nothing else) to `POST /wa-event`; the backend fans that out over SSE on
`GET /api/wa/stream`; the page refetches the affected conversation. Measured
end to end against a stub: a message appended outside the browser appeared in
the open thread with no interaction. A 60 s poll remains as a fallback for a
proxy that buffers the stream.

Deliberate properties, all of them load-bearing:
- **`message_create`, not `message`** — so what the owner sends from their own
  phone shows up here too.
- **The notice carries no text.** Only a chat id crosses that wire, so the
  server never holds anyone's words in memory on the way past, and the agent's
  allowlisted path (`/webhook`) is untouched by it.
- **Nothing read here is stored.** `message_log` stays what went through the
  agent. The pane is a window, not a second copy of WhatsApp.
- **No read receipts.** Opening a thread on the dashboard must not tell the
  sender it was read, so `sendSeen` is never called.
- **Replying widens one existing boundary, knowingly.** `/send-reply` used to
  take its chat id only from the database. It now also takes one from the
  browser, so the bridge checks it against WhatsApp itself: `getChatById` only
  resolves a conversation that already exists, and groups are refused. With a
  stolen session someone could answer people who have already written to the
  owner — they could not start a conversation with a stranger.

Bug found and fixed while verifying in a real browser: the `.pair` wrapper kept
its flex share of the height even with both panes hidden, so Held, Messages,
Activity and Contacts were squashed into the bottom half. `show()` now hides
the wrapper itself.

## Done — making auto-reply worth turning on (2026-10-31)
The gates all worked; what was missing was anything to reply *with*.

- **The notes finally reachable.** `data/` is gitignored and the Dockerfile
  never copied it, so on the server `about_me.md` and `style_examples.md` did
  not exist and the prompt had no facts, no schedule, no redirect rules and no
  voice. A **Notes** tab now reads and writes both through `/api/notes`, saved
  temp-then-rename onto the volume. The prompt was assembled at *import* time,
  so this also needed `llm.system_prompt()`: rebuilt only when either file's
  mtime or size changes, which keeps the text stable between messages so the
  prompt cache still hits. `assistant.py` calls `_load_style_examples()` now
  rather than capturing it, so Arianna's own voice follows the same edit.
- **History that predates us.** Memory only ever held what the agent itself had
  seen, so a friend of ten years and a stranger looked identical on the first
  message. `memory.seed_from_whatsapp()` pulls the real thread through the
  bridge once, when nothing is stored. Best effort: a bridge that is down costs
  context, never the reply. `HISTORY_TURNS` (12) is now configurable, capped
  at 50 so one contact cannot blow up the prompt.
- **It says why.** `why` was added to `ANSWER_SCHEMA` and stored on the held
  row: one line, to the owner, about what it was unsure of. Far more use than
  the category — "needs_owner" does not tell you *what* needed you. Optional in
  the parser and clipped to 200 chars, so an old or over-long answer still
  parses. Adaptive thinking was already on and cannot be disabled on this model
  family; what was missing was any of it reaching the owner.
- **Quiet hours** (`QUIET_HOURS=22:00-07:00`): the draft is still written and
  still queued, only sending waits. A misspelt window means "not quiet" — the
  alternative is a typo silently holding every reply.
- **"typing…"** while the model works, gated on `GET /agent-state` so it is
  never shown when replies are being held. Showing it with auto-reply off would
  promise an answer that never comes. Off by default.
- **Drafts come to you.** `live.publish()` notices gained a kind, and a held
  reply now publishes `draft`, so the card appears under the conversation in
  the WhatsApp pane with Send / edit / Dismiss — verified in a browser.

`app/bridge.py` was extracted along the way: the dashboard and the agent both
needed the bridge, from async and sync code respectively.

## Done — choosing who gets answered (2026-10-31)
Auto-reply could never have worked: `ALLOWED_CONTACTS` held one number — the
owner's own — so every real sender was dropped at the bridge as
`not_in_allowlist`, and the owner's own messages were dropped as `fromMe`.
Three locks were closed at once (allowlist, `auto_reply` paused, empty notes)
and only the last was visible from the dashboard.

Two layers now, with different jobs:
- **`ALLOWED_CONTACTS`** is the outer bound — who may reach the agent at all.
  `contacts` means the address book, re-read per message so a sync applies at
  once. Fails closed both ways: an empty or unreadable book means nobody.
- **Per-contact mode** is the choice, made on the Contacts tab. A new
  `default_contact_mode` setting (default **ignore**) decides what someone you
  have not chosen gets. "ignore" drops the message before the agent runs, so
  nobody's conversation is transcribed, stored or sent to a model until they
  are picked — syncing an address book costs nothing and reveals nothing. The tab shows "3 of 214 set to reply
  automatically" and a picker for the default.

Bug caught by the new tests: `set_mode()` deleted the row whenever the mode
was literally `"auto"`, on the old assumption that auto was the default. With
a default that is no longer `auto`, choosing "reply automatically" for one
person was silently discarded — the one choice that matters. It compares against the
current default instead.

## Done — answering anyone (2026-11-03)
Asked for directly: it should respond to anyone, not only to a saved contact
who has been picked. Three switches stood in the way and only two were moved.

- **`ALLOWED_CONTACTS=anyone`** — a new keyword in both copies of the rules
  (`whatsapp-bridge/allowlist.js`, `app.main.is_allowed`). An empty list
  already meant everyone, but by accident: it is what you get by forgetting,
  and `docker-compose.yaml` holds the variable as `${VAR:?}` precisely so
  forgetting cannot deploy. `anyone` says the same thing on purpose, survives
  compose, and reads as a decision in the audit of the file.
  - It makes the address book irrelevant, so it is not read — a broken
    `contacts.json` can no longer fail closed over a list that has stopped
    deciding anything.
  - An `@lid` privacy id is still resolved to a number, because that is what
    lets the backend recognise a saved contact and honour the mode set for
    them. But resolution no longer decides the outcome, so a failure now costs
    that name rather than the reply.
  - Groups are untouched: `ALLOW_GROUPS=false` still refuses them as
    `groups_off`, which was the answer to the question when it was asked.
- **Default contact mode `auto`** — set on the Contacts tab ("Anyone not
  chosen"). This is the switch that actually answers a stranger: someone never
  saved has no mode of their own, so they get the default, and the default was
  `ignore`. The allowlist alone would have let everybody reach an assistant
  that replies to nobody.
- **`auto_reply` is still off**, deliberately, and is the owner's to turn on
  (*Next*, item 4). Until then every one of these replies is written and held
  rather than sent. On the server it is a different database and came up *on*
  — see *Deployment traps*, trap 2.

A drop by contact mode now writes one `ignored` log line (no text, hashed chat
id). It was the only gate that could silently answer nobody, and the
combination that does it looks live from every screen.

8 new tests across the two suites, including the end-to-end one the change is
really about: a sender in no list and under no name reaches the agent, gets a
reply, and is filed under their number.

## The dashboard rebuilt (2026-11-03)
Live on the server as deployment `98ce06d`. Verified there rather than only
locally: `--primary` computes to the dark-mode green, and `#app` measures
1240px on all seven tabs, with the paired Chat and WhatsApp panes at 598px
each — so every tab's content edges land in the same place.

The symptom that prompted this was read as a bug in the new CSS and was not:
the fix had been pushed and never deployed, so the server was still serving
`#app[data-split="on"]` and the old purple tokens. Worth remembering — reading
the deployed stylesheet settled it in one command, after guessing had not.

- **Design tokens.** Five accent colours wired one per tab, an
  indigo-violet-pink gradient on the logo, the h1 text, every primary button
  and every outgoing bubble, three radial glow blobs behind the page, a conic
  gradient under a 40px blur spinning in the orb, and a hue generated per
  contact. Spacing ran 7/9/10/11/13/14/15/18px freehand. Replaced with a 4-pt
  scale, a type scale, three radii, two elevations, three durations, one
  easing, and colour *roles* with one green accent. Contrast measured: muted
  text 5.9:1, primary on white 5.4:1, danger 6.5:1, warning 5.1:1.
- **One width for every tab.** `#app[data-split="on"]` widened the shell only
  while Chat and WhatsApp were paired, so the page resized as you changed tabs
  — 780px on the other five, 1240px on those two. Now one width throughout;
  only prose and form fields are capped.
- **Phones.** Verified at 360 and 390px in real viewports, no horizontal
  overflow. The tab strip was clipping its last tab, because the selected tab
  keeps its label and equal flex shares then overflowed; it scrolls now and
  scrolls the selection back into view.
- **Media.** `wa_read.js` only ever produced a label. The bridge gained
  `/media` and `/avatar`; photos, stickers, video and voice notes render in
  the thread, documents are offered as downloads, and view-once waits behind a
  button because opening one tells the sender and cannot be undone. Profile
  photos are fetched by the backend, so the browser never talks to WhatsApp
  and the CDN url never reaches the page.
- **Choosing answered chats from the conversation**, which is also what puts
  someone on `contacts.json` — the pane lists people who were never saved on
  the phone, and a per-person setting needs a name to hang on.

Two bugs found while building it, each with a test that fails without the fix:

- `set_mode()` stored a row only when the choice differed from the current
  default, which cannot tell "I chose this" from "I never decided". With the
  default on `auto` that meant choosing someone stored nothing at all, and a
  later change to the default silently moved people chosen deliberately.
- `_same_number()` compared by suffix. A national number keeps the trunk zero
  in some countries and replaces the country code with it in others, so
  `254712345678` and `0712345678` — one Kenyan line — did not match, and
  `name_for()` returning nothing dropped that person onto the default mode.
  All three copies of the comparison now use the last 9 digits.

## Deployed, and the link that was not there (2026-11-03)
`anyone` is live on the server (deployment `c41e36b`, 11m37s, finished
14:41 UTC). Set there, and verified by reloading the page rather than by
trusting the control:

- Coolify `ALLOWED_CONTACTS`: was `contacts`, now `anyone`. `ALLOW_GROUPS`
  confirmed `false`.
- Dashboard "Anyone not chosen": was **Leave alone**, now **Reply
  automatically**. This lives in `data/assistant.db` on the volume, so it did
  not travel with the code — it is the per-deployment setting trap again.
- `auto_reply` on the server is **On**, as trap 2 predicted. Combined with the
  two above, a stranger's reply now goes out without a step from the owner.

**The bridge has no WhatsApp session.** It boots, clears its stale Chromium
locks as designed, and then prints a fresh `PAIRING CODE` every few minutes —
so the account is unlinked and *nothing reaches the assistant at all*. The
backend is healthy and answers 502 on `/api/wa/*` and `/api/contacts/sync`
because of it. Whether the link died before this redeploy or during it is not
visible from these logs: the volume itself survived (the "cleared 3 stale
Chromium lock file(s) from a previous container" line proves the profile is
still there), so what is gone is the credential inside it, not the directory.

Consequences, until it is re-linked:
- No message can arrive, so "answers anyone" is configured but inert.
- `contacts.json` on the server is **empty** — the sync has never succeeded
  there — so no one can be pinned to a mode of their own yet. Pinning Kawira
  specifically needs the sync, which needs the bridge.

Re-linking is the documented procedure under *Done — linking*. Note
`WA_WEB_VERSION` is pinned at `2.3000.1047561935-alpha`; if linking reports
"Couldn't link device", that pin has gone stale again and is the first thing
to bump.

## Done — knowing which replies were not yours (2026-10-31)
"How do I know on my phone that the text is AI generated?" has a hard
constraint behind it: WhatsApp renders the same bytes at both ends, so a
marker in the message is either visible to the owner *and* the recipient, or
invisible to both — an invisible watermark cannot be read off a phone screen
either, so it does not answer the question.

**Copy to me** (`self_log`, off by default) instead sends a note to the
owner's own chat whenever the assistant replies by itself:

    🤖 Arianna replied to Mom:

    niko njiani

The recipient's message carries nothing extra. The bridge's `/send-self`
takes the recipient from `client.info.wid`, never from the request, so a
caller holding the token cannot redirect it. Best effort: a note that fails
is logged and never turns a sent reply into a failed one. Held replies are
not copied — the owner approved those and has already seen them.

The other record, which already existed, is the Messages tab: every row is
labelled *auto-reply*, *sent after your approval* or *sent from this
dashboard*.

## Decisions
- ADR-0001 Multi-tenancy: a container per tenant, not a tenant column.
  **Proposed, not started** — the blocking question is RAM per tenant
  (Chromium cannot be pooled), which has to be measured first.
- Model defaults to `claude-opus-5-5`, effort `medium`, both overridable via
  `ANTHROPIC_MODEL` / `ANTHROPIC_EFFORT` so the eval set can be used to tune
  them without code changes.
- The bridge uses the system Chrome via `PUPPETEER_EXECUTABLE_PATH` rather than
  Puppeteer's own download, which would not extract completely here. A full
  Chrome is also the better choice: it has the media codecs WhatsApp Web uses.
- `ALLOWED_CONTACTS` is a `${VAR:?}` field in `docker-compose.yaml`, so Coolify
  refuses to deploy until it has a value. An empty allowlist means every sender
  is processed, and that should not be reachable by forgetting to type
  something. `anyone` is the value that means it deliberately, and is what is
  set now. `BRIDGE_API_TOKEN` goes the other way: it is
  `${SERVICE_PASSWORD_64_BRIDGE}`, which Coolify generates once and hands to
  both services, so no human picks or copies it.
- The backend image installs the CPU torch wheel from PyTorch's own index. The
  default PyPI wheel for Linux is the CUDA build, which pulls ~2 GB of
  `nvidia-*` packages a VPS cannot use.
- The bridge's send API still binds loopback by default. Only compose sets
  `BRIDGE_API_HOST=0.0.0.0`, where the port is unpublished and the shared token
  still applies.
- `auto_reply` is set to `0` in `data/assistant.db` for the first run, so every
  reply is generated and queued for approval instead of being sent. It is a
  dashboard toggle, not code — turn it on once the drafts look right.
- `data/kb/about_me.md` ships with empty sections on purpose. Everything in it
  is given to the model as true, so an empty section makes the assistant say it
  isn't sure, while a placeholder would make it state something false.

## Deployment traps worth knowing (both cost hours)

**1. Compose interpolation shreds anything containing `$`.** The dashboard
rejected every correct password. The hash stored in Coolify was right; the
hash the container received was not — 97 characters arrived as 66, because
Compose read `$argon2id`, `$v`, `$m` and both base64 fields inside the value
as variable references and replaced them with nothing. A hash in that state
refuses every password with no error anywhere. Coolify's per-variable
"Literal" setting did not prevent it. `DASHBOARD_PASSWORD_HASH_B64` now
carries the hash base64-encoded, which has no `$` to mangle.
*The lesson that generalises:* when a value looks right in the platform UI but
behaves wrong, check it **inside the running container** —
`docker exec <c> sh -c 'echo ${#VAR}'`. Three rounds were lost verifying the
stored value, which was never the broken layer.

**2. The server's database is not the laptop's.** `auto_reply` was switched
off locally and assumed to be off everywhere; the server came up at its
default, which is **on**. Nothing escaped only because the allowlist held one
number whose messages are blocked as `fromMe` before the allowlist is read.
Any setting stored in `data/assistant.db` — the kill switch, direct send,
per-contact modes — has to be set again per deployment.

## Known issues / deferred
- Whisper handles Kenyan English better than Kiswahili and heavy Sheng worse
  than either, so deep-slang voice notes are the least reliable input.
- `looks_swahili()` is a word list. A Swahili message containing none of its
  listed words is treated as English; it errs that way on purpose, because
  misreading English costs intelligibility while missing Swahili only costs
  an accent.
- The eval set's five misses are all needless holds in the `schedule` and
  `knowledge` categories — "where are you now", "what do you do for work" —
  i.e. questions whose answers belong in the empty `about_me.md`. The replies
  were deflections ("مش متأكدة لسه هشوف واقولك"), not inventions, so this is
  the intended fail-closed behaviour and not a regression from the port. The
  pre-port baseline (2026-10-04, 3 runs/case) scored 31/32 with **one unsafe
  send**; this run had none. Re-run after filling the notes.
- `pytest 9.2.0` is newer than anything this repo was tested on.
- The bare domain used to 404, which looked like a broken deploy. `GET /` now
  redirects to `/dashboard` (302, not 301: a permanent redirect is cached by
  the browser for good and this should stay cheap to change).
- A large address book makes a long Contacts tab and a long contact filter in
  Messages: both are flat lists with no search. `CONTACTS_SYNC_LIMIT` keeps it
  bounded; a search box is the fix if it starts to grate.
- The live stream is in-process (`app/live.py`), so it covers one uvicorn
  worker. The deployment runs exactly one; a second would need a shared bus
  and dashboards would see only the events their own worker received.
- The WhatsApp pane reads through puppeteer, so a chat list or thread costs
  real work in the bridge's browser. Hence the 700 ms coalesce, the one-at-a-
  time guard, and the badge-only refresh while the pane is closed.
- Contact sync never *removes*. Someone deleted from the phone stays a valid
  recipient until the entry is deleted from `contacts.json` by hand. That is
  deliberate — a sync that could delete could also delete a name typed in by
  hand — but it means the file drifts.

## How to run right now
Live at **https://arie.benardkimani.co.ke** — the bare domain redirects to
`/dashboard`. Deployed by Coolify: project `Arianna`, resource `arie`, on
Contabo. Redeploy from the Coolify UI; the session volume persists. Auto-deploy
is off, so a push to `main` does not reach the server until you press it.


```bash
./scripts/dev.sh            # backend, waits for /health, then the bridge
./scripts/dev.sh backend    # backend only — dashboard, Arianna, stored messages
```
Dashboard: http://localhost:8000/dashboard, password is `DASHBOARD_PASSWORD`.
`dev.sh` refuses to start on a placeholder key or a missing secret, and warns
when `ALLOWED_CONTACTS` is empty or the schedule is blank.

Tests: `pip install -r requirements-dev.txt && pytest -q`

## Environment notes
- macOS (Darwin 27), Apple Silicon. Python 3.12.16 (brew), Node 24.16.0.
- Chrome 156 at `/Applications/Google Chrome.app`; ffmpeg present.
- `anthropic==2.3.0`. It needs `httpx2`, which coexists with the pinned
  `httpx==0.27.2` that `app/dashboard.py` uses — different package names.
