// Local API so the Python backend can ask the bridge to send things.
// Binds loopback by default, needs a secret header, and can only message
// names listed in contacts.json (never a number taken from the request).

const fs = require("fs");
const http = require("http");
const path = require("path");
const { Location } = require("whatsapp-web.js");
const { mergeContacts, DEFAULT_LIMIT } = require("./contacts_sync");
const { shapeChats, shapeMessages, CHAT_ID, DEFAULT_CHATS, DEFAULT_MESSAGES } = require("./wa_read");
const { qrToSvg } = require("./qr_svg");

// Project root by default; CONTACTS_FILE moves it so the backend and the bridge
// can share one copy when they run as separate containers.
const CONTACTS_FILE = process.env.CONTACTS_FILE
  || path.resolve(__dirname, "..", "contacts.json");
const ROUTES = ["/send-text", "/send-location", "/send-reply", "/sync-contacts",
  "/chats", "/thread", "/send-self", "/media", "/avatar", "/link-status"];
// Everything except /link-status needs a linked account. /link-status is the
// one route that exists precisely because there is not one.
const NEEDS_LINK = ROUTES.filter((r) => r !== "/link-status");
// A photo or a video can be large, and the whole thing is base64 in one JSON
// reply. Past this it is not worth holding in memory on either side.
const MAX_MEDIA_BYTES = 12 * 1024 * 1024;
let started = false; // "ready" can fire again after a reconnect

// Re-read on every request so edits to contacts.json apply without a restart.
function readContacts() {
  try {
    const raw = JSON.parse(fs.readFileSync(CONTACTS_FILE, "utf-8"));
    return Object.fromEntries(Object.entries(raw).map(([k, v]) => [String(k), String(v)]));
  } catch {
    return {};   // missing or hand-edited into invalid JSON: treat as empty
  }
}

// Written via a temporary file in the same directory and renamed over the old
// one, so a sync interrupted halfway cannot leave a truncated recipient list
// behind — a half-written file is an unparseable one, and the assistant would
// then have nobody it is allowed to message.
function writeContacts(contacts) {
  const tmp = `${CONTACTS_FILE}.${process.pid}.tmp`;
  fs.mkdirSync(path.dirname(CONTACTS_FILE), { recursive: true });
  fs.writeFileSync(tmp, `${JSON.stringify(contacts, null, 2)}\n`, "utf-8");
  fs.renameSync(tmp, CONTACTS_FILE);
}

function findEntry(name) {
  const wanted = String(name || "").trim().toLowerCase();
  for (const [key, value] of Object.entries(readContacts())) {
    if (key.trim().toLowerCase() === wanted) return value.trim();
  }
  return null;
}

/**
 * @param {object} client the WhatsApp client
 * @param {{linked: boolean, qr: string|null}} link live link state, owned by
 *        index.js and read on every request so the answer is never stale
 */
