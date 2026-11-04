# ADR-0003: How this reaches WhatsApp when there is more than one person
- Status: proposed
- Date: 2026-11-04
- Deciders: owner

## Context

ADR-0001 decided the shape of multi-tenancy — a container per tenant, with a
control plane — and said plainly which fact decided it:

> The hard constraint is WhatsApp itself. whatsapp-web.js drives a real browser
> session per linked account: roughly 300-400 MB of Chromium and a persistent
> profile **per tenant**, which cannot be pooled or shared.

The measurement since then was worse than the estimate. The bridge container
was seen at **3,709 MiB and 200–280% sustained CPU**, which is why both
containers are now capped in `docker-compose.yaml`. Some of that was V8's old
space growing without bound — `index.js` now passes `--max-old-space-size=512`,
and the compose comments expect the real working set to land near 1.4 GiB — but
even 1.4 GiB per tenant means **three or four tenants on an 11.7 GB box**. That
is not a business; it is a hobby with a hosting bill.

ADR-0002 establishes that no language change touches this. The browser is a
separate process tree; the Node process that starts it measured at 97.8 MiB
beside the container's 3,709. So the question that actually decides whether
multi-tenancy is affordable is the one this ADR is about: **is there a way to
reach WhatsApp that is not a browser?**

There are two candidate answers, and a constraint on both that has to come
first.

### The constraint: the account must not be banned

This outranks everything else in this document. It has been stated repeatedly
and it is correct to state it repeatedly: a banned WhatsApp account is not a
degraded service, it is the loss of the thing the product is attached to, and
for a personal number it is the loss of years of conversation.

It is worth being exact about where the project already stands, because the
honest starting point is not "no risk".

`whatsapp-web.js`, which this system runs today, says so itself
([github.com/pedroslopez/whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js),
22.9k stars, Apache-2.0, read 2026-11-04):

> WhatsApp does not allow bots or unofficial clients on their platform, so this
> shouldn't be considered totally safe.

and

> Also it is not guaranteed you will not be blocked by using this method.

So **the ban risk has already been accepted.** Every option below that keeps the
owner's own number is a choice between unofficial clients, not a choice about
whether to use one. What the repo does about it is in
`whatsapp-bridge/send_queue.js:1–12`, and it is the right instinct:

> WhatsApp bans accounts for automated messaging, and the tells are not
> subtle: replies that land in the same few hundred milliseconds every time,
> dozens of them at once to people who have never written before, at four in
> the morning. A human's reply times are spread across minutes and hours.

That is the part of ban risk this project can control, and it travels unchanged
across every option below. The part it cannot control — whether Meta
fingerprints the client itself — is discussed with each.

### Route one: the official WhatsApp Business Cloud API

This is the route ADR-0001's alternatives table called "the *right* answer for a
real product". That is no longer true, and this ADR supersedes that row.

