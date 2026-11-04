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
    multi_vcard: "Contact cards", poll_creation: "Poll", revoked: "Deleted message",
    call_log: "Call", ciphertext: "Waiting for this message", album: "Album",
    e2e_notification: "Security code changed", gp2: "Group update",
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

/** The account an id names: the digits, and the namespace they belong to.
 *  An id arrives either serialized or as the wid object it was built from,
 *  and a second linked device writes the same account as "…:12@c.us". */
function idParts(id) {
  if (!id) return { user: "", server: "" };
  const serialized = typeof id === "string" ? id : String(id._serialized || id.$1 || "");
  const [local, domain] = serialized.split("@");
  const user = local || (typeof id === "object" ? String(id.user || "") : "");
  const server = domain || (typeof id === "object" ? String(id.server || "") : "");
  return { user: user.split(":")[0], server };
}

/** Whether two ids name the same account. Digits alone are not enough: a
 *  phone number and a privacy id are two different namespaces that happen to
 *  look alike, so ids from different namespaces are treated as unrelated
 *  rather than compared digit by digit. */
function sameAccount(a, b) {
  const one = idParts(a);
  const other = idParts(b);
  if (!one.user || !other.user) return false;
  if (one.server && other.server && one.server !== other.server) return false;
  return one.user === other.user;
}

/**
 * Whether the owner is out of this group — removed from it, or gone from it
 * of their own accord. Either way the conversation is over, and WhatsApp
 * keeps it in the chat list regardless.
 *
 * Membership is the only honest test. `isReadOnly` looks like the answer and
 * is not: the library overwrites that column with the group's "only admins
 * can post" flag, so an announcement group the owner is still in reads as
 * read-only while a group they were thrown out of reads as writable.
 *
 * Answered only when WhatsApp actually said who is in the group. An empty
 * participant list means the metadata never arrived, not that the owner is
 * gone; and a group whose participants are all privacy ids cannot be measured
 * against a phone number at all, because the two namespaces do not line up.
 * Both of those report false, since leaving a dead group in the list is a
 * much smaller wrong than telling someone they are out of a group they run.
 */
function hasLeftGroup(chat, me) {
  if (!chat || !chat.isGroup) return false;
  // Read from the metadata rather than through the chat's own participants
  // getter, which is the same field and dereferences the metadata without
  // checking it is there.
  const participants = (chat.groupMetadata || {}).participants || [];
  if (!Array.isArray(participants) || !participants.length) return false;
  const mine = idParts(me);
  if (!mine.user) return false;
  const comparable = participants.some((p) => {
    const theirs = idParts(p && p.id).server;
    return !theirs || !mine.server || theirs === mine.server;
  });
  if (!comparable) return false;
  return !participants.some((p) => sameAccount(p && p.id, me));
}

// What a group system message says happened. WhatsApp sends these as a "gp2"
// message carrying no text of its own, so with nothing said here the newest
// thing in a group is a blank preview — which is how a group the owner was
// removed from months ago arrives at the top of the list saying nothing.
//
// The subtype says what happened but not who it happened to, so each one that
// can be about a person has two readings. Which one is used depends on the
// affected accounts WhatsApp names; the rest of the time the impersonal
// wording is the true one, because "You were removed" about someone else is a
// worse lie than vagueness. Subtypes with no person in them have one reading.
const GROUP_EVENTS = {
  add: { owner: "You were added", anyone: "Someone was added" },
  invite: { owner: "You joined with the invite link", anyone: "Someone joined with the invite link" },
  remove: { owner: "You were removed", anyone: "Someone was removed" },
  leave: { owner: "You left", anyone: "Someone left" },
  promote: { owner: "You are now an admin", anyone: "Someone is now an admin" },
  demote: { owner: "You are no longer an admin", anyone: "Someone is no longer an admin" },
  subject: { anyone: "Group name changed" },
  description: { anyone: "Group description changed" },
  picture: { anyone: "Group photo changed" },
  announce: { anyone: "Who can send messages changed" },
  restrict: { anyone: "Who can edit the group changed" },
};

/** What to show for a group system message, in words. An unrecognised
 *  subtype keeps the written label, which says something happened without
 *  pretending to know what. */
function groupEvent(msg, me) {
  const data = (msg && msg._data) || {};
  const words = GROUP_EVENTS[String(data.subtype || "")];
  if (!words) return mediaLabel("gp2");
  if (!words.owner) return words.anyone;
  const recipients = Array.isArray(data.recipients) ? data.recipients : [];
  return recipients.some((who) => sameAccount(who, me)) ? words.owner : words.anyone;
}

