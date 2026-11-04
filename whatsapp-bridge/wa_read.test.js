const test = require("node:test");
const assert = require("node:assert");
const { shapeChats, shapeMessages } = require("./wa_read");

const chat = (over = {}) => ({
  id: { _serialized: "254712345678@c.us" },
  name: "Jane",
  isGroup: false,
  unreadCount: 0,
  timestamp: 1000,
  lastMessage: { body: "niaje", fromMe: false, timestamp: 1200, type: "chat", hasMedia: false },
  ...over,
});

const message = (over = {}) => ({
  id: { _serialized: "false_254712345678@c.us_ABC" },
  timestamp: 100, fromMe: false, body: "niaje", type: "chat", hasMedia: false, author: "",
  ...over,
});

test("a chat is reduced to what the dashboard shows", () => {
  assert.deepStrictEqual(shapeChats([chat({ unreadCount: 3 })]), [{
    id: "254712345678@c.us", name: "Jane", is_group: false,
    unread: 3, ts: 1200, preview: "niaje", from_me: false, archived: false, pinned: false,
  }]);
});

test("status updates, channels and broadcast lists are not conversations", () => {
  const out = shapeChats([
    chat({ id: { _serialized: "status@broadcast" } }),
    chat({ id: { _serialized: "120363000000000000@newsletter" } }),
    chat({ id: { _serialized: "1234567890@broadcast" } }),
    chat({ id: { _serialized: "status@broadcast" } }),           // not a conversation
    chat({ id: { _serialized: "254712345678@c.us" } }),
  ]);
  assert.deepStrictEqual(out.map((c) => c.id), ["254712345678@c.us"]);
});

test("groups and privacy-id chats are kept", () => {
  const out = shapeChats([
    chat({ id: { _serialized: "254700000000@g.us" }, isGroup: true, name: "Fam" }),
    chat({ id: { _serialized: "112257593794569@lid" }, name: "Lulu" }),
  ]);
  assert.deepStrictEqual(out.map((c) => [c.name, c.is_group]), [["Fam", true], ["Lulu", false]]);
});

test("the most recently active chat comes first, and the list is capped", () => {
  const out = shapeChats([
    chat({ id: { _serialized: "1@c.us" }, lastMessage: { timestamp: 50, body: "old" } }),
    chat({ id: { _serialized: "2@c.us" }, lastMessage: { timestamp: 900, body: "new" } }),
    chat({ id: { _serialized: "3@c.us" }, lastMessage: { timestamp: 500, body: "mid" } }),
  ], { limit: 2 });
  assert.deepStrictEqual(out.map((c) => c.id), ["2@c.us", "3@c.us"]);
});

test("a chat with no name falls back to the number, and a privacy id to Unknown", () => {
  const out = shapeChats([
    chat({ name: "", id: { _serialized: "254712345678@c.us" } }),
    chat({ name: undefined, id: { _serialized: "112257593794569@lid" } }),
  ]);
  assert.deepStrictEqual(out.map((c) => c.name).sort(), ["254712345678", "Unknown"]);
});

test("an attachment with no caption is described instead of showing blank", () => {
  const out = shapeChats([
    chat({ lastMessage: { body: "", hasMedia: true, type: "ptt", timestamp: 9 } }),
    chat({ id: { _serialized: "2@c.us" }, lastMessage: { body: "look", hasMedia: true, type: "image", timestamp: 8 } }),
  ]);
  assert.strictEqual(out[0].preview, "Voice note");
  assert.strictEqual(out[1].preview, "look");   // a caption is better than "Photo"
});

test("a chat that has never had a message still sorts and renders", () => {
  const out = shapeChats([chat({ lastMessage: undefined, timestamp: 77 })]);
  assert.deepStrictEqual([out[0].ts, out[0].preview], [77, ""]);
});

test("a long preview is cut to one line", () => {
  const out = shapeChats([chat({ lastMessage: { body: `x${"y".repeat(400)}\nz`, timestamp: 5 } })]);
  assert.ok(out[0].preview.length <= 90 && out[0].preview.endsWith("…"));
  assert.ok(!out[0].preview.includes("\n"));
});

