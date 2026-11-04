// Thin WhatsApp connection layer.
// All "thinking" (RAG, LLM, STT, TTS, vision) happens in the Python backend.
// This file only: receives messages, forwards them, sends back the reply.

const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");
const axios = require("axios");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const { checkAllowed } = require("./allowlist");
const { SendQueue } = require("./send_queue");
const { readContacts } = require("./bridge_api");
const { startBridgeApi } = require("./bridge_api");

// Load the SAME .env file the Python backend uses, from the project root
// (one level up from whatsapp-bridge/).
require("dotenv").config({ path: path.resolve(__dirname, "..", ".env") });

// PHASE 1: leave this true to test the WhatsApp connection alone, with no
// Python backend involved — it just echoes whatever you send back to you.
// PHASE 2: flip to false once main.py is running, to route through the
// real agent instead.
const ECHO_ONLY = false;

// Same shape as anon() in app/log.py: a stable short id for one conversation,
// so a thread can be followed through the logs without recording who it is.
// The host keeps these logs, and most of what passes through here belongs to
// people who are not on the allowlist and never agreed to anything.
function anon(id) {
  return crypto.createHash("sha256").update(String(id || "")).digest("hex").slice(0, 8);
}

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000/webhook";
// Where to say "this conversation moved". Same host as the webhook, so it
// follows BACKEND_URL without a second thing to configure.
const ACTIVITY_URL = process.env.ACTIVITY_URL || BACKEND_URL.replace(/\/webhook$/, "/wa-event");
const STATE_URL = process.env.STATE_URL || BACKEND_URL.replace(/\/webhook$/, "/agent-state");
// "typing…" while the model works, so the wait reads as a person rather than
// as lag. Off by default: it is a promise that an answer is coming, and it is
// only honest when replies are actually going out.
const TYPING_INDICATOR = (process.env.TYPING_INDICATOR || "false").toLowerCase() === "true";
const STATE_TTL = 30000;
const ALLOW_GROUPS = (process.env.ALLOW_GROUPS || "false").toLowerCase() === "true";
const ALLOWED_CONTACTS = (process.env.ALLOWED_CONTACTS || "")
  .split(",")
  .map((c) => c.trim())
  .filter(Boolean);

// Applied BEFORE anything else, in both echo mode and real mode. The rules
// live in allowlist.js so they can be tested without a WhatsApp connection —
// this is the boundary that decides whose messages get transcribed, stored and
// sent to a model, and it had no tests at all. Run them with: npm test
function allowCheck(msg) {
  return checkAllowed(msg.from, {
    allowed: ALLOWED_CONTACTS,
    allowGroups: ALLOW_GROUPS,
    // Read fresh each time, so syncing a new contact takes effect at once.
    addressBook: () => Object.values(readContacts()),
    // Some contacts arrive as an opaque "@lid" privacy id rather than a
    // number; only then is a lookup worth the round trip.
    resolveContact: () => msg.getContact(),
  });
}

// Headless on a server, but WhatsApp now asks for a passkey when linking a new
// companion device, and that ceremony needs a real browser with a real
// authenticator. Set HEADLESS=false on a laptop to link in a visible window
// (Touch ID / Windows Hello), then copy .wwebjs_auth to the server: the gate
// applies only at link time, and an existing session keeps working.
const puppeteerConfig = {
  headless: process.env.HEADLESS !== "false",
  args: [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-first-run",
    "--no-zygote",
    // V8 decides how large its old space may grow from how much memory the
    // machine appears to have, and an uncapped container looks like the whole
    // VPS. So WhatsApp Web's chat store accumulated with nothing ever pressing
    // GC to run, and the host's monitor reported this container at 3709 MiB.
    // Bounding the heap makes V8 collect as it approaches the ceiling instead
    // of asking the kernel for more. 512 MiB is well clear of one account's
    // live set and is the first number to raise if the renderer starts dying
    // with "Target closed" — that is what running out of heap looks like here.
    "--js-flags=--max-old-space-size=512",
    // Chromium's own background traffic — the component updater, variations,
    // safe-browsing lists — is work this container should never do: it talks to
    // web.whatsapp.com and nothing else, and the component updater in
    // particular downloads and unpacks on a schedule. Page network is
    // untouched, so WhatsApp Web does not notice.
    "--disable-background-networking",
    // --disable-gpu above does not end the story: Chromium falls back to
    // SwiftShader, a software rasteriser that loads its own library and
    // allocates its own buffers. WhatsApp Web draws through 2D canvas, which
    // does not need it. The pair belongs together.
    "--disable-software-rasterizer",
  ],
};
if (process.env.PUPPETEER_EXECUTABLE_PATH) {
  // Set this to use an already-installed Chrome, and install with
  // PUPPETEER_SKIP_DOWNLOAD=true. Without it, puppeteer uses the browser it
  // downloads on install — which needs that download to have succeeded.
  puppeteerConfig.executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;
}

