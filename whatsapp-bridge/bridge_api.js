// Local API so the Python backend can ask the bridge to send things.
// Binds loopback by default, needs a secret header, and can only message
// names listed in contacts.json (never a number taken from the request).

const fs = require("fs");
const http = require("http");
const path = require("path");
const { Location } = require("whatsapp-web.js");
const { mergeContacts, DEFAULT_LIMIT } = require("./contacts_sync");
const { shapeChats, shapeMessages, CHAT_ID, MESSAGE_ID, DEFAULT_CHATS,
  DEFAULT_MESSAGES, isReactionEmoji, canDeleteForEveryone, inGroup,
  PIN_DURATIONS } = require("./wa_read");
const { qrToSvg } = require("./qr_svg");

// Project root by default; CONTACTS_FILE moves it so the backend and the bridge
// can share one copy when they run as separate containers.
const CONTACTS_FILE = process.env.CONTACTS_FILE
  || path.resolve(__dirname, "..", "contacts.json");
const ROUTES = ["/send-text", "/send-location", "/send-reply", "/sync-contacts",
  "/chats", "/thread", "/send-self", "/media", "/avatar", "/link-status",
  // WhatsApp's own message menu, the half of it that has to happen on this
  // side. Reply is "/send-reply" and already above; Copy never leaves the
  // browser; Report is a moderation action this assistant does not take.
  "/react", "/forward", "/star", "/pin-message", "/delete-message"];
// The five above, which share their first three checks and their lookup.
const MESSAGE_ACTIONS = ["/react", "/forward", "/star", "/pin-message", "/delete-message"];
// Everything except /link-status needs a linked account. /link-status is the
// one route that exists precisely because there is not one.
const NEEDS_LINK = ROUTES.filter((r) => r !== "/link-status");
// A photo or a video can be large, and the whole thing is base64 in one JSON
// reply. Past this it is not worth holding in memory on either side.
const MAX_MEDIA_BYTES = 12 * 1024 * 1024;
let started = false; // "ready" can fire again after a reconnect

