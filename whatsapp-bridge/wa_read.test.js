const test = require("node:test");
const assert = require("node:assert");
const { shapeChats, shapeMessages, MESSAGE_ID } = require("./wa_read");

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
    left: false, community: false, is_community_parent: false,
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
                               ["multi_vcard", "Contact cards"], ["ptt", "Voice note"],
                               ["call_log", "Call"]]) {
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

// ---- groups the owner is no longer in, and the system messages that say so ----
//
// These are the chats that were arriving at the very top of the pane with an
// empty preview: a group the owner was removed from or left, whose last word
// was the notice of their going.

const ME = "254733000222@c.us";

const group = (over = {}) => chat({
  id: { _serialized: "120363000000000001@g.us" },
  name: "Fam",
  isGroup: true,
  groupMetadata: { participants: [{ id: ME }, { id: "254722000111@c.us" }] },
  ...over,
});

const notice = (subtype, recipients) => ({
  timestamp: 9000, type: "gp2", body: "", hasMedia: false, _data: { subtype, recipients },
});

test("a group the owner is no longer in is marked left and sinks below older live chats", () => {
  const out = shapeChats([
    group({ name: "Removed", groupMetadata: { participants: [{ id: "254722000111@c.us" }] },
            lastMessage: notice("remove", [ME]) }),
    chat({ name: "Jane", lastMessage: { timestamp: 50, body: "niaje", type: "chat" } }),
  ], { me: ME });
  assert.deepStrictEqual(out.map((c) => [c.name, c.left]), [["Jane", false], ["Removed", true]]);
});

test("a group the owner is still in is not marked left, device suffix and all", () => {
  // A second linked device writes the same account as "…:12@c.us"; it is the
  // same person, on whichever side the suffix happens to appear.
  const [theirs] = shapeChats([group({
    groupMetadata: { participants: [{ id: "254733000222:12@c.us" }, { id: "254722000111@c.us" }] },
  })], { me: ME });
  assert.strictEqual(theirs.left, false);

  const [mine] = shapeChats([group({
    // A participant id can also arrive as the wid object it was built from.
    groupMetadata: { participants: [{ id: { _serialized: ME, user: "254733000222", server: "c.us" } }] },
  })], { me: "254733000222:12@c.us" });
  assert.strictEqual(mine.left, false);
});

test("a group with no participant list is not guessed to be left", () => {
  // An empty list means the metadata never arrived, not that the owner is
  // gone. Saying they left a group they run is the worse of the two wrongs.
  for (const metadata of [undefined, {}, { participants: [] }, { participants: null }]) {
    const [c] = shapeChats([group({ groupMetadata: metadata })], { me: ME });
    assert.strictEqual(c.left, false, String(JSON.stringify(metadata)));
  }
});

test("not knowing who the owner is means no group is called left", () => {
  const [c] = shapeChats([group({ groupMetadata: { participants: [{ id: "254722000111@c.us" }] } })]);
  assert.strictEqual(c.left, false);
});

test("privacy ids and phone numbers are not compared, so neither reads as left", () => {
  // When toPn cannot map a group's participants they stay as @lid, and the
  // digits of a lid have nothing to do with the digits of a phone number.
  const [theirs] = shapeChats([group({
    groupMetadata: { participants: [{ id: "112257593794569@lid" }, { id: "118800000000001@lid" }] },
  })], { me: ME });
  assert.strictEqual(theirs.left, false);

  const [mine] = shapeChats([group({
    groupMetadata: { participants: [{ id: "254722000111@c.us" }, { id: "254700000000@c.us" }] },
  })], { me: "112257593794569@lid" });
  assert.strictEqual(mine.left, false);
});

test("a group system message says what happened instead of showing blank", () => {
  const words = (subtype, recipients) => shapeChats(
    [group({ lastMessage: notice(subtype, recipients) })], { me: ME },
  )[0].preview;
  assert.strictEqual(words("remove", [ME]), "You were removed");
  assert.strictEqual(words("leave", [ME]), "You left");
  assert.strictEqual(words("add", [ME]), "You were added");
  assert.strictEqual(words("subject"), "Group name changed");
  assert.strictEqual(words("picture"), "Group photo changed");
  assert.strictEqual(words("promote", [ME]), "You are now an admin");
  // Nothing here knows what a subtype WhatsApp has not shipped yet means.
  assert.strictEqual(words("something_new", [ME]), "Group update");
});

test("it only says the owner when the recipients name the owner", () => {
  const words = (recipients) => shapeChats(
    [group({ lastMessage: notice("remove", recipients) })], { me: ME },
  )[0].preview;
  assert.strictEqual(words([ME]), "You were removed");
  assert.strictEqual(words(["254722000111@c.us"]), "Someone was removed");
  assert.strictEqual(words([{ _serialized: ME }]), "You were removed");
  assert.strictEqual(words(undefined), "Someone was removed");
  // Without the owner's id there is nothing to check the list against.
  assert.strictEqual(shapeChats([group({ lastMessage: notice("remove", [ME]) })])[0].preview,
                     "Someone was removed");
});

test("a removal reads as a removal inside the thread too", () => {
  const gp2 = (over) => ({ id: { _serialized: "a" }, timestamp: 1, type: "gp2", body: "", ...over });
  const kind = (over, opts) => shapeMessages([gp2(over)], opts)[0].kind;
  assert.strictEqual(kind({ _data: { subtype: "remove", recipients: [ME] } }, { me: ME }),
                     "You were removed");
  assert.strictEqual(kind({ _data: { subtype: "add", recipients: ["254722000111@c.us"] } }, { me: ME }),
                     "Someone was added");
  assert.strictEqual(kind({ _data: {} }), "Group update");
});

test("a pinned chat still comes first, against a live chat and a dead group alike", () => {
  const out = shapeChats([
    group({ name: "dead", groupMetadata: { participants: [{ id: "254722000111@c.us" }] },
            lastMessage: { timestamp: 9000, body: "x", type: "chat" } }),
    chat({ name: "live", lastMessage: { timestamp: 500, body: "x" } }),
    chat({ id: { _serialized: "254700000000@c.us" }, name: "pinned", pinned: true,
           lastMessage: { timestamp: 1, body: "x" } }),
  ], { me: ME });
  assert.deepStrictEqual(out.map((c) => c.name), ["pinned", "live", "dead"]);
});

test("the limit is a budget for live chats, and the archived ones still come through", () => {
  // Capping the whole list spent the budget on chats the dashboard then
  // hides, so a pane asking for a hundred conversations showed far fewer.
  const chats = [];
  for (let i = 1; i <= 4; i += 1) {
    chats.push(chat({ id: { _serialized: `25470000000${i}@c.us` }, name: `live${i}`,
                      lastMessage: { timestamp: 100 + i, body: "x" } }));
    chats.push(chat({ id: { _serialized: `25471000000${i}@c.us` }, name: `old${i}`, archived: true,
                      lastMessage: { timestamp: 50 + i, body: "x" } }));
  }
  const out = shapeChats(chats, { limit: 2, me: ME });
  assert.deepStrictEqual(out.filter((c) => !c.archived).map((c) => c.name), ["live4", "live3"]);
  assert.deepStrictEqual(out.filter((c) => c.archived).map((c) => c.name), ["old4", "old3"]);
  // Live first: the page renders them in the order it is given.
  assert.deepStrictEqual(out.map((c) => c.archived), [false, false, true, true]);
});

test("a community's subgroup and its announcement group are reported as such", () => {
  const made = (extra) => shapeChats(
    [group({ groupMetadata: { participants: [{ id: ME }], ...extra } })], { me: ME },
  )[0];
  const sub = made({ parentGroup: { _serialized: "120363000000000009@g.us" } });
  assert.deepStrictEqual([sub.community, sub.is_community_parent], [true, false]);
  const parent = made({ isParentGroup: true });
  assert.deepStrictEqual([parent.community, parent.is_community_parent], [false, true]);
  const plain = made({});
  assert.deepStrictEqual([plain.community, plain.is_community_parent], [false, false]);
});

test("a direct chat is never a community or a group the owner left", () => {
  const [c] = shapeChats([chat()], { me: ME });
  assert.deepStrictEqual([c.left, c.community, c.is_community_parent], [false, false, false]);
});


// ---- replies: the message a message is answering ----
//
// The quote is already on the raw message, so these go through _data rather
// than through the client: reading it any other way is a browser round trip
// per message, fifty of them to open one thread.

// A reply as it arrives: `quotedMsg` is WhatsApp's serialization of the
// message being answered, and the fields naming it sit beside the quote.
const quoting = (quotedMsg, beside = {}, over = {}) =>
  message({ _data: { quotedMsg, ...beside }, ...over });
const quoteOf = (quotedMsg, beside, opts) =>
  shapeMessages([quoting(quotedMsg, beside)], opts)[0].quoted;

test("a reply carries the message it answers, and who wrote it", () => {
  assert.deepStrictEqual(
    quoteOf({ type: "chat", body: "can you send it today?" },
            { quotedStanzaID: "3EB0AA11BB22", quotedParticipant: "254722000111@c.us" },
            { me: ME }),
    { id: "", stanza_id: "3EB0AA11BB22", from_me: false,
      author: "254722000111", preview: "can you send it today?", kind: "" },
  );
});

test("a quoted message is the owner's own when WhatsApp names them as its author", () => {
  // Nothing in the raw quote says whose message it was, so the author it is
  // attributed to is the only reading there is.
  const sender = (participant, me) =>
    quoteOf({ type: "chat", body: "on my way" }, { quotedParticipant: participant }, { me });
  assert.strictEqual(sender(ME, ME).from_me, true);
  assert.strictEqual(sender("254722000111@c.us", ME).from_me, false);
  // A second linked device writes the same account as "…:12@c.us".
  assert.strictEqual(sender("254733000222:12@c.us", ME).from_me, true);
  // With nobody to compare against, a quote reads as the other person's: a
  // quote wrongly labelled the owner's own is the worse of the two wrongs.
  assert.strictEqual(sender(ME, "").from_me, false);
  assert.strictEqual(sender("", ME).from_me, false);
});

test("what the raw quote says about who sent it beats the reading from its author", () => {
  // The library takes a message's fromMe off its id, so the id is believed
  // here too, over an author that disagrees with it.
  const quoted = quoteOf(
    { type: "chat", body: "I will", id: { fromMe: true, _serialized: "true_254712345678@c.us_3EB0AA11BB22", id: "3EB0AA11BB22" } },
    { quotedParticipant: "254722000111@c.us" }, { me: ME },
  );
  assert.strictEqual(quoted.from_me, true);
  assert.strictEqual(quoted.id, "true_254712345678@c.us_3EB0AA11BB22");
  assert.strictEqual(quoted.stanza_id, "3EB0AA11BB22");
});

test("a quoted photo with no caption is named rather than left blank", () => {
  // An unnamed quote block is a bubble pointing at nothing, which tells the
  // owner less than their own thread already did.
  const photo = quoteOf({ type: "image", directPath: "/v/t62.7118", caption: "" });
  assert.deepStrictEqual([photo.kind, photo.preview], ["Photo", ""]);
  assert.strictEqual(quoteOf({ type: "ptt", directPath: "/v/t62.7117" }).kind, "Voice note");
  // The words of anything carrying a file live in its caption; its body is
  // not the text the owner typed.
  const captioned = quoteOf({ type: "image", directPath: "/v/x", caption: "at the shop" });
  assert.deepStrictEqual([captioned.kind, captioned.preview], ["Photo", "at the shop"]);
});

test("a quoted message that carries no file is labelled all the same", () => {
  // These have no directPath, so there is nothing to call media about them,
  // and they used to arrive as an empty quote.
  assert.strictEqual(quoteOf({ type: "location" }).kind, "Location");
  assert.strictEqual(quoteOf({ type: "revoked" }).kind, "Deleted message");
  assert.strictEqual(quoteOf({ type: "something_new" }).kind, "Attachment");
});

test("a quoted poll keeps its question, wherever WhatsApp put it", () => {
  const written = quoteOf({ type: "poll_creation", body: "Lunch at one?" });
  assert.deepStrictEqual([written.kind, written.preview], ["Poll", "Lunch at one?"]);
  // A poll's question is not in a body at all when WhatsApp serializes it.
  assert.strictEqual(quoteOf({ type: "poll_creation", pollName: "Lunch at one?" }).preview,
                     "Lunch at one?");
});

test("a quoted contact card is labelled, not dumped as raw vCard text", () => {
  // A contact card's body *is* the vCard, and the quote block is the next
  // place that run of syntax would have been rendered as somebody's words.
  const card = quoteOf({ type: "vcard", body: "BEGIN:VCARD\nVERSION:3.0\nFN:Jane" });
  assert.strictEqual(card.kind, "Contact card");
  assert.strictEqual(card.preview, "", "the vCard syntax must not be shown as text");
});

test("a quote is one clipped line, however long the message it answers was", () => {
  const long = quoteOf({ type: "chat", body: `x${"y".repeat(400)}\nz` });
  assert.ok(long.preview.length <= 90 && long.preview.endsWith("…"));
  assert.ok(!long.preview.includes("\n"), "a quote block is a single line");
});

test("a message that is not a reply has no quote rather than an empty one", () => {
  assert.strictEqual(shapeMessages([message()])[0].quoted, null);
  for (const nothing of [null, undefined, "", 0, false, "BEGIN:VCARD"]) {
    assert.strictEqual(quoteOf(nothing), null, String(nothing));
  }
  assert.strictEqual(shapeMessages([message({ _data: undefined })])[0].quoted, null);
});

test("a partial quote degrades to what is known instead of throwing", () => {
  // This runs over every message in every thread, so a key WhatsApp did not
  // send has to read as nothing to show — one missing field cannot be
  // allowed to take a whole conversation down with it.
  for (const partial of [{}, { type: "chat" }, { body: "" }, { type: null, body: null },
                         { id: null }, { id: "false_254712345678@c.us_3EB0AA11BB22" },
                         { type: "chat", body: { unexpected: true } }]) {
    const quoted = quoteOf(partial, {}, { me: ME });
    assert.strictEqual(quoted.from_me, false, JSON.stringify(partial));
    assert.strictEqual(quoted.author, "", JSON.stringify(partial));
    assert.strictEqual(typeof quoted.id, "string", JSON.stringify(partial));
    assert.strictEqual(typeof quoted.stanza_id, "string", JSON.stringify(partial));
    assert.strictEqual(typeof quoted.preview, "string", JSON.stringify(partial));
    assert.strictEqual(typeof quoted.kind, "string", JSON.stringify(partial));
  }
  // A quote with nothing in it at all says nothing, rather than guessing at a
  // label for a message whose type never arrived.
  assert.deepStrictEqual(quoteOf({}), { id: "", stanza_id: "", from_me: false,
                                        author: "", preview: "", kind: "" });
  // An id is reported in whichever form the raw data holds it, and never
  // assembled out of parts: an invented id names a message that is not there.
  assert.strictEqual(quoteOf({ id: "false_254712345678@c.us_3EB0AA11BB22" }).id,
                     "false_254712345678@c.us_3EB0AA11BB22");
  assert.strictEqual(quoteOf({}, { quotedStanzaID: "3EB0AA11BB22" }).stanza_id, "3EB0AA11BB22");
});

test("a message id is recognised by its real shape, and junk is refused", () => {
  // /send-reply hands a quoted id to the browser page to look up, so what is
  // accepted here is what WhatsApp itself writes and nothing besides.
  for (const id of [
    "true_254712345678@c.us_3EB0C767D097F5A2C7C1",
    "false_254712345678@c.us_3EB0C767D097F5A2C7C1",
    "false_112257593794569@lid_BAE5A1B2C3D4E5F6",
    "false_120363000000000001@g.us_3EB0AA11BB22_254722000111@c.us",
    "true_254700000000-1234567890@g.us_3EB0AA11BB22",   // a group made before the renumbering
    "false_254712345678:12@c.us_3EB0AA11BB22",          // a second linked device
    "false_254712345678@c.us_AbC-dEf=",                 // not every client writes hex
  ]) assert.ok(MESSAGE_ID.test(id), id);

  for (const junk of [
    "", "nonsense", "undefined",
    "254712345678@c.us",                                // a chat id, not a message id
    "maybe_254712345678@c.us_3EB0AA11BB22",             // the owner sent it or did not
    "true_254712345678@c.us",                           // nothing naming the message
    "true_@c.us_3EB0AA11BB22",                          // no account in it
    "true_254712345678@newsletter_3EB0AA11BB22",        // a channel is not a conversation
    "true_254712345678@c.us_3EB",                       // too short to be a real id
    "true_254712345678@c.us_3EB0AA11BB22 ",
    "true_254712345678@c.us_3EB0AA11BB22\n",
    "true_254712345678@c.us_3EB0AA;rm -rf /",
    "true_254712345678@c.us_3EB0AA11BB22_254722000111@g.us",   // a group cannot be the author
    `true_254712345678@c.us_${"A".repeat(200)}`,
  ]) assert.ok(!MESSAGE_ID.test(junk), JSON.stringify(junk));
});