**Meta's terms now appear to prohibit precisely this product.** The
WhatsApp Business Platform terms
([facebook.com/legal/Meta-Terms-for-WhatsApp-Business-Platform](https://www.facebook.com/legal/Meta-Terms-for-WhatsApp-Business-Platform),
last modified **23 September 2026**; the older
`whatsapp.com/legal/business-solution-terms` URL now redirects there) carry a
section 4.7, "AI Providers". It defines the category as "Providers and
developers of artificial intelligence or machine learning technologies",
expressly including "general-purpose artificial intelligence assistants", and
says such providers "are strictly prohibited from accessing or using the
WhatsApp Business Platform" where those technologies are the primary
functionality offered, as Meta determines. Certain countries are exempt and the
section survives termination.

Reporting puts the dates at 15 October 2025 for new accounts and **15 January
2026 for existing ones**, with a 90-day grace period in which providers were
required to stop answering user queries and tell their users the bot would stop
working
([respond.io](https://respond.io/blog/whatsapp-general-purpose-chatbots-ban)).
Meta excluded Brazilian numbers after Brazil's competition regulator ordered the
policy suspended
([TechCrunch, 15 January 2026](https://techcrunch.com/2026/01/15/after-italy-whatsapp-excludes-brazil-from-rival-chatbot-ban)).
A separate AI-provider pricing change was dated 16 February 2026.

Whether this product falls inside 4.7 is **genuinely unresolved and needs a
lawyer, not an engineer.** The argument that it does: the thing being sold is an
AI assistant; the AI is the primary functionality, not a supporting feature of
some other business. The argument that it does not: section 4.7 also says "you
may retain an AI Provider as your Solution Provider" — a Solution Provider being,
per section 1.1, "a third party authorized to use these APIs on such business'
behalf" — so a structure in which each tenant is the Client and this product is
their Solution Provider is contemplated by the terms. Against that: section 1.2
defines "you" as "you and the company that you are authorized to represent",
and a person wanting an assistant on their personal number has no company to
represent. The terms are written for businesses messaging their customers. This
product is a person messaging their friends.

Set 4.7 aside and the Cloud API still cannot do what this system is. Three
things:

- **The number cannot be the owner's own, not without destroying it.** A number
  active on the consumer WhatsApp app cannot be registered on the Cloud API; the
  account has to be deleted first, and its history goes with it
  ([respond.io](https://respond.io/help/whatsapp/phone-number-migration-to-whatsapp-cloud-api)).
  The exception is Meta's "coexistence", which lets a WhatsApp Business app
  number connect to the API without deletion — but Meta lists group chats,
  broadcast lists and calls among the features that stop working while a number
  is on both. **This system handles groups** (`ALLOW_GROUPS`,
  `app/main.py:37`; the `g.us` branch of `CONVERSATION_ID`, `main.py:53` and
  `main.py:59`; the
  whole of `tests/test_held_groups.py`), so coexistence would remove a feature
  that already exists.
- **It is a business sender, with a display name and template rules**, not a
  person. Freeform replies are confined to a window after the customer writes;
  anything outside it is a pre-approved template. "Reply as me, in my voice, in
  Sheng" is not a template category.
- **It bills per message.** Billing moved from per-conversation to per-message,
  and since 1 October 2026 service messages — the category a reply falls into —
  are billable after 1,000 per business number per month
  ([telnyx](https://telnyx.com/resources/whatsapp-business-api-cost),
  [eesel](https://www.eesel.ai/blog/whatsapp-business-api-pricing)). Quoted US
  rates are roughly $0.025 for marketing and $0.0034 for utility,
  authentication and service, with wide variation by country. **The sources
  disagree on specific figures and all of them are resellers; Meta's own rate
  card is the only authority and it was not read for this ADR.** The order of
  magnitude is what matters here: a few tenths of a cent per message, on top of
  the Claude call, after the first thousand.

**Ban risk on this route: effectively none.** It is Meta's own platform. That is
its one decisive advantage, and it is a large one — which is why the 4.7
question deserves a real answer rather than this ADR's guess.

### Route two: a protocol client instead of a browser

WhatsApp's multi-device protocol has been reverse-engineered, and there are
libraries that speak it directly over a WebSocket, with the Signal protocol for
encryption and no browser anywhere. This is the route that changes the
per-tenant arithmetic, because a session stops being a rendered web page and
becomes some encryption state and a socket.

Three implementations matter, and their maturity is the whole finding (all read
2026-11-04):

| Library | Language | Stars | State |
|---|---|---|---|
| [Baileys](https://github.com/WhiskeySockets/Baileys) | TypeScript / Node | 11.5k, 3.5k forks, MIT | "a WebSockets-based TypeScript library for interacting with the WhatsApp Web API". At 7.0.0, which "introduced" multiple breaking changes; one roundup notes the major version sat on release candidates for months |
| [whatsmeow](https://github.com/tulir/whatsmeow) | Go | 7.8k, 1.3k forks, MPL-2.0 | "Go library for the WhatsApp web multidevice API". Messages, media, groups, invite links, receipts, app state, retry receipts. Calls not implemented. The de facto reference implementation — the Rust projects say they verified their wire formats against it |
| [whatsapp-rust](https://github.com/oxidezap/whatsapp-rust) | Rust | 970, 166 forks, MIT | 1,996 commits, 2 open issues. v0.7.0 released 7 August 2026; 46,243 downloads a month; **ten releases, six of them breaking** ([lib.rs](https://lib.rs/crates/whatsapp-rust)). Broader feature list than whatsmeow — it claims calls and screen sharing — but "Windows/macOS are unqualified", ESP32 is experimental, and the application must supply its own storage backend |

Two other Rust results are worth naming so nobody rediscovers them hopefully.
[lordmacu/whatsapp-rs](https://github.com/lordmacu/whatsapp-rs) is 4 stars and
109 commits — a personal project. The `whatsmeow` crate on crates.io (v0.1.4)
is not a port at all: it is Rust bindings that compile the Go library through a
bridge, so choosing it means shipping the Go runtime anyway.

**So the non-browser route exists and is mature — in Node and in Go.** The Rust
option is the least proven implementation of the component that must not break.
That is the finding that answers the Rust half of the owner's question from the
other direction, and it is recorded in ADR-0002's decision.

**Ban risk on this route: unresolved, and this ADR will not pretend
otherwise.** It is a real question with arguments both ways and no measurement.

Against the swap: `whatsapp-web.js` runs WhatsApp's *actual* web client inside a
real browser, so at the protocol level its traffic is a genuine web session by
construction. A reimplementation can diverge — in handshake details, presence,
receipts, app-state sync, client version strings — and any divergence is a
potential tell. These libraries track the protocol, and tracking means lagging.

For the swap: there is already evidence in this repo that Meta gates on client
version — `index.js:111` records that an old build is served to existing
sessions but **refuses to link a new device**, and `index.js:123` pins
`WA_WEB_VERSION` because of it. A browser is not a free pass. And every source
found, including the ones selling the opposite, converges on behaviour rather
than technology as what gets accounts banned: volume, timing, messaging
strangers. One put it as the risk coming "from how you send, not from the
technology". The vendor blogs quoting "permanent ban within weeks" are selling
official-API onboarding and should be read as such; the libraries' own
disclaimers are the honest sources, and `whatsapp-rust`'s is the bluntest:
"Using it may violate Meta's Terms of Service and could result in account
suspension."

What follows from "unresolved" is not paralysis, it is a test design. **The
owner's own number must not be the experiment.** The behavioural controls in
`send_queue.js` are the part that demonstrably matters and they must be ported
before anything else — which is easy, because they are 81 lines with no
dependency on the client at all, deliberately "free of timers and of the client
so the rules can be tested directly".

### The multi-tenant version of the ban question is a different question

One thing deserves separating out, because it changes with tenant count rather
than with technology.

Today the owner runs an unofficial client on their own number and accepts the
risk for themselves. That is a decision a person is entitled to make. **In a
multi-tenant product that risk is transferred to paying customers who did not
read `whatsapp-web.js`'s README, and the product is the proximate cause of any
ban.** A tenant losing their personal WhatsApp — their family, their work, ten
years of messages — because of software they paid for is a category of harm
quite different from the owner taking a chance on their own account.

That does not settle which route to take. It does mean two things are
non-negotiable before anyone other than the owner is onboarded: the risk is
stated in plain words at sign-up, before a QR code is scanned, and the
per-account send ceilings are enforced per tenant rather than per process
(`send_queue.js:18–26`, instantiated once at `index.js:192` — see ADR-0002's
inventory).

### The seam that makes this cheap to try

The bridge is already behind an interface. `whatsapp-bridge/bridge_api.js`
serves a fixed list of ten routes — `/send-text`, `/send-location`,
`/send-reply`, `/sync-contacts`, `/chats`, `/thread`, `/send-self`, `/media`,
`/avatar`, `/link-status` (the `ROUTES` constant in `bridge_api.js`; that file
is under active edit, so it is cited by name rather than by line) —
authenticated by one shared token, and `index.js` calls back to exactly three
backend endpoints:
`/webhook`, `/wa-event` and `/agent-state` (`index.js:34–38`). On the Python
side the whole client is `app/bridge.py`, thirty lines.

**That is the entire contract a replacement transport has to satisfy:
ten routes in, three callbacks out.** Nothing in `app/` knows that a browser
exists. A Baileys-backed bridge can be built, run against the same backend, and
compared on the same dashboard without touching the backend at all — and if it
is worse, deleting it costs nothing. That is the cheapest and most reversible
experiment available, and it is the reason the staged path below starts there.

## Decision

Proposed, in three parts.

**1. Keep `whatsapp-web.js` for the single tenant that exists.** It works, the
session on the laptop is valid, and the current blocker is empty notes rather
than transport (`docs/CONTEXT.md`). Nothing here is urgent enough to risk a
working link. Specifically: do not re-link, migrate or experiment on the
owner's number while it is the only one.

**2. The multi-tenant path is a protocol client, not a browser, and the
prototype is Baileys behind today's bridge interface.** Baileys because it is
the most proven of the three (11.5k stars, MIT) *and* because it is in the
language the bridge is already written in, so the experiment is a swap of one
module behind an existing HTTP contract rather than a new service in a new
language. whatsmeow in Go is the fallback if Baileys' protocol coverage proves
insufficient — it is the reference implementation and both Rust projects verify
themselves against it. Rust is not the choice here, for the maturity reason
above and for the reasons in ADR-0002.

**3. The Cloud API is a different product, not a migration — and before anyone
builds toward it, section 4.7 needs a real legal read.** It cannot be the
owner's own number, cannot reply freely outside a window, cannot be in groups
under coexistence, and bills per message. If it turns out that 4.7 permits a
per-tenant Solution Provider structure, the thing worth building on it is a
*business* assistant — a shop, a clinic, a school answering its own customers —
which is a legitimate and much larger market with no ban risk at all. It is
simply not "reply as me from my own number", and the two should not be
conflated in a roadmap.

## Consequences

**Positive**

- The per-tenant cost stops being dominated by a browser. If a Baileys session
  lands anywhere near the Node process's measured 97.8 MiB floor plus per-session
  encryption state, the box goes from three or four tenants to something that
  could plausibly be a business.
- The 200–280% sustained CPU goes with it. That is Chromium rendering; a
  WebSocket client does not render.
- The `wa-session` volume becomes credentials rather than a Chromium profile —
  smaller, and far easier to back up, move between hosts and reason about.
- Chromium's breakage mode disappears: no more pinned `WA_WEB_VERSION`, no more
  "serves an old build but refuses to link a new device".
- The experiment is reversible. Ten routes and three callbacks, with the
  existing bridge left in place beside it.

**Negative / trade-offs**

- **The ban question is genuinely unresolved and this route does not resolve
  it.** It may be neutral; it may be worse. Nothing measured it, and the only
  honest way to find out is slowly, on numbers nobody minds losing.
- Protocol-level maintenance replaces browser-level maintenance. WhatsApp
  changes its protocol and Baileys has to follow; a version sitting on release
  candidates for months is a real signal about that cost.
- Baileys at 7.0.0 "introduced multiple breaking changes", and `app/` has
  features that depend on things a browser gave for free: avatars, media
  download, thread history (`wa_read.js` is 376 lines with 558 lines of tests).
  Each has to be re-proved against a protocol client. Some may not be there.
- Running two bridges during the comparison doubles the bridge's footprint for
  as long as it takes.
- No memory figure for a Baileys session was found. Its README's claim of
  saving "like half a gig of ram" is anecdotal and self-reported; no controlled
  benchmark turned up. **The central economic premise of this ADR is therefore
  directional, not measured.**

**Follow-ups, in order**

1. **Measure a Baileys session's resident memory before building anything on
   it.** A script that links a spare number, syncs, idles an hour and reports
   RSS. This single number decides whether multi-tenancy is a business, and it
   is a day's work. Everything below is contingent on it.
2. **Get a legal read on section 4.7.** It costs a consultation and it either
   opens the official platform or closes it for good. Either answer is worth
   more than the guesswork in this document, and the second-order question —
   whether a per-tenant Solution Provider structure qualifies — is exactly the
   kind of thing only a lawyer should answer.
3. **Port `send_queue.js` first, unchanged, and make it per account.** It is 81
   lines, deliberately decoupled from the client, and it encodes the only part
   of ban risk that is demonstrably controllable. No transport experiment sends
   a message without it.
4. **Build the Baileys bridge against the existing ten-route contract**, on a
   spare number, and run it beside the real one. Compare on the dashboard:
   does the thread load, do avatars resolve, does media download, do groups
   behave.
5. **Then watch that number for weeks, not days.** Normal traffic, normal
   hours, through the send queue. A library swap that gets an account banned in
   month two is worse than no swap.
6. **Only then**: the control plane, sign-up and the rest of ADR-0001's list —
   with the per-tenant number from step 1 in hand, so the pricing is arithmetic
   rather than hope.
7. **Write the risk into sign-up in plain words**, before any tenant scans a
   QR code, and keep it there.

## Alternatives considered

| Option | Why not |
|---|---|
| Stay on `whatsapp-web.js` and scale by adding servers | Honest and simple, and it is what happens if nothing is done. At roughly 1.4 GiB per tenant post-V8-cap it is three or four tenants per 11.7 GB box, so the hosting bill per tenant is a large fraction of what anyone would pay. Worth keeping as the answer if step 1 above comes back badly |
| Move to the Cloud API as the multi-tenant product | Section 4.7 appears to prohibit an AI assistant as the primary functionality; it cannot use the owner's personal number without deleting that account; coexistence drops group chats, which this system supports; and freeform replies are confined to a window. It is a different product, and part 3 of the decision keeps it on the table as one |
| whatsmeow in Go for the bridge | The strongest technical case of the three — the reference implementation, MPL-2.0, 7.8k stars — and the fallback if Baileys falls short. Not first, because it introduces a third language for a component whose interface is already satisfied by a Node module, and because calls are not implemented |
| `whatsapp-rust` for the bridge | 970 stars against Baileys' 11.5k, and six of ten releases breaking. It would mean the least-proven implementation of the one component whose failure costs an account, in exchange for memory that is not the binding constraint. **Revisit at 1.0 with a stable API** — the feature list is broad and the commit count is serious |
| The `whatsmeow` crate (Rust bindings over the Go library) | It compiles the Go library through a bridge, so the Go runtime ships anyway. It buys Rust's syntax and none of Rust's footprint |
| One browser, many accounts | `whatsapp-web.js` is one account per `Client`, and several clients in one process means several Chromiums in one process — ADR-0001's "worst of both" still holds |
| Start sessions on demand to save memory | Already rejected in ADR-0001 and still right: a session must stay connected to receive messages, and a cold one misses them. Worth noting it becomes *less* necessary if a session is tens of megabytes rather than gigabytes |
| An off-the-shelf unofficial gateway (Green API, WAHA and similar) | Hands a third party the session credentials for every tenant's personal WhatsApp account. The `wa-session` volume comment already says what those are worth: "copying it lets someone else use the account." Not for other people's accounts |

## Honest summary

The browser is the whole problem. It is roughly 3.6 GiB and 200–280% of a core
per tenant, it is not reducible by a language change, and it is what makes
multi-tenancy uneconomic. There is a way off it — a protocol client — and it is
mature in Node and in Go.

Two things are not established, and the decision above is staged specifically so
that neither has to be guessed. Nobody measured what a Baileys session costs,
so the economic case for the swap is directional; one day's work settles it, and
it should be done before anything is built. And nobody can say whether a
protocol client is more or less likely to get an account banned than the browser
client this project already runs in violation of the same terms — so the answer
is weeks of watching a number that is not the owner's.

The one thing that did change since ADR-0001 is the escape hatch. That ADR's
closing line was that if per-tenant cost proved too high, "the answer is not a
better architecture, it is the Business Cloud API and a different product".
Section 4.7 of Meta's terms, last modified 23 September 2026, now appears to
prohibit exactly that product. The official route may still be open as a
business assistant, under a Solution Provider structure, to a different customer
than the one this system was built for — but that reading needs a lawyer, and
until it has one, the unofficial route is not the lazy choice. It is the only
one left.