test("a thread reads downwards, oldest first", () => {
  const out = shapeMessages([
    message({ timestamp: 300, body: "third" }),
    message({ timestamp: 100, body: "first" }),
    message({ timestamp: 200, body: "second" }),
  ]);
  assert.deepStrictEqual(out.map((m) => m.body), ["first", "second", "third"]);
});

test("a capped thread keeps the newest messages, not the oldest", () => {
  const many = Array.from({ length: 10 }, (_, i) => message({ timestamp: i, body: `m${i}` }));
  const out = shapeMessages(many, { limit: 3 });
  assert.deepStrictEqual(out.map((m) => m.body), ["m7", "m8", "m9"]);
});

test("who sent it survives, and a device suffix is dropped from a group author", () => {
  const out = shapeMessages([
    message({ fromMe: true, body: "mine" }),
    message({ author: "254722000111:12@c.us", body: "theirs", timestamp: 200 }),
  ]);
  assert.deepStrictEqual(out.map((m) => [m.from_me, m.author]), [[true, ""], [false, "254722000111"]]);
});

test("a voice note shows as one, with its caption when it has one", () => {
  const out = shapeMessages([
    message({ hasMedia: true, type: "ptt", body: "" }),
    message({ hasMedia: true, type: "image", body: "at the shop", timestamp: 200 }),
  ]);
  assert.deepStrictEqual(out.map((m) => [m.kind, m.body]), [["Voice note", ""], ["Photo", "at the shop"]]);
});

test("nothing to show is not an error", () => {
  assert.deepStrictEqual(shapeChats(undefined), []);
  assert.deepStrictEqual(shapeChats([null, {}]), []);
  assert.deepStrictEqual(shapeMessages(null), []);
});

// ---- media: what the dashboard needs in order to show it ----

const msg = (over = {}) => ({ id: { _serialized: "A" }, timestamp: 1, ...over });

test("a photo says how to render it as well as what to call it", () => {
  const [m] = shapeMessages([msg({ hasMedia: true, type: "image" })]);
  assert.strictEqual(m.kind, "Photo");
  assert.strictEqual(m.render_as, "image");
  assert.strictEqual(m.has_media, true);
});

test("a sticker is rendered as a sticker, not as a photo", () => {
  // Same element, smaller and without a frame: a sticker in a photo's box
  // reads as a tiny broken picture.
  assert.strictEqual(shapeMessages([msg({ hasMedia: true, type: "sticker" })])[0].render_as, "sticker");
});

test("a voice note and a plain audio file both play", () => {
  assert.strictEqual(shapeMessages([msg({ hasMedia: true, type: "ptt" })])[0].render_as, "voice");
  assert.strictEqual(shapeMessages([msg({ hasMedia: true, type: "audio" })])[0].render_as, "voice");
});

test("a video is rendered as a video", () => {
  assert.strictEqual(shapeMessages([msg({ hasMedia: true, type: "video" })])[0].render_as, "video");
});

test("a document keeps its label and is not rendered inline", () => {
  const [m] = shapeMessages([msg({ hasMedia: true, type: "document" })]);
  assert.strictEqual(m.kind, "Document");
  assert.strictEqual(m.render_as, "", "an unknown file must not be guessed at");
});

test("a view-once message is flagged so it is never fetched unasked", () => {
  const [m] = shapeMessages([msg({ hasMedia: true, type: "image", isViewOnce: true })]);
  assert.strictEqual(m.view_once, true);
  assert.strictEqual(m.render_as, "image", "it still knows how to show it once asked");
});

test("a message with no media carries no media fields to act on", () => {
  const [m] = shapeMessages([msg({ body: "just text" })]);
  assert.strictEqual(m.has_media, false);
  assert.strictEqual(m.render_as, "");
  assert.strictEqual(m.media_type, "");
});