// whatsapp-web.js drives WhatsApp Web's own internal modules, so it pins the
// exact web build it was written against. That pin goes stale: WhatsApp keeps
// serving an old build to existing sessions but REFUSES TO LINK A NEW DEVICE
// on one, which surfaces as "Couldn't link device - try again later" and looks
// like a scanning problem rather than a version problem.
//
// So fetch a current build instead of using the bundled pin. wa-version
// archives WhatsApp Web's HTML per build; {version} is substituted by the
// library. Bump WA_WEB_VERSION when linking starts failing again:
//   https://github.com/wppconnect-team/wa-version/tree/main/html
// Bumped 2026-11-03: the previous pin was ~5.3 million builds behind, and
// WhatsApp refuses to link a NEW device on a stale build while existing
// sessions keep working. The symptom was requestPairingCode() failing outright
// ("Could not request a pairing code"), then a QR that nothing would accept.
const WA_WEB_VERSION = process.env.WA_WEB_VERSION || "2.3000.1052866173-alpha";

// whatsapp-web.js deletes the entire profile when WhatsApp reports this device
// logged out: Client.js's frame handler calls authStrategy.logout(), and
// LocalAuth.logout() is fs.rm(userDataDir, { recursive: true, force: true }).
//
// On a laptop that costs a rescan. Here it costs the account. The passkey gate
// means this container cannot link itself, so that directory is the only copy
// of a credential that has to be created on another machine and carried in —
// and one logout deletes it with nothing to recover from. Park it instead; the
// library recreates the directory on the next two calls it makes.
class KeepProfileLocalAuth extends LocalAuth {
  async logout() {
    if (!this.userDataDir) return;
    const parked = `${this.userDataDir}.loggedout-${new Date().toISOString().replace(/[:.]/g, "-")}`;
    try {
      await fs.promises.rename(this.userDataDir, parked);
      console.error(`WhatsApp logged this device out. The profile was kept at ${parked} ` +
                    `rather than deleted. It is no longer a working session — re-linking ` +
                    `needs a machine with a real browser — but nothing was destroyed.`);
    } catch (err) {
      console.error("Could not park the logged-out profile:", err.message);
    }
  }
}

const client = new Client({
  // The path is pinned to this file rather than left to resolve against the
  // working directory. That default is what produced two profiles on the
  // development laptop — one linked, one not — and sent a transplant to the
  // wrong one. It also makes this agree with clearStaleChromiumLocks() by
  // construction instead of by coincidence.
  authStrategy: new KeepProfileLocalAuth({ dataPath: path.join(__dirname, ".wwebjs_auth") }),
  puppeteer: puppeteerConfig,
  webVersion: WA_WEB_VERSION,
  webVersionCache: {
    type: "remote",
    remotePath: "https://raw.githubusercontent.com/wppconnect-team/wa-version/main/html/{version}.html",
  },
});
console.log(`Using WhatsApp Web version ${WA_WEB_VERSION}`);

// Linking by pairing code instead of QR. A QR has to be photographed off a
// screen before it rotates, which is roughly a 20 second window and a losing
// race when the only place it renders is a web log viewer. A pairing code is
// eight characters, lasts about three minutes, and is typed in by hand.
// Set PAIRING_NUMBER to the number of the account being linked, digits only.
const PAIRING_NUMBER = (process.env.PAIRING_NUMBER || "").replace(/\D/g, "");

// Asking WhatsApp for a pairing code is rate limited — the linking API carries
// a MaxPrimaryHelloError for exactly that. A failed request used to clear the
// guard, so the next "qr" event asked again, and those arrive every twenty
// seconds: one transient failure turned into thousands of requests and a cap
// that could never decay while the bridge was running. Back off instead, and
// widen the gap each time, so a refusal costs minutes rather than the account.
const PAIRING_BACKOFF_MS = [5, 15, 30, 60].map((m) => m * 60 * 1000);
let pairingAttempts = 0;
let pairingBlockedUntil = 0;

// Whether this account is linked, and the code to link it with. Shared with
// the API, which reads it on every request. A server has nowhere to show a QR
// — the terminal art in these logs rotates every twenty seconds and cannot be
// scanned off a log viewer — so the dashboard shows it instead.
const link = { linked: false, qr: null, code: null, error: null, stage: "starting" };

