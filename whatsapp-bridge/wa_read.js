// Shapes WhatsApp's own chats and messages for the dashboard to read.
//
// Read-only on purpose: nothing here marks a chat as seen, because opening a
// thread on the dashboard should not send a read receipt to the other person.
// Nothing here is stored either — the database holds only what went through the
// agent, and these are the owner's own chats passing through on their way to
// their own screen.
//
// Free of the WhatsApp client, so the shaping and filtering can be tested
// directly. Run the tests with: npm test

const PREVIEW_CHARS = 90;
const BODY_CHARS = 4000;
const DEFAULT_CHATS = 25;
const DEFAULT_MESSAGES = 50;

// A direct chat, a privacy-id chat, or a group. Status updates, channels and
// broadcast lists are not conversations and are left out.
// Digits, plus a ":12" multi-device suffix or a classic group's
// "<creator>-<created>" stamp. Only groups made since the renumbering use the
// all-digit 120363... form, so requiring that dropped every older group from
// the pane — the conversations were simply missing, with nothing to show why.
const CHAT_ID = /^\d[\d:-]*@(c\.us|lid|g\.us)$/;

// What the dashboard can render itself, and in which element. Anything not
// here keeps its written label: a document or a contact card is a file, not
// something to show inline, and guessing at one would break the layout.
const RENDERABLE = {
  image: "image", sticker: "sticker", video: "video", ptt: "voice", audio: "voice",
};

/** How the dashboard should show this, or "" for a label only. */
function renderAs(type) {
  return RENDERABLE[type] || "";
}

/** Something to show for a message with no text of its own. */
function mediaLabel(type) {
  return {
    image: "Photo", video: "Video", sticker: "Sticker", document: "Document",
    audio: "Audio", ptt: "Voice note", location: "Location", vcard: "Contact card",
    poll_creation: "Poll", revoked: "Deleted message",
  }[type] || "Attachment";
}

/** One line, for a preview. */
const clip = (s, max) => {
  const text = String(s || "").replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
};

/** The message itself, with its line breaks. A preview has to be one line;
 *  the body does not, and collapsing it turned every multi-line message into
 *  a single run-on line even though the page already renders pre-wrap. */
const clipBody = (s, max) => {
  const text = String(s || "").replace(/\r\n/g, "\n").replace(/[ \t]+\n/g, "\n").trim();
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
};

// Types that carry no downloadable file, so hasMedia is false and they used to
// arrive with no label at all — an empty bubble reading "(no text)".
const UNDOWNLOADABLE = new Set(["location", "vcard", "multi_vcard", "revoked", "poll_creation", "album"]);

/** Fall back to the number when WhatsApp has no name for the chat. */
function chatName(chat, id) {
  const given = String(chat.name || "").replace(/\s+/g, " ").trim();
  if (given) return clip(given, 60);
  const [local, domain] = id.split("@");
  return domain === "c.us" ? local : "Unknown";
}

/**
 * @param {object} [opts]
 * @param {Set<string>} [opts.blocked] ids the owner has blocked. Their chats
 *        stay in WhatsApp's own list, so without this they appear here as
 *        ordinary conversations the assistant offers to answer — which is the
 *        opposite of what blocking someone means.
 */
function shapeChats(chats, { limit = DEFAULT_CHATS, blocked = new Set() } = {}) {
  return (chats || [])
    .filter((chat) => chat && chat.id && CHAT_ID.test(chat.id._serialized || ""))
    .filter((chat) => !blocked.has(chat.id._serialized))
    .map((chat) => {
      const id = chat.id._serialized;
      const last = chat.lastMessage;
      const body = last ? clip(last.body, PREVIEW_CHARS) : "";
      return {
        id,
        name: chatName(chat, id),
        is_group: Boolean(chat.isGroup),
        unread: Number(chat.unreadCount) || 0,
        ts: Number(last ? last.timestamp : chat.timestamp) || 0,
        preview: body || (last && (last.hasMedia || UNDOWNLOADABLE.has(last.type))
          ? mediaLabel(last.type) : ""),
        from_me: Boolean(last && last.fromMe),
        // Archived chats are still chats. They are reported so the dashboard
        // can set them aside rather than silently dropping them, which is how
        // a conversation goes missing with nothing to explain it.
        archived: Boolean(chat.archived),
        pinned: Boolean(chat.pinned),
      };
    })
    // Pinned first, then most recently active — the order the phone shows.
    .sort((a, b) => (Number(b.pinned) - Number(a.pinned)) || (b.ts - a.ts))
    .slice(0, Math.max(1, limit));
}

function shapeMessages(messages, { limit = DEFAULT_MESSAGES } = {}) {
  return (messages || [])
    .filter(Boolean)
    .map((msg) => ({
      id: (msg.id && (msg.id._serialized || msg.id.id)) || "",
      ts: Number(msg.timestamp) || 0,
      from_me: Boolean(msg.fromMe),
      // In a group, who in the group said it.
      author: String(msg.author || "").split("@")[0].split(":")[0] || "",
      // A contact card's "body" is the raw vCard — BEGIN:VCARD VERSION:3.0 …
      // — which was being rendered as the message text.
      body: UNDOWNLOADABLE.has(msg.type) && msg.type !== "poll_creation"
        ? "" : clipBody(msg.body, BODY_CHARS),
      kind: (msg.hasMedia || UNDOWNLOADABLE.has(msg.type)) ? mediaLabel(msg.type) : "",
      has_media: Boolean(msg.hasMedia),
      // The raw type decides the element; the label above is the words.
      media_type: msg.hasMedia ? String(msg.type || "") : "",
      render_as: msg.hasMedia ? renderAs(msg.type) : "",
      // Opening one of these is not like opening a photo: WhatsApp shows the
      // sender it was opened, and it cannot be opened twice. So it is never
      // fetched with the thread — the dashboard asks for it only when the
      // owner says so.
      view_once: Boolean(msg.isViewOnce),
      // WhatsApp sends a GIF as a video; played with controls it looks like a
      // still that has to be clicked.
      is_gif: Boolean(msg.isGif),
      forwarded: Boolean(msg.isForwarded),
    }))
    .sort((a, b) => a.ts - b.ts)   // oldest first: a thread reads downwards
    .slice(-Math.max(1, limit));
}

module.exports = {
  shapeChats, shapeMessages, mediaLabel, chatName, renderAs,
  CHAT_ID, DEFAULT_CHATS, DEFAULT_MESSAGES,
};