// The digits an id names, with neither the "@c.us" namespace nor the ":12" a
// second linked device adds. Two ids that have to be compared are compared on
// this, never whole.
const local = (id) => String(id || "").split("@")[0].split(":")[0];

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
  // The owner's own id, so a group they are no longer in can be told apart
  // from one they are still in, and a removal can read as "you were removed"
  // rather than "someone was". Absent until the client is linked, and an
  // empty id simply means those checks stay cautious.
  const ownId = () => (client.info && client.info.wid
    ? (client.info.wid._serialized || client.info.wid.$1) : "");

  /**
   * Finds the message an action names, and makes sure it really is in the
   * chat the caller named.
   *
   * Every message action needs both, in that order. Reacting to, forwarding,
   * pinning, keeping or deleting the wrong message is not something the owner
   * can take back, and an id alone names a message in any conversation the
   * session can reach — so holding a session must not be enough to act inside
   * a thread the caller did not name. The chat is compared on the numeric
   * local part, exactly as /media and /send-reply compare it, because one
   * conversation is addressed as both @lid and @c.us.
   *
   * Looked up the way /media looks a message up, through getMessageById,
   * rather than by fetching the thread and searching it: that is the one
   * route this codebase already uses, and it resolves a message whether or
   * not the page happens to be holding it.
   *
   * @returns {Promise<{msg: object}|{code: number, error: string}>} the
   *          message, or the refusal to send back — never both.
   */
  const findMessageIn = async (chatId, msgId) => {
    let msg;
    try {
      msg = await client.getMessageById(msgId);
    } catch (err) {
      // getMessageById throws out of the page for an id it cannot parse at
      // all. That is a caller handing over something shaped like an id and
      // not one, which reads as "no such message" rather than as a fault
      // here. The reason is logged; the id is not.
      console.log(`[act] lookup failed reason=${err.message}`);
      return { code: 404, error: "no such message" };
    }
    if (!msg) return { code: 404, error: "no such message" };
    const from = (msg.id && msg.id.remote) || msg.from || "";
    if (local(from) !== local(chatId)) {
      console.log("[act] refused reason=different_chat");
      return { code: 400, error: "message is from another chat" };
    }
    return { msg };
  };

  if (started) return null;

  const token = process.env.BRIDGE_API_TOKEN;
  const port = Number(process.env.BRIDGE_API_PORT || 3001);
  if (!token) {
    console.error("BRIDGE_API_TOKEN is not set - send API disabled.");
    return null;
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
          const limit = Math.min(Number(data.limit) || DEFAULT_CHATS, 600);
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
          const chats = shapeChats(await client.getChats(), { limit, blocked, me: ownId() });
          console.log(`[read] /chats returned=${chats.length}`);   // no names, numbers or text
          return reply(200, { chats });
        }

        if (req.url === "/thread") {
          const chatId = String(data.chat_id || "");
          if (!CHAT_ID.test(chatId)) return reply(400, { error: "bad chat id" });
          const limit = Math.min(Number(data.limit) || DEFAULT_MESSAGES, 100);
          const chat = await client.getChatById(chatId);
          if (!chat) return reply(404, { error: "no such chat" });
          const messages = shapeMessages(await chat.fetchMessages({ limit }), { limit, me: ownId() });
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

          // Every branch below says which one it was. These used to return a
          // bare 404 with nothing logged, so "the media does not show" was
          // indistinguishable from "the message is not here", "this device
          // never had the file" and "WhatsApp has expired it".
          const refuse = (reason, detail) => {
            console.log(`[read] /media refused reason=${reason}${detail ? ` ${detail}` : ""}`);
            return reply(404, { error: reason });
          };

          let msg;
          try {
            msg = await client.getMessageById(msgId);
          } catch (err) {
            return refuse("lookup_failed", err.message);
          }
          if (!msg) return refuse("no_such_message");
          // The id names the message; the chat is checked so that holding a
          // session cannot be used to read media out of a conversation the
          // caller did not name. Compared on the numeric local part, because
          // the same conversation is addressed as both @lid and @c.us.
          const from = (msg.id && msg.id.remote) || msg.from || "";
          if (local(from) !== local(chatId)) {
            return refuse("different_chat", `asked=${local(chatId)} is=${local(from)}`);
          }
          if (!msg.hasMedia) return refuse("no_media", `type=${msg.type}`);

          let media;
          try {
            media = await msg.downloadMedia();
          } catch (err) {
            // The usual cause: this device linked after the message was sent,
            // so it never held the file, and WhatsApp has since expired it.
            return refuse("download_failed", err.message);
          }
          if (!media || !media.data) return refuse("not_on_this_device", `type=${msg.type}`);
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
          // Which earlier message this answers, when the owner picked one.
          const quotedId = String(data.quoted_msg_id || "");
          // No hyphen here on purpose: that is what keeps groups out, which
          // the dashboard promises and the held queue depends on.
          if (!/^\d[\d:]*@(c\.us|lid)$/.test(chatId)) return reply(400, { error: "bad chat id" });
          if (!text || text.length > 4000) return reply(400, { error: "bad text" });
          // Checked rather than passed through, for the reason chat_id is:
          // this id is handed to the browser page to look up, and the page is
          // the one place a request's own string must never reach unexamined.
          if (quotedId && !MESSAGE_ID.test(quotedId)) return reply(400, { error: "bad quoted id" });
          // A quote carries the quoted words into the conversation, so an id
          // from another chat would copy one person's message into somebody
          // else's thread. The same check /media makes, on the same local
          // part, because one conversation is addressed as both @lid and
          // @c.us. Refused rather than dropped: a reply that quietly loses
          // its quote is not the message the owner wrote.
          if (quotedId && local(quotedId.split("_")[1]) !== local(chatId)) {
            return reply(400, { error: "quoted message is from another chat" });
          }
          if (!(await client.getChatById(chatId))) return reply(404, { error: "no such chat" });
          // ignoreQuoteErrors is true unless it is said to be false, and what
          // it ignores is the page failing to find the quoted message: the
          // text then goes out with the quote silently stripped off it. In a
          // busy thread that is a different message from the one the owner
          // wrote — they answered something specific — so the failure is
          // surfaced instead and they can decide what to send.
          const options = quotedId
            ? { quotedMessageId: quotedId, ignoreQuoteErrors: false } : {};
          let sent;
          try {
            sent = await client.sendMessage(chatId, text, options);
          } catch (err) {
            // Utils.js throws a bare Error("Could not get the quoted
            // message.") from inside the page, and it arrives wrapped in
            // puppeteer's own "Evaluation failed" wording — page internals,
            // which is not something to hand the dashboard as a 500. Nothing
            // was sent in this case: the throw happens while the message is
            // still being assembled.
            if (quotedId && /quoted message/i.test(String(err.message || ""))) {
              return reply(404, { error: "quoted message not found" });
            }
            throw err;
          }
          console.log("[send] /send-reply");
          // The id of what was just sent, so the backend can tie its own
          // record of this reply to the message now in the thread. Empty if
          // WhatsApp returned no message for it, which the caller has to
          // allow for — there is nothing here that could invent one.
          return reply(200, {
            status: "sent",
            msg_id: (sent && sent.id && (sent.id._serialized || sent.id.$1)) || "",
          });
        }

        // ---- WhatsApp's own message menu, acting on one existing message ----
        //
        // Five routes rather than one, because they are five different
        // consequences: a reaction the other person sees, a copy of a message
        // landing in a third conversation, a pin everyone in a group sees, a
        // bookmark only the owner sees, and a deletion. Reply is /send-reply
        // above; Copy never leaves the browser; Report is a moderation action
        // this assistant does not take on the owner's behalf.
        //
        // What is checked and in what order is the same for all five: the
        // chat id, the message id, then this action's own argument — all three
        // before the message is looked up, so a malformed request costs no
        // round trip through the browser — and the lookup last, which is also
        // what proves the message is in the chat the caller named.
        if (MESSAGE_ACTIONS.includes(req.url)) {
          const chatId = String(data.chat_id || "");
          const msgId = String(data.msg_id || "");
          // Groups are allowed here, unlike /send-reply: pinning, starring,
          // reacting and deleting in a group are all things the owner does
          // from their own phone, and this is the owner's own menu rather
          // than the assistant speaking for them.
          if (!CHAT_ID.test(chatId)) return reply(400, { error: "bad chat id" });
          // The same check /send-reply makes on a quoted id, for the same
          // reason: this string is handed to the browser page to look up.
          if (!MESSAGE_ID.test(msgId)) return reply(400, { error: "bad message id" });

          // Each action's own argument. Every one of them has to be stated:
          // there is no default for which kind of delete, or for pin against
          // unpin, because guessing wrong is visible to somebody else.
          let emoji = "";
          let destination = "";
          let seconds = 0;
          let everyone = false;
          let on = false;

          if (req.url === "/react") {
            // An absent key is not an empty one. "" is how the library takes
            // a reaction off again — Message.react("") — so it is accepted
            // and means exactly that, and a caller who simply forgot the
            // field must not have their reaction cleared for them.
            if (typeof data.emoji !== "string") return reply(400, { error: "emoji is required" });
            emoji = data.emoji;
            if (!isReactionEmoji(emoji)) return reply(400, { error: "bad emoji" });
          }

          if (req.url === "/forward") {
            destination = String(data.to_chat_id || "");
            if (!CHAT_ID.test(destination)) return reply(400, { error: "bad destination chat id" });
          }

          if (req.url === "/star" || req.url === "/pin-message") {
            const starring = req.url === "/star";
            const flag = starring ? data.starred : data.pinned;
            if (typeof flag !== "boolean") {
              return reply(400, { error: starring ? "starred must be true or false"
                : "pinned must be true or false" });
            }
            on = flag;
          }

          if (req.url === "/pin-message" && on) {
            seconds = PIN_DURATIONS[String(data.duration || "")] || 0;
            if (!seconds) {
              return reply(400, {
                error: `duration must be one of ${Object.keys(PIN_DURATIONS).join(", ")}`,
              });
            }
          }

          if (req.url === "/delete-message") {
            const scope = String(data.scope || "");
            if (scope !== "me" && scope !== "everyone") {
              return reply(400, { error: 'scope must be "me" or "everyone"' });
            }
            everyone = scope === "everyone";
          }

          const found = await findMessageIn(chatId, msgId);
          if (found.error) return reply(found.code, { error: found.error });
          const msg = found.msg;

          if (req.url === "/react") {
            await msg.react(emoji);
            // sendReaction resolves whether or not WhatsApp kept the
            // reaction, so what comes back says what was asked for rather
            // than claiming to know what WhatsApp did with it.
            console.log(`[act] /react ${emoji ? "set" : "cleared"}`);   // the emoji is content
            return reply(200, { status: emoji ? "reacted" : "removed" });
          }

          if (req.url === "/forward") {
            // getChatById resolves only a conversation that already exists,
            // which is the guard /send-reply leans on too — and the library's
            // own forward() is documented to fail for a chat the owner has
            // never been in. Refused here with something the dashboard can
            // show, rather than left to come back as page internals.
            if (!(await client.getChatById(destination))) {
              return reply(404, { error: "no such destination chat" });
            }
            await msg.forward(destination);
            console.log("[act] /forward");
            return reply(200, { status: "forwarded" });
          }

          if (req.url === "/star") {
            // Keep, in WhatsApp's own wording. The library checks whether the
            // message can be starred inside the page and does nothing at all
            // when it cannot, without saying so, so there is nothing to
            // report here beyond what was asked for. The read side carries
            // the state itself, off the message's own data.
            await (on ? msg.star() : msg.unstar());
            console.log(`[act] /star ${on ? "on" : "off"}`);
            return reply(200, { status: on ? "starred" : "unstarred" });
          }

          if (req.url === "/pin-message") {
            // The one action in this menu the library reports on: it returns
            // false when the page could not find the message or WhatsApp did
            // not accept the pin. A chat caps how many messages can be
            // pinned at once, so false is an ordinary answer that belongs to
            // the caller rather than inside a 500.
            const ok = on ? await msg.pin(seconds) : await msg.unpin();
            console.log(`[act] /pin-message ${on ? `on seconds=${seconds}` : "off"} ok=${ok}`);
            if (!ok) {
              return reply(409, { error: on ? "WhatsApp would not pin this message"
                : "WhatsApp would not unpin this message" });
            }
            return reply(200, { status: on ? "pinned" : "unpinned", seconds: on ? seconds : 0 });
          }

          // ---- delete ----
          // Asked before the attempt, because the library will not refuse a
          // revoke it cannot perform: past WhatsApp's limit, delete(true)
          // quietly deletes the message on this device only and resolves as
          // though it had taken it back from everybody. There is no error to
          // translate afterwards, which is why each of these is a refusal
          // rather than a rescue, and each says which of the three it is so
          // the dashboard has something true to show.
          if (everyone) {
            if (String(msg.type || "") === "revoked") {
              console.log("[act] /delete-message refused reason=already_deleted");
              return reply(409, { error: "this message has already been deleted" });
            }
            if (!msg.fromMe && !inGroup(msg)) {
              console.log("[act] /delete-message refused reason=not_mine");
              return reply(409, {
                error: "only the person who sent a message can delete it for everyone",
              });
            }
            if (!canDeleteForEveryone(msg)) {
              console.log("[act] /delete-message refused reason=too_late");
              return reply(409, {
                error: "too late to delete this for everyone - WhatsApp allows that for"
                  + " two days and twelve hours after a message is sent",
              });
            }
          }
          await msg.delete(everyone);
          console.log(`[act] /delete-message scope=${everyone ? "everyone" : "me"}`);
          return reply(200, { status: everyone ? "deleted for everyone" : "deleted for me" });
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
  // Handed back so a test can drive these routes against a stub client on a
  // port the operating system picks, and close the server afterwards. index.js
  // has no use for it and ignores it.
  return server;
}

module.exports = { startBridgeApi, readContacts };