// How fast this account is allowed to talk. Every automatic send waits its
// turn here: WhatsApp bans accounts for automated messaging, and replies that
// land in the same few hundred milliseconds, dozens at once, at any hour, are
// what that looks like from the outside.
const sendQueue = new SendQueue();
const pause = (ms) => new Promise((done) => setTimeout(done, ms));

/** Space out one automatic send. Returns false when a ceiling says not now. */
async function paced(textLength, what) {
  const capped = sendQueue.overCap();
  if (capped) {
    console.log(`[send] held back: ${what} would pass the per-${capped} ceiling`);
    return false;
  }
  const wait = sendQueue.delayFor(textLength);
  console.log(`[send] ${what} in ${Math.round(wait / 1000)}s`);
  await pause(wait);
  sendQueue.record();
  return true;
}

function announcePairingCode(code) {
  // Kept for the dashboard as well as the log: a code printed only here is a
  // code somebody has to go and find, and it expires in about three minutes.
  link.code = code;
  const line = "=".repeat(54);
  console.log(`\n${line}\n  PAIRING CODE: ${code}\n  On your phone: WhatsApp > Linked devices >\n  "Link with phone number instead", then type this code.\n${line}\n`);
}

client.on("qr", async (qr) => {
  link.qr = qr;
  link.linked = false;
  link.stage = "waiting for a scan";
  if (!PAIRING_NUMBER) {
    console.log("Scan this QR code with WhatsApp > Linked devices:");
    qrcode.generate(qr, { small: true });
    return;
  }
  // The QR event is the signal that the client is ready to pair; ask once, then
  // let WhatsApp refresh the code on its own interval via the "code" event.
  if (Date.now() < pairingBlockedUntil) return;
  pairingBlockedUntil = Infinity;              // nothing else asks while this one is in flight
  try {
    announcePairingCode(await client.requestPairingCode(PAIRING_NUMBER));
    pairingAttempts = 0;
  } catch (err) {
    const wait = PAIRING_BACKOFF_MS[Math.min(pairingAttempts, PAIRING_BACKOFF_MS.length - 1)];
    pairingAttempts += 1;
    pairingBlockedUntil = Date.now() + wait;
    // The thrown value is minified by WhatsApp's own bundle, so the name is
    // usually more use than the message — MaxPrimaryHelloError means the
    // account has been asked too often and needs to be left alone a while.
    const named = [err && err.name, err && err.message].filter(Boolean).join(": ");
    console.error(`Could not request a pairing code (${named || "unknown"}). ` +
                  `Next attempt in ${Math.round(wait / 60000)} min.`);
    link.stage = `pairing code refused; retrying in ${Math.round(wait / 60000)} min`;
    console.log("Scan the QR on the dashboard instead:");
    qrcode.generate(qr, { small: true });
  }
});

client.on("code", announcePairingCode);

// Everything below exists because a failed scan used to be completely silent:
// the only events reported were qr, ready and disconnected, so WhatsApp
// refusing a link looked exactly like nobody having scanned yet.
client.on("authenticated", () => {
  console.log("Scan accepted — restoring the session.");
  link.stage = "scan accepted, loading";
  link.error = null;
});

client.on("auth_failure", (message) => {
  console.error(`WhatsApp refused the link: ${message}`);
  link.stage = "refused";
  link.error = String(message || "WhatsApp refused the link");
});

client.on("loading_screen", (percent, message) => {
  link.stage = `loading ${percent}%`;
  console.log(`Loading ${percent}% ${message || ""}`.trim());
});

// client.on("ready", () => {
//   console.log(`WhatsApp bridge is ready. ECHO_ONLY = ${ECHO_ONLY}`);
// });
client.on("ready", () => {
  // Which account is linked decides what counts as "fromMe", so log it — hashed
  // with the same anon() as everything else, so it can be confirmed by hashing
  // a candidate number rather than by printing one.
  const me = client.info && client.info.wid ? client.info.wid._serialized : "";
  console.log(`WhatsApp bridge is ready. ECHO_ONLY = ${ECHO_ONLY} linked=${anon(me)}`);
  link.linked = true;
  link.qr = null;              // nothing left to scan
  link.code = null;
  link.stage = "linked";
  link.error = null;
  pairingAttempts = 0;
  pairingBlockedUntil = 0;
});

client.on("disconnected", (reason) => {
  console.log(`WhatsApp disconnected: ${reason}`);
  link.linked = false;
  link.stage = `disconnected: ${reason}`;
  pairingBlockedUntil = 0;
});