/**
 * @param {object} [opts]
 * @param {Set<string>} [opts.blocked] ids the owner has blocked. Their chats
 *        stay in WhatsApp's own list, so without this they appear here as
 *        ordinary conversations the assistant offers to answer — which is the
 *        opposite of what blocking someone means.
 * @param {string} [opts.me] the owner's own id. Without it there is no way to
 *        tell a group the owner is still in from one they are out of, nor a
 *        group event about them from one about somebody else, so both fall
 *        back to the cautious reading rather than being guessed at.
 */
function shapeChats(chats, { limit = DEFAULT_CHATS, blocked = new Set(), me = "" } = {}) {
  const budget = Math.max(1, limit);
  const shaped = (chats || [])
    .filter((chat) => chat && chat.id && CHAT_ID.test(chat.id._serialized || ""))
    .filter((chat) => !blocked.has(chat.id._serialized))
    .map((chat) => {
      const id = chat.id._serialized;
      const last = chat.lastMessage;
      const body = last ? clip(last.body, PREVIEW_CHARS) : "";
      const group = (chat.isGroup && chat.groupMetadata) || {};
      return {
        id,
        name: chatName(chat, id),
        is_group: Boolean(chat.isGroup),
        unread: Number(chat.unreadCount) || 0,
        ts: Number(last ? last.timestamp : chat.timestamp) || 0,
        // A group system message is the one case where the words come from
        // the event rather than from anything anybody typed.
        preview: last && last.type === "gp2" ? groupEvent(last, me)
          : body || (last && (last.hasMedia || UNDOWNLOADABLE.has(last.type))
            ? mediaLabel(last.type) : ""),
        from_me: Boolean(last && last.fromMe),
        // Archived chats are still chats. They are reported so the dashboard
        // can set them aside rather than silently dropping them, which is how
        // a conversation goes missing with nothing to explain it.
        archived: Boolean(chat.archived),
        pinned: Boolean(chat.pinned),
        // A group the owner is out of. WhatsApp keeps it, and its last word
        // was usually the notice of their leaving, so it claims a recent
        // timestamp it has not earned.
        left: hasLeftGroup(chat, me),
        // Whether this group sits inside a community, and whether it is the
        // community's own announcement group. Reported so the dashboard can
        // tell those apart from an ordinary group instead of reading the
        // name. These come from WhatsApp's own group metadata; the library's
        // `parentGroupId` is a different thing — an option for creating a
        // group — and is not present here.
        community: Boolean(group.parentGroup),
        is_community_parent: Boolean(group.isParentGroup),
      };
    })
    // Pinned first, as the phone shows them. Then live conversations above
    // dead ones, because a group the owner is out of should not outrank a
    // conversation they are still having just because the notice of their
    // removal happens to be the most recent thing in it. Then, within each,
    // most recently active.
    .sort((a, b) => (Number(b.pinned) - Number(a.pinned))
      || (Number(a.left) - Number(b.left))
      || (b.ts - a.ts));

  // The limit is a budget for the conversations the owner is actually looking
  // at. Capping the whole sorted list spent those slots on archived chats
  // that the dashboard then hides, so a pane that asked for a hundred
  // conversations showed far fewer while its count insisted nothing was
  // missing. Archived chats are still returned, under a budget of their own,
  // because the dashboard's Archived filter has nowhere else to get them.
  // The two buckets come back as one array with the live chats first, since
  // the page renders them in the order it is given.
  const live = shaped.filter((c) => !c.archived).slice(0, budget);
  const archived = shaped.filter((c) => c.archived).slice(0, budget);
  return live.concat(archived);
}

/**
 * @param {object} [opts]
 * @param {string} [opts.me] the owner's own id, for the same reason as in
 *        shapeChats: without it a removal inside a thread can only be read
 *        impersonally.
 */
function shapeMessages(messages, { limit = DEFAULT_MESSAGES, me = "" } = {}) {
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
      // Anything with no words of its own gets named. A bubble reading
      // "(no text)" tells the owner nothing — a voice note should say it is a
      // voice note whether or not its file can be fetched.
      // Anything with no words of its own gets named, so a voice note says it
      // is a voice note whether or not its file can be fetched. A plain text
      // message that is genuinely empty is left alone — calling that an
      // attachment would be a worse lie than saying nothing.
      // A group system message says what happened, here as in the chat list:
      // read in a thread, a removal should read as a removal and not as the
      // generic "Group update" that tells the owner nothing.
      kind: msg.type === "gp2" ? groupEvent(msg, me)
        : (msg.hasMedia || UNDOWNLOADABLE.has(msg.type)
           || (!String(msg.body || "").trim() && msg.type && msg.type !== "chat"))
          ? mediaLabel(msg.type) : "",
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
  idParts, sameAccount, hasLeftGroup, groupEvent,
  CHAT_ID, DEFAULT_CHATS, DEFAULT_MESSAGES,
};