test("an unknown media type is labelled rather than dropped", () => {
  const [m] = shapeMessages([msg({ hasMedia: true, type: "something_new" })]);
  assert.strictEqual(m.kind, "Attachment");
  assert.strictEqual(m.render_as, "");
});


// ---- blocked and archived ----

test("a blocked contact's chat is left out entirely", () => {
  // WhatsApp keeps blocked people in its own chat list, so without this they
  // appear here as ordinary conversations the assistant offers to answer.
  const chats = [chat({ id: { _serialized: "254712345678@c.us" }, name: "Kawira" }),
                 chat({ id: { _serialized: "254700000000@c.us" }, name: "Blocked" })];
  const out = shapeChats(chats, { blocked: new Set(["254700000000@c.us"]) });
  assert.deepStrictEqual(out.map((c) => c.name), ["Kawira"]);
});

test("no blocked list means nothing is dropped", () => {
  const chats = [chat({ id: { _serialized: "254712345678@c.us" }, name: "Kawira" })];
  assert.strictEqual(shapeChats(chats).length, 1);
});

test("an archived chat is reported as archived rather than dropped", () => {
  // Dropping it is how a conversation goes missing with nothing to explain it.
  const [c] = shapeChats([chat({ id: { _serialized: "254712345678@c.us" }, archived: true })]);
  assert.strictEqual(c.archived, true);
});

test("an ordinary chat is not marked archived", () => {
  const [c] = shapeChats([chat({ id: { _serialized: "254712345678@c.us" } })]);
  assert.strictEqual(c.archived, false);
});

// ---- what the phone shows, and what used to come through blank ----

test("a contact card is labelled, not dumped as raw vCard text", () => {
  // Message.body for a CONTACT_CARD *is* the vCard, and it was rendering as
  // the message: BEGIN:VCARD VERSION:3.0 FN:… across the bubble.
  const [m] = shapeMessages([{ id: { _serialized: "a" }, timestamp: 1, type: "vcard",
                               body: "BEGIN:VCARD\nVERSION:3.0\nFN:Jane" }]);
  assert.strictEqual(m.kind, "Contact card");
  assert.strictEqual(m.body, "", "the vCard syntax must not be shown as text");
});

test("types that carry no file are still labelled", () => {
  // These have no directPath, so hasMedia is false and they arrived with no
  // label at all — an empty bubble reading "(no text)".
  for (const [type, label] of [["location", "Location"], ["revoked", "Deleted message"],
                               ["multi_vcard", "Attachment"]]) {
    const [m] = shapeMessages([{ id: { _serialized: "a" }, timestamp: 1, type }]);
    assert.strictEqual(m.kind, label, type);
  }
});

test("a poll keeps its question as well as its label", () => {
  const [m] = shapeMessages([{ id: { _serialized: "a" }, timestamp: 1,
                               type: "poll_creation", body: "Lunch at one?" }]);
  assert.strictEqual(m.kind, "Poll");
  assert.strictEqual(m.body, "Lunch at one?");
});

test("a message keeps its line breaks", () => {
  // The page renders pre-wrap; collapsing whitespace here made every
  // multi-line message one run-on line before it ever got there.
  const [m] = shapeMessages([{ id: { _serialized: "a" }, timestamp: 1, body: "one\ntwo\nthree" }]);
  assert.strictEqual(m.body, "one\ntwo\nthree");
});

test("a preview is still a single line", () => {
  const [c] = shapeChats([chat({ lastMessage: { body: "one\ntwo", timestamp: 2 } })]);
  assert.strictEqual(c.preview, "one two");
});

test("pinned chats sort above more recent ones", () => {
  const mk = (id, name, extra) => chat({ id: { _serialized: id }, name, ...extra });
  // Chat exposes `pinned`; `pin` is the raw field it is built from.
  const out = shapeChats([mk("1@c.us", "recent", { timestamp: 9 }),
                          mk("2@c.us", "pinned", { pinned: true, timestamp: 1 })]);
  assert.deepStrictEqual(out.map((c) => c.name), ["pinned", "recent"]);
});