// The API used to start on "ready", so an unlinked bridge answered nothing at
// all and the dashboard could not tell "not linked" from "not running". It
// starts with the process now, and refuses the routes that need an account.
startBridgeApi(client, link);

// Every message in or out, so the dashboard's WhatsApp pane follows the real
// thing instead of polling for it. Deliberately carries nothing but the chat
// id: the pane reads the conversation itself over the authenticated API, so
// no message text passes through here, and this is NOT the agent's path —
// that stays below, behind the allowlist. "message_create" rather than
// "message" so what you send from your own phone shows up too.
// Must match wa_read.js: a classic group is "<creator>-<created>@g.us" and a
// multi-device chat can carry a ":12" suffix. Requiring all digits meant those
// conversations never sent an activity ping, so the pane only ever noticed
// them on its 60-second fallback poll.
const CHAT_ID = /^\d[\d:-]*@(c\.us|lid|g\.us)$/;

client.on("message_create", async (msg) => {
  const chatId = String((msg.fromMe ? msg.to : msg.from) || "");
  if (!CHAT_ID.test(chatId)) return;
  try {
    await axios.post(ACTIVITY_URL, { chat_id: chatId }, {
      timeout: 5000,
      headers: { "x-bridge-token": process.env.BRIDGE_API_TOKEN || "" },
    });
  } catch (err) {
    // A dashboard that misses a nudge falls back to its slow refresh, so this
    // must never take the bridge down with it.
    console.error(`activity ping failed for ${anon(chatId)}:`, err.message);
  }
});

// Whether replies are flowing right now (auto-reply on, not quiet hours).
// Cached briefly: it is asked once per incoming message and changes rarely.
let agentState = { sending: false, at: 0 };
async function repliesAreFlowing() {
  if (Date.now() - agentState.at < STATE_TTL) return agentState.sending;
  try {
    const { data } = await axios.get(STATE_URL, {
      timeout: 4000,
      headers: { "x-bridge-token": process.env.BRIDGE_API_TOKEN || "" },
    });
    agentState = { sending: Boolean(data.auto_reply), at: Date.now() };
  } catch {
    agentState = { sending: false, at: Date.now() };   // unsure means stay quiet
  }
  return agentState.sending;
}

// WhatsApp clears "typing…" after about 25 s and a reply can take longer, so
// it is refreshed until the answer is ready. Entirely cosmetic: every failure
// here is swallowed, because none of it is worth losing a reply over.
async function showTyping(msg) {
  if (!TYPING_INDICATOR || !(await repliesAreFlowing())) return null;
  try {
    const chat = await msg.getChat();
    await chat.sendStateTyping();
    const refresh = setInterval(() => { chat.sendStateTyping().catch(() => {}); }, 20000);
    return () => {
      clearInterval(refresh);
      chat.clearState().catch(() => {});
    };
  } catch {
    return null;
  }
}