function startBridgeApi(client, link) {
  if (started) return;

  const token = process.env.BRIDGE_API_TOKEN;
  const port = Number(process.env.BRIDGE_API_PORT || 3001);
  if (!token) {
    console.error("BRIDGE_API_TOKEN is not set - send API disabled.");
    return;
  }
  started = true;

  const server = http.createServer((req, res) => {
    const reply = (code, obj) => {
      res.writeHead(code, { "Content-Type": "application/json" });
      res.end(JSON.stringify(obj));
    };

    if (req.method !== "POST" || !ROUTES.includes(req.url)) return reply(404, { error: "not found" });
    if (req.headers["x-bridge-token"] !== token) return reply(401, { error: "unauthorized" });

    // Is this account linked, and if not, what should be shown to link it?
    // Answered before the account exists, because the API now starts with the
    // process rather than on "ready" — an unlinked bridge used to refuse every
    // connection, which looked identical to a bridge that had crashed.
    if (req.url === "/link-status") {
      let qr = null;
      if (!link.linked && link.qr) {
        try {
          qr = qrToSvg(link.qr);
        } catch (err) {
          console.error("Could not draw the QR:", err.message);
        }
      }
      return reply(200, {
        linked: Boolean(link.linked),
        qr_svg: qr,
        // The eight-character alternative to scanning, for linking from the
        // same device the dashboard is open on.
        code: link.linked ? null : (link.code || null),
        // What the bridge is doing, and why the last attempt failed, so the
        // owner is not left guessing whether their scan even arrived.
        stage: link.stage || "",
        error: link.error || null,
      });
    }

    if (!link.linked && NEEDS_LINK.includes(req.url)) {
      return reply(503, { error: "not linked" });
    }

    let body = "";
    req.on("data", (chunk) => {
      body += chunk;
      if (body.length > 20000) req.destroy();
    });
    req.on("end", async () => {
      try {
        const data = body ? JSON.parse(body) : {};   // /sync-contacts needs no body

        // ---- reading the owner's own chats, for the dashboard's WhatsApp pane ----
        // Neither route marks anything as seen: opening a thread on the
        // dashboard must not send a read receipt to the other person.
        if (req.url === "/chats") {
          const limit = Math.min(Number(data.limit) || DEFAULT_CHATS, 200);
          // One call for the whole blocked list, rather than a contact lookup
          // per chat: that would be another browser round trip each.
          let blocked = new Set();
          try {
            blocked = new Set((await client.getBlockedContacts())
              .map((c) => (c && c.id && (c.id._serialized || c.id.$1)) || "")
              .filter(Boolean));
          } catch (err) {
            // Showing a blocked contact is worse than showing none of the
            // list, so say so rather than carrying on silently.
            console.error("Could not read the blocked list:", err.message);
            return reply(503, { error: "could not read the blocked list" });
          }
          const chats = shapeChats(await client.getChats(), { limit, blocked });
          console.log(`[read] /chats returned=${chats.length}`);   // no names, numbers or text
          return reply(200, { chats });
        }

        if (req.url === "/thread") {
          const chatId = String(data.chat_id || "");
          if (!CHAT_ID.test(chatId)) return reply(400, { error: "bad chat id" });
          const limit = Math.min(Number(data.limit) || DEFAULT_MESSAGES, 100);
          const chat = await client.getChatById(chatId);
          if (!chat) return reply(404, { error: "no such chat" });
          const messages = shapeMessages(await chat.fetchMessages({ limit }), { limit });
          console.log(`[read] /thread returned=${messages.length}`);
          return reply(200, {
            chat: { id: chatId, name: shapeChats([chat], { limit: 1 })[0].name, is_group: Boolean(chat.isGroup) },
            messages,
          });
        }

        // ---- one message's media, fetched only when the dashboard asks ----
        // Never bundled with /thread: a thread of fifty messages would mean
        // fifty downloads through the browser session for media most of which
        // is never looked at.
        if (req.url === "/media") {
          const chatId = String(data.chat_id || "");
          const msgId = String(data.msg_id || "");
          if (!CHAT_ID.test(chatId) || !msgId) return reply(400, { error: "bad request" });

          const msg = await client.getMessageById(msgId);
          if (!msg) return reply(404, { error: "no such message" });
          // The id names the message; the chat is checked so that holding a
          // session cannot be used to read media out of a conversation the
          // caller did not name.
          const from = (msg.id && msg.id.remote) || msg.from || "";
          if (String(from) !== chatId) return reply(404, { error: "no such message" });
          if (!msg.hasMedia) return reply(404, { error: "no media" });

          const media = await msg.downloadMedia();
          if (!media || !media.data) return reply(404, { error: "media is gone" });
          // WhatsApp drops media from its servers after a while, and an old
          // message in an open thread is exactly when that shows up.
          const bytes = Math.ceil((media.data.length * 3) / 4);
          if (bytes > MAX_MEDIA_BYTES) return reply(413, { error: "too large" });

          console.log(`[read] /media type=${msg.type} bytes=${bytes}`);  // no id, name or number
          return reply(200, {
            mimetype: media.mimetype || "application/octet-stream",
            data: media.data,
            filename: media.filename || "",
          });
        }

        // ---- a chat's profile photo ----
        // Returns the URL rather than the bytes: WhatsApp serves these from
        // its own CDN and the backend fetches it, so the browser never talks
        // to WhatsApp directly and the URL never reaches the page.
        if (req.url === "/avatar") {
          const chatId = String(data.chat_id || "");
          if (!CHAT_ID.test(chatId)) return reply(400, { error: "bad chat id" });
          const chat = await client.getChatById(chatId);
          if (!chat) return reply(404, { error: "no such chat" });
          const contact = await chat.getContact();
          // Undefined means no photo, or one the owner is not allowed to see
          // because of the other person's privacy setting. Both are "no photo".
          const url = contact ? await contact.getProfilePicUrl() : null;
          if (!url) return reply(404, { error: "no photo" });
          return reply(200, { url: String(url) });
        }

        if (req.url === "/send-self") {
          // A note in the owner's own "message yourself" chat, so what the
          // assistant sent in their name is visible on their phone without
          // marking the message the other person received. The recipient is
          // the linked account itself and is never taken from the request.
          const me = client.info && client.info.wid
            ? (client.info.wid._serialized || client.info.wid.$1) : "";
          if (!me) return reply(503, { error: "not linked yet" });
          const text = String(data.text || "").trim();
          if (!text || text.length > 4000) return reply(400, { error: "bad text" });
          await client.sendMessage(me, text);
          console.log("[send] /send-self");
          return reply(200, { status: "sent" });
        }

        if (req.url === "/send-reply") {
          // Reply into an existing conversation. Two callers: approving a held
          // reply, where the backend reads chat_id from its own database, and
          // the dashboard's WhatsApp pane, where it comes from the browser.
          // So the id is checked against WhatsApp itself rather than trusted —
          // getChatById only resolves a conversation that already exists, which
          // keeps a stolen session from starting one with a stranger. Group
          // chats are refused outright.
          const chatId = String(data.chat_id || "");
          const text = String(data.text || "").trim();
          // No hyphen here on purpose: that is what keeps groups out, which
          // the dashboard promises and the held queue depends on.
          if (!/^\d[\d:]*@(c\.us|lid)$/.test(chatId)) return reply(400, { error: "bad chat id" });
          if (!text || text.length > 4000) return reply(400, { error: "bad text" });
          if (!(await client.getChatById(chatId))) return reply(404, { error: "no such chat" });
          await client.sendMessage(chatId, text);
          console.log("[send] /send-reply");
          return reply(200, { status: "sent" });
        }

        if (req.url === "/sync-contacts") {
          // Copy the phone's own address book into contacts.json. Only ever
          // adds: see contacts_sync.js for what is skipped and why.
          const limit = Number(process.env.CONTACTS_SYNC_LIMIT || DEFAULT_LIMIT);
          const before = readContacts();
          const result = mergeContacts(await client.getContacts(), before, { limit });
          if (result.added.length) writeContacts(result.contacts);
          // Counts only: a name or a number here would put the owner's address
          // book in the host's logs.
          console.log(`[sync] contacts added=${result.added.length} kept=${result.kept} `
            + Object.entries(result.skipped).map(([k, v]) => `${k}=${v}`).join(" "));
          return reply(200, {
            status: "ok",
            added: result.added.length,
            names: result.added,
            total: Object.keys(result.contacts).length,
            skipped: result.skipped,
          });
        }

        const entry = findEntry(data.contact);
        if (!entry) return reply(400, { error: "unknown contact" });

        let chatId = entry; // a full id like 112257593794569@lid is used as-is
        if (!entry.includes("@")) {
          // plain phone number: ask WhatsApp for the matching id
          const wid = await client.getNumberId(entry.replace(/\D/g, ""));
          if (!wid) return reply(404, { error: "number is not on WhatsApp" });
          chatId = wid._serialized || wid.$1; // WhatsApp renamed _serialized to $1 in July 2026
        }

        if (req.url === "/send-text") {
          const text = String(data.text || "").trim();
          if (!text || text.length > 4000) return reply(400, { error: "bad text" });
          await client.sendMessage(chatId, text);
        } else {
          const { latitude, longitude } = data;
          if (
            !Number.isFinite(latitude) || !Number.isFinite(longitude) ||
            Math.abs(latitude) > 90 || Math.abs(longitude) > 180
          ) return reply(400, { error: "bad coordinates" });

          const mapsUrl = `https://maps.google.com/?q=${latitude},${longitude}`;
          try {
            await client.sendMessage(chatId, new Location(latitude, longitude, { name: "My location", url: mapsUrl }));
          } catch (err) {
            console.error("Pin failed, sending a map link instead:", err.stack || err);
            await client.sendMessage(chatId, mapsUrl);
          }
        }
        console.log(`[send] ${req.url} -> "${data.contact}"`); // no message text or coordinates in logs
        reply(200, { status: "sent" });
      } catch (err) {
        console.error(`${req.url} failed:`, err.stack || err);
        reply(500, { error: "request failed" });
      }
    });
  });

  // Loopback by default, so on a laptop nothing off the machine can reach it.
  // Under compose the backend is a different host, so BRIDGE_API_HOST is set to
  // 0.0.0.0 there — the port is never published outside the compose network and
  // every request still has to carry the shared token.
  const host = process.env.BRIDGE_API_HOST || "127.0.0.1";
  server.listen(port, host, () => console.log(`Bridge API on ${host}:${port}`));
}

module.exports = { startBridgeApi, readContacts };