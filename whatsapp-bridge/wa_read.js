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

// What a message id looks like. WhatsApp builds one out of whether the owner
// sent it, the chat it belongs to, WhatsApp's own id for the message, and —
// for a message somebody else sent in a group — who sent it:
// "true_254712345678@c.us_3EB0…", "false_…@g.us_…_254722000111@c.us".
// Here beside CHAT_ID because it exists for the same reason: /send-reply hands
// a quoted id to the browser page to look up, and a page is no place to send
// whatever string a request happened to carry. The id of the message itself is
// not plain hex everywhere — other clients write base64-ish ids — so the
// characters allowed there are wider than the ids this machine happens to see.
const MESSAGE_ID = /^(?:true|false)_\d[\d:-]*@(?:c\.us|lid|g\.us)_[A-Za-z0-9=-]{4,128}(?:_\d[\d:-]*@(?:c\.us|lid))?$/;

// A reaction is one emoji and nothing besides. It is checked rather than
// passed through for the reason a quoted id is: this string is handed to the
// browser page, and a page is no place to send whatever a request happened to
// carry.
//
// One grapheme cluster is the whole rule. That is what "one emoji" means once
// skin tones, variation selectors and the zero-width joiners inside a family
// emoji are counted, and it is the unit WhatsApp's own picker hands out:
// a family is seven code points and one cluster, while two thumbs-up are two
// clusters and are refused, because WhatsApp keeps one reaction per person
// per message and the second would simply be dropped. Counting clusters is
// also why there is no length limit in characters here — a bound in
// characters is either too small for a legitimate emoji or large enough to
// be no bound at all.
//
// The code points allowed are the pieces an emoji is built from: the pictures
// themselves, the skin-tone modifiers, the pair of regional indicators a flag
// is made of, the tag characters a subdivision flag adds, the zero-width
// joiner, the emoji variation selector, the enclosing keycap, and the digits
// and symbols a keycap is built on. Letters, punctuation and whitespace are
// not among them, so nothing that could read as text, markup or code gets
// as far as the page.
const EMOJI_PIECES = /^[\p{Extended_Pictographic}\p{Emoji_Modifier}\p{Regional_Indicator}\u200D\uFE0F\u20E3#*0-9\u{E0020}-\u{E007F}]+$/u;
// At least one of those pieces has to be a picture, or "123" — three keycap
// bases with no keycaps on them — would pass as a reaction.
const EMOJI_PICTURE = /[\p{Extended_Pictographic}\p{Regional_Indicator}\u20E3]/u;
const GRAPHEMES = new Intl.Segmenter(undefined, { granularity: "grapheme" });

/**
 * Whether this is something WhatsApp will take as a reaction.
 *
 * The empty string is deliberately valid. That is how the library removes a
 * reaction — Message.react("") — so "remove my reaction" is the same call
 * with nothing in it rather than a route of its own, and the dashboard has
 * one thing to call either way.
 */
function isReactionEmoji(value) {
  if (typeof value !== "string") return false;
  if (value === "") return true;
  if (!EMOJI_PIECES.test(value) || !EMOJI_PICTURE.test(value)) return false;
  return Array.from(GRAPHEMES.segment(value)).length === 1;
}

// How many emoji WhatsApp still draws large when a message is made of nothing
// else. WhatsApp's own clients stop at three: past that it is a message
// written in emoji rather than an emoji sent as the message, and it is laid
// out as ordinary text.
const EMOJI_ONLY_MAX = 3;

/**
 * Whether this message is nothing but a few emoji — "👍", "😂😂", "🎉" — and
 * so should be drawn large, the way WhatsApp draws it on the phone.
 *
 * Said here rather than worked out on the page for the reason the rest of this
 * file exists: the page would have to do the same grapheme counting in order
 * to get it right, and getting it wrong is visible. `Array.from(s).length`
 * counts code points, which makes one family emoji read as seven and one
 * flag as two, so a message of a single flag would be laid out as text while
 * a message of one family would be laid out as four emoji too many.
 *
 * The pieces an emoji may be built from are the same set `isReactionEmoji`
 * accepts, and for the same reason: everything else — letters, punctuation,
 * anything that could read as words — means this is text. Whitespace between
 * emoji does not, so it is taken out before counting; "👍 👍" is two emoji
 * and a space, not a sentence.
 */
function isEmojiOnly(text) {
  const compact = String(text || "").replace(/\s+/g, "");
  if (!compact) return false;
  if (!EMOJI_PIECES.test(compact) || !EMOJI_PICTURE.test(compact)) return false;
  return Array.from(GRAPHEMES.segment(compact)).length <= EMOJI_ONLY_MAX;
}

// How long a pinned message stays pinned, in seconds, under the names
// WhatsApp's own menu gives them. It offers exactly these three and nothing
// between, and the library hands the number straight to the page, so a fourth
// value would be one no WhatsApp client ever produces.
const PIN_DURATIONS = { "24h": 86400, "7d": 604800, "30d": 2592000 };

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

/**
 * `text` cut to at most `max` characters, never through a character.
 *
 * JavaScript measures and slices a string in UTF-16 code units, so a cut at a
 * fixed offset lands between the two halves of an emoji about half the time —
 * and a lone surrogate is not text. It survives as far as the JSON on the wire
 * and then arrives on the page as a replacement glyph: the owner sees "…a�…"
 * at the end of a preview that was fine on their phone.
 *
 * Grapheme clusters are the unit a person reads, and they are what is kept
 * whole here: an emoji with a skin tone, a family built out of zero-width
 * joiners, a flag made of two regional indicators and a Devanagari syllable
 * are each one cluster, each several code units long. Every cluster is either
 * kept entire or left out entirely.
 */
const cutTo = (text, max) => {
  if (text.length <= max) return text;
  const budget = max - 1;      // the ellipsis takes the last character
  let kept = "";
  for (const { segment } of GRAPHEMES.segment(text)) {
    if (kept.length + segment.length > budget) break;
    kept += segment;
  }
  return `${kept}…`;
};

/** One line, for a preview. */
const clip = (s, max) => cutTo(String(s || "").replace(/\s+/g, " ").trim(), max);

/** The message itself, with its line breaks. A preview has to be one line;
 *  the body does not, and collapsing it turned every multi-line message into
 *  a single run-on line even though the page already renders pre-wrap. */
const clipBody = (s, max) => cutTo(
  String(s || "").replace(/\r\n/g, "\n").replace(/[ \t]+\n/g, "\n").trim(), max);

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
 * The message a reply is answering, or null when this one is not a reply.
 *
 * Read straight off the raw serialized message, where the quote already is:
 * the library's own `hasQuotedMsg` is nothing more than a test for
 * `_data.quotedMsg`. Asking the library for the quoted message instead goes
 * through the browser page, which is one round trip per message — fifty of
 * them to open a single thread.
 *
 * What is read here is WhatsApp's own serialization rather than a shape the
 * library promises, so every field is treated as optional: a key that is not
 * there means there is nothing to show, never a thrown error halfway down a
 * thread the owner was only trying to read.
 */
function quotedOf(msg, me) {
  const data = (msg && msg._data) || {};
  const quoted = data.quotedMsg;
  if (!quoted || typeof quoted !== "object") return null;

  const type = String(quoted.type || "");
  const qid = (quoted.id && typeof quoted.id === "object") ? quoted.id : {};
  // The library computes a message's hasMedia rather than finding it — a
  // message has a file when WhatsApp gave a path to fetch it from — and the
  // quote is that same raw data, so it has no hasMedia of its own to read.
  const hasMedia = Boolean(quoted.directPath);
  // Which field holds the words is the library's rule too: the caption for
  // anything carrying a file, otherwise the body, with a poll's question and
  // an event's name standing in for the body neither of those has.
  const said = hasMedia ? quoted.caption : (quoted.body || quoted.pollName || quoted.eventName);
  // Who wrote it is named beside the quote and not inside it: WhatsApp puts
  // the quoted author in the replying message's own context, under a key of
  // its own, because a quote has no `author` field the way a message does.
  const who = data.quotedParticipant || quoted.participant || quoted.author || "";
  // Whether it was the owner's own message. The library reads this off a
  // message's id, where fromMe is the authority, so the id is asked first and
  // a flag beside it second. With neither stated, the only reading left is
  // whether the author is the owner — which answers "not the owner" when
  // there is no author to compare, and a quote shown as the other person's is
  // the harmless way to be wrong about this.
  const stated = [qid.fromMe, quoted.fromMe].find((f) => typeof f === "boolean");

  return {
    // Two ways to name the quoted message, so the dashboard can find it in a
    // thread it has already loaded: the serialized id, when the quote carries
    // one, and WhatsApp's own id for the message, which arrives beside the
    // quote. Neither is built out of the other — a serialized id assembled
    // from parts would name a message that does not exist, which is worse
    // than an empty string the dashboard can test for.
    id: typeof quoted.id === "string" ? quoted.id : String(qid._serialized || qid.$1 || ""),
    stanza_id: String(data.quotedStanzaID || qid.id || ""),
    from_me: typeof stated === "boolean" ? stated : sameAccount(who, me),
    author: idParts(who).user,
    // A contact card's body is the raw vCard — BEGIN:VCARD VERSION:3.0 … —
    // and a quote block is exactly where that run of syntax would land next.
    preview: UNDOWNLOADABLE.has(type) && type !== "poll_creation"
      ? "" : clip(said, PREVIEW_CHARS),
    // Named when it has no words of its own, as the message itself is, so a
    // quoted voice note reads as a voice note instead of as a blank line.
    kind: (hasMedia || UNDOWNLOADABLE.has(type)
           || (!String(said || "").trim() && type && type !== "chat"))
      ? mediaLabel(type) : "",
  };
}

// How long after a message was sent WhatsApp will still take it back from
// everybody: two days and twelve hours, WhatsApp's own published figure.
//
// Not a constant the library exposes. The real test lives in the page, in
// WAWebMsgActionCapability, and the library consults it without ever saying
// what it found — so this number is a restatement of somebody else's rule,
// and the day WhatsApp moves it this line is the one that has to change.
const REVOKE_WINDOW_SECONDS = 2 * 86400 + 12 * 3600;

/** Whether a message sits in a group conversation. Read off the chat its own
 *  id names, since a message is not handed the chat it came from: the id
 *  carries the chat under `remote`, and a message that arrived without one is
 *  placed by the serialized id it was built from, or by `from`. */
function inGroup(msg) {
  const id = (msg && msg.id) || {};
  const remote = typeof id.remote === "string"
    ? id.remote
    : String((id.remote && (id.remote._serialized || id.remote.$1)) || "");
  const named = remote
    || (typeof id._serialized === "string" ? id._serialized : "")
    || String((msg && msg.from) || "");
  return /@g\.us\b/.test(named);
}

/**
 * Whether "delete for everyone" is still worth offering for this message.
 *
 * WhatsApp settles this in the page and the library does not pass the answer
 * back: Message.delete(true) on a message that can no longer be revoked
 * quietly deletes it for this device only and resolves as though it had done
 * what was asked. There is therefore no failure to catch afterwards, and the
 * only honest moment to answer is before — which is the whole reason this is
 * derived here rather than left to the attempt.
 *
 * Deliberately cautious wherever the message alone cannot settle it:
 *   - a message already revoked has nothing left to take back;
 *   - a message with no timestamp cannot be placed inside the window at all;
 *   - in a one-to-one chat only the sender can revoke, so somebody else's
 *     message is never revocable there however recent it is;
 *   - in a group an admin can revoke anyone's message, and whether the owner
 *     is an admin is not on the message. That answers yes, because the cost
 *     of being wrong is an action that silently does the smaller thing,
 *     against the cost of hiding an action a group's admin really does have.
 *
 * @param {object} [opts]
 * @param {number} [opts.now] seconds since the epoch, so a thread can be
 *        shaped against a fixed clock in a test instead of the wall clock.
 */
function canDeleteForEveryone(msg, { now = Math.floor(Date.now() / 1000),
                                     windowSeconds = REVOKE_WINDOW_SECONDS } = {}) {
  if (!msg || String(msg.type || "") === "revoked") return false;
  const ts = Number(msg.timestamp) || 0;
  if (!ts) return false;
  if (now - ts > Math.max(0, windowSeconds)) return false;
  return Boolean(msg.fromMe) || inGroup(msg);
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
 * @param {number} [opts.now] seconds since the epoch, passed through to the
 *        one field that depends on how old a message is. Only a test has any
 *        reason to set it; the wall clock is the right answer in production.
 */
function shapeMessages(messages, { limit = DEFAULT_MESSAGES, me = "",
                                   now = Math.floor(Date.now() / 1000) } = {}) {
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
      // Whether this message is nothing but a few emoji, which WhatsApp draws
      // large. Asked of a plain text message and of nothing else: a photo's
      // caption is laid out as a caption whatever it says, and a poll whose
      // question happens to be an emoji is still a poll. Read off the raw
      // body rather than the clipped one because the answer cannot differ —
      // four emoji are already too many, and nothing that short is clipped.
      emoji_only: (!msg.type || msg.type === "chat") && isEmojiOnly(msg.body),
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
      // Whether anybody has reacted to this. The emoji themselves are not
      // here: WhatsApp keeps reactions in a collection of their own, and the
      // library's only road to them — Message.getReactions() — is a browser
      // round trip for one message, which is fifty of them to open a thread
      // most of whose messages nobody reacted to. So the menu is told there
      // is something to show and can ask for it when the owner opens it,
      // the way /media is asked for rather than bundled.
      has_reaction: Boolean(msg.hasReaction),
      // Starred — "Keep" in WhatsApp's own menu. This one is genuinely on the
      // message data, as `star`, so knowing it costs nothing.
      //
      // Pinned is not, and so is not reported: WhatsApp keeps pins in a table
      // of their own, and the library reads them a whole chat at a time
      // (Client.getPinnedMessages) rather than per message. Saying nothing is
      // better than a field that is false because nobody looked.
      starred: Boolean(msg.isStarred),
      // Whether offering "delete for everyone" would be honest. The library
      // will not refuse a revoke it cannot perform — it silently deletes the
      // message for this device only — so the menu has to decide beforehand
      // whether to grey the action out.
      can_delete_for_everyone: canDeleteForEveryone(msg, { now }),
      // The message this one is answering, so a reply reads as a reply rather
      // than as an answer to nothing in particular — which is how a thread of
      // one-word agreements reads with the quotes stripped out of it.
      quoted: quotedOf(msg, me),
    }))
    .sort((a, b) => a.ts - b.ts)   // oldest first: a thread reads downwards
    .slice(-Math.max(1, limit));
}

module.exports = {
  shapeChats, shapeMessages, mediaLabel, chatName, renderAs,
  idParts, sameAccount, hasLeftGroup, groupEvent,
  isReactionEmoji, isEmojiOnly, inGroup, canDeleteForEveryone,
  CHAT_ID, MESSAGE_ID, DEFAULT_CHATS, DEFAULT_MESSAGES,
  PIN_DURATIONS, REVOKE_WINDOW_SECONDS, EMOJI_ONLY_MAX,
};