client.on("message", async (msg) => {
  try {
    if (msg.fromMe) {
      console.log(`[BLOCKED - fromMe] ${anon(msg.from)}`);
      return;
    }
    const { allowed, number, kind, reason } = await allowCheck(msg);
    if (!allowed) {
      console.log(`[BLOCKED] ${anon(msg.from)} kind=${kind} reason=${reason} resolved=${anon(number)}`);
      return;
    }
    console.log(`[ALLOWED] ${anon(msg.from)} type=${msg.type}`);

    if (ECHO_ONLY) {
      await client.sendMessage(msg.from, `echo: ${msg.body}`);
      return;
    }
      const payload = {
        from: msg.from,
        number,
        body: msg.body || "",
        type: "text",
        media_base64: null,
        mimetype: null,
      };  

      console.log(`[DEBUG] ${anon(msg.from)} type=${msg.type} hasMedia=${msg.hasMedia}`);

      const stopTyping = await showTyping(msg);

      if (msg.hasMedia && (msg.type === "ptt" || msg.type === "audio" || msg.type === "image")) {
        try {
          const media = await msg.downloadMedia();
          if (!media || !media.data) throw new Error("downloadMedia returned nothing");
          payload.media_base64 = media.data;
          payload.mimetype = media.mimetype;
          payload.type = msg.type === "image" ? "image" : "voice";
        } catch (err) {
          console.error("downloadMedia failed:", err.stack || err);
          if (!payload.body) return; // voice note or image with no text: nothing to answer
          // image with a caption: fall through and answer the text only
        }
      }

      let data;
      try {
        ({ data } = await axios.post(BACKEND_URL, payload, {
          timeout: 180000,
          headers: { "x-bridge-token": process.env.BRIDGE_API_TOKEN || "" },
        }));
      } finally {
        if (stopTyping) stopTyping();   // never leave them watching "typing…" forever
      }

      if (data.action === "send") {
        const text = data.reply_text || "";
        if (!(await paced(text.length, "auto-reply"))) {
          // Over a ceiling. Saying nothing is the right failure here: the
          // draft is already in the owner's held queue either way.
          console.log(`Held back for ${anon(msg.from)} — check your dashboard.`);
        } else if (data.reply_audio_base64) {
          const voiceMedia = new MessageMedia("audio/ogg", data.reply_audio_base64);
          await client.sendMessage(msg.from, voiceMedia, { sendAudioAsVoice: true });
        } else if (text) {
          // She decides per message whether the reply needs to show what it
          // answers — quoting every one is what makes something read as a
          // bot. ignoreQuoteErrors is spelled out because the library's
          // default is true: left off, a quote that cannot be attached is
          // dropped and the bare text goes out anyway, which in a busy group
          // is a different message from the one she wrote.
          const quoted = data.quote ? { quotedMessageId: msg.id._serialized,
                                        ignoreQuoteErrors: false } : {};
          try {
            await client.sendMessage(msg.from, text, quoted);
          } catch (err) {
            // Only the quote can fail here; the text is still worth sending,
            // and a reply that never arrives is worse than one without its
            // quote. Retried once, plainly, and said out loud.
            if (!data.quote) throw err;
            console.error("Quoting failed, sending without it:", err.message);
            await client.sendMessage(msg.from, text);
          }
        }
      } else if (data.action === "hold") {
        console.log(`Held for approval for ${anon(msg.from)} — check your dashboard.`);
      }
    }   catch (err) {
      console.error("Error handling message:", err.stack || err);
    } 
});

//     const payload = {
//       from: msg.from,
//       body: msg.body || "",
//       type: "text",
//       media_base64: null,
//       mimetype: null,
//     };

//     if (msg.hasMedia && (msg.type === "ptt" || msg.type === "audio" || msg.type === "image")) {
//       const media = await msg.downloadMedia();
//       payload.media_base64 = media.data;
//       payload.mimetype = media.mimetype;
//       payload.type = msg.type === "image" ? "image" : "voice";
//     }

//     const { data } = await axios.post(BACKEND_URL, payload, { timeout: 60000 });

//     if (data.action === "send") {
//       if (data.reply_audio_base64) {
//         const voiceMedia = new MessageMedia("audio/ogg", data.reply_audio_base64);
//         await client.sendMessage(msg.from, voiceMedia, { sendAudioAsVoice: true });
//       } else if (data.reply_text) {
//         await client.sendMessage(msg.from, data.reply_text);
//       }
//     } else if (data.action === "hold") {
//       console.log(`Held for approval: "${data.reply_text}" — check your dashboard.`);
//     } else {
//       // "ignore" — not on the allowlist, or a group chat with ALLOW_GROUPS off.
//       // No reply, no log spam, nothing sent back.
//     }
//   } catch (err) {
//     console.error("Error handling message:", err.message);
//   }
// });

// Chromium writes SingletonLock/Cookie/Socket into its profile, tagged with the
// hostname of the machine that created them. A container redeploy keeps the
// session volume (so the QR does not have to be scanned again) but gets a NEW
// hostname, so Chromium finds a lock belonging to "another computer", refuses
// to launch, and the bridge crash-loops without ever printing a QR.
//
// Only one bridge process ever uses this profile, so any lock found at startup
// is left over from a container that no longer exists.
function clearStaleChromiumLocks(dir) {
  const names = /^Singleton(Lock|Cookie|Socket)$/;
  let removed = 0;
  const walk = (d) => {
    let entries;
    try {
      entries = fs.readdirSync(d, { withFileTypes: true });
    } catch {
      return; // first run: the profile does not exist yet
    }
    for (const entry of entries) {
      const full = path.join(d, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (names.test(entry.name)) {
        try {
          fs.unlinkSync(full);
          removed += 1;
        } catch (err) {
          console.error(`Could not remove stale lock ${full}:`, err.message);
        }
      }
    }
  };
  walk(dir);
  if (removed) console.log(`Cleared ${removed} stale Chromium lock file(s) from a previous container.`);
}

clearStaleChromiumLocks(path.resolve(__dirname, ".wwebjs_auth"));

client.initialize().catch((err) => {
  console.error("Failed to start WhatsApp client:", err.message);
});