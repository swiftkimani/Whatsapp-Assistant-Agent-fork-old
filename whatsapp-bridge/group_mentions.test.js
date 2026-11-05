const assert = require("node:assert/strict");
const { test } = require("node:test");

const { addressedIn, namePattern, namesFrom, repliesToOwner } = require("./group_mentions");

// The owner is one person with two ids. WhatsApp names her by phone number in
// some places and by privacy id in others, and the digits of the two are
// unrelated, so the bridge passes both and either one is her.
const OWNER_PHONE = "254797877563@c.us";
const OWNER_LID = "112257593794569@lid";
const OWNER_IDS = [OWNER_PHONE, OWNER_LID];
const NAMES = ["Arianna", "Arie"];

/** A group message with nothing special about it. */
function msg(extra = {}) {
  return { body: "who is bringing the chairs", mentionedIds: [], _data: {}, ...extra };
}

/** A message quoting another one, as WhatsApp serializes it. */
function quoting(quoted, context = {}) {
  return msg({ body: "yes do that", hasQuotedMsg: true, _data: { quotedMsg: quoted, ...context } });
}

const opts = (extra = {}) => ({ me: OWNER_IDS, names: NAMES, ...extra });

// ---- @mentions ----

test("an @mention of the owner is being addressed", () => {
  const r = addressedIn(msg({ mentionedIds: [OWNER_PHONE] }), opts());
  assert.deepEqual(r, { addressed: true, how: "mention" });
});

test("an @lid mention of the same account as a @c.us id is recognised as the same person", () => {
  // The group named her by privacy id; the bridge knows her by phone number
  // and by that id, and a mention of either is a mention of her.
  const r = addressedIn(msg({ mentionedIds: [OWNER_LID] }), opts());
  assert.equal(r.addressed, true);
  assert.equal(r.how, "mention");
});

test("a single owner id still works, without being wrapped in a list", () => {
  const r = addressedIn(msg({ mentionedIds: [OWNER_PHONE] }), opts({ me: OWNER_PHONE }));
  assert.equal(r.addressed, true);
});

test("a mention from the owner's second device is still the owner", () => {
  // WhatsApp writes a linked device as "…:12@c.us".
  const r = addressedIn(msg({ mentionedIds: ["254797877563:12@c.us"] }), opts());
  assert.equal(r.addressed, true);
});

test("a mention given as a wid object rather than a string is read too", () => {
  const wid = { _serialized: OWNER_PHONE, user: "254797877563", server: "c.us" };
  assert.equal(addressedIn(msg({ mentionedIds: [wid] }), opts()).addressed, true);
});

test("someone else being @mentioned is not being addressed", () => {
  const r = addressedIn(msg({ mentionedIds: ["254700000000@c.us"] }), opts());
  assert.deepEqual(r, { addressed: false, how: "" });
});

test("a privacy id that is not hers is not her", () => {
  const r = addressedIn(msg({ mentionedIds: ["999999999999@lid"] }), opts());
  assert.equal(r.addressed, false);
});

test("the owner's digits in the other namespace are not matched by accident", () => {
  // A lid is not a phone number with a different suffix: the two are separate
  // namespaces that happen to look alike, so they are never compared.
  const r = addressedIn(msg({ mentionedIds: ["254797877563@lid"] }), opts({ me: OWNER_PHONE }));
  assert.equal(r.addressed, false);
});

test("several people mentioned, one of them her, counts", () => {
  const r = addressedIn(msg({ mentionedIds: ["254700000000@c.us", OWNER_LID] }), opts());
  assert.equal(r.how, "mention");
});

// ---- quotes ----

test("a reply to one of her own messages is being addressed", () => {
  const r = addressedIn(quoting({ id: { fromMe: true }, body: "I'll ask her" }), opts());
  assert.deepEqual(r, { addressed: true, how: "reply" });
});

test("a reply to somebody else's message is not", () => {
  const r = addressedIn(quoting({ id: { fromMe: false }, body: "chairs" }), opts());
  assert.equal(r.addressed, false);
});

test("a quote with no flag on it falls back to who wrote the quoted message", () => {
  const mine = quoting({ body: "I'll ask her" }, { quotedParticipant: OWNER_PHONE });
  assert.equal(addressedIn(mine, opts()).how, "reply");
  const theirs = quoting({ body: "chairs" }, { quotedParticipant: "254700000000@c.us" });
  assert.equal(addressedIn(theirs, opts()).addressed, false);
});

test("a quote naming nobody at all is not read as hers", () => {
  assert.equal(repliesToOwner(quoting({ body: "chairs" }), OWNER_IDS), false);
});

test("a message that quotes nothing is not a reply to her", () => {
  assert.equal(repliesToOwner(msg(), OWNER_IDS), false);
});

test("a malformed quote does not throw", () => {
  for (const quoted of [null, "a string", 0, [], undefined]) {
    assert.equal(repliesToOwner(quoting(quoted), OWNER_IDS), false);
  }
  assert.equal(repliesToOwner(undefined, OWNER_IDS), false);
  assert.equal(repliesToOwner({}, OWNER_IDS), false);
});

// ---- her name in the words ----

test("her name in the message is being addressed", () => {
  const r = addressedIn(msg({ body: "Arianna what time did she say?" }), opts());
  assert.deepEqual(r, { addressed: true, how: "name" });
});

test("a nickname counts, whatever the case", () => {
  assert.equal(addressedIn(msg({ body: "ARIE you there?" }), opts()).how, "name");
});

test("a group talking about Arianna's shop is talking to her", () => {
  // Possessives, brackets and punctuation all end a word.
  for (const body of ["Arianna's shop is closed", "(arie) please", "ask Arianna."]) {
    assert.equal(addressedIn(msg({ body }), opts()).addressed, true, body);
  }
});

test("her name inside a longer word is not being addressed", () => {
  for (const body of ["ariannaesque taste in curtains", "Ariannas", "variannal",
                      "the barie stall", "we need more batteries"]) {
    assert.deepEqual(addressedIn(msg({ body }), opts()), { addressed: false, how: "" }, body);
  }
});

test("ordinary chatter is not being addressed", () => {
  assert.deepEqual(addressedIn(msg(), opts()), { addressed: false, how: "" });
});

test("a message with no body at all is not being addressed", () => {
  for (const body of [undefined, null, "", 0]) {
    assert.equal(addressedIn(msg({ body }), opts()).addressed, false);
  }
});

test("a name with a regex character in it is a name and not a pattern", () => {
  for (const name of ["A+", "Ari(e)", "a|b", "[Arie]", "^Arie$", "A.*e", "Ari\\e"]) {
    const names = [name];
    assert.equal(addressedIn(msg(), opts({ names })).addressed, false,
                 `${name} should not match unrelated chatter`);
    assert.equal(addressedIn(msg({ body: `hey ${name} you there` }), opts({ names })).addressed,
                 true, `${name} should match itself`);
  }
});

test("a name of pure punctuation is dropped rather than matching everything", () => {
  for (const name of [".", "*", ".*", "???", "  ", "-", "\\"]) {
    assert.equal(namePattern([name]), null, name);
    assert.equal(addressedIn(msg({ body: "so what time is it. who knows?" }),
                             opts({ names: [name] })).addressed, false, name);
  }
});

test("no names configured matches nothing", () => {
  assert.equal(namePattern([]), null);
  assert.equal(addressedIn(msg({ body: "Arianna" }), opts({ names: [] })).addressed, false);
});

// ---- where the names come from ----

test("the names are the same two settings the backend reads", () => {
  assert.deepEqual(namesFrom({}), ["Arianna", "Arie"]);
  assert.deepEqual(namesFrom({ ASSISTANT_NAME: "Zuri", ASSISTANT_NICKNAMES: "Zu, Zuzu" }),
                   ["Zuri", "Zu", "Zuzu"]);
});

test("clearing the nicknames leaves only her name", () => {
  assert.deepEqual(namesFrom({ ASSISTANT_NAME: "Zuri", ASSISTANT_NICKNAMES: "" }), ["Zuri"]);
});

test("the names come from the environment when none are passed in", () => {
  const r = addressedIn(msg({ body: "Zuri?" }), { me: OWNER_IDS, env: { ASSISTANT_NAME: "Zuri" } });
  assert.equal(r.how, "name");
});

// ---- precedence ----

test("a real mention is reported ahead of her name in the same message", () => {
  const both = msg({ body: "Arianna please", mentionedIds: [OWNER_PHONE] });
  assert.equal(addressedIn(both, opts()).how, "mention");
});

test("a quote of hers is reported ahead of her name in the same message", () => {
  const both = quoting({ id: { fromMe: true } });
  both.body = "thanks Arie";
  assert.equal(addressedIn(both, opts()).how, "reply");
});
