const assert = require("node:assert/strict");
const { test } = require("node:test");

const { checkAllowed } = require("./allowlist");

const ALLOWED = ["254797877563", "254776599915"];
const opts = (extra = {}) => ({ allowed: ALLOWED, allowGroups: false, ...extra });

test("a listed number in the normal form gets through", async () => {
  const r = await checkAllowed("254776599915@c.us", opts());
  assert.equal(r.allowed, true);
  assert.equal(r.kind, "direct");
});

test("a number not on the list is refused", async () => {
  const r = await checkAllowed("254700000000@c.us", opts());
  assert.equal(r.allowed, false);
  assert.equal(r.reason, "not_in_allowlist");
});

test("groups are refused even when the sender is listed", async () => {
  const r = await checkAllowed("254776599915-1497087597@g.us", opts());
  assert.equal(r.allowed, false);
  assert.equal(r.reason, "groups_off");
});

test("groups get through when they are switched on", async () => {
  const r = await checkAllowed("254776599915@g.us", opts({ allowGroups: true }));
  assert.equal(r.allowed, true);
});

test("a device suffix does not stop a match", async () => {
  // WhatsApp appends ":12" for a specific device on multi-device accounts.
  const r = await checkAllowed("254776599915:12@c.us", opts());
  assert.equal(r.allowed, true);
});

test("the same person stored in another format still matches", async () => {
  // Saved locally as 0776599915, listed as 254776599915 — one human.
  const r = await checkAllowed("0776599915@c.us", opts());
  assert.equal(r.allowed, true, "last-9-digit comparison should match");
});

test("an @lid privacy id is resolved to a real number before comparing", async () => {
  const r = await checkAllowed("112257593794569@lid", opts({
    resolveContact: async () => ({ number: "+254 776 599915" }),
  }));
  assert.equal(r.allowed, true);
  assert.equal(r.number, "254776599915", "punctuation should be stripped");
});

test("an @lid that resolves to someone else is refused", async () => {
  const r = await checkAllowed("999999999999@lid", opts({
    resolveContact: async () => ({ number: "254700000000" }),
  }));
  assert.equal(r.allowed, false);
});

test("an @lid that cannot be resolved fails closed", async () => {
  const r = await checkAllowed("112257593794569@lid", opts({
    resolveContact: async () => { throw new Error("offline"); },
  }));
  assert.equal(r.allowed, false, "an unresolvable id must not be let through");
  assert.equal(r.reason, "lid_unresolved");
});

test("an empty allowlist lets everyone through, which is the documented risk", async () => {
  const r = await checkAllowed("254700000000@c.us", { allowed: [], allowGroups: false });
  assert.equal(r.allowed, true);
  assert.equal(r.reason, "allowlist_empty");
});

// ---- "contacts": the address book as the allowlist ----

const book = (...numbers) => () => numbers;

test("everyone in the address book gets through when contacts is listed", async () => {
  const opts = { allowed: ["contacts"], addressBook: book("254712345678", "0722000111") };
  assert.strictEqual((await checkAllowed("254712345678@c.us", opts)).allowed, true);
  assert.strictEqual((await checkAllowed("254722000111@c.us", opts)).allowed, true);  // stored locally
});

test("someone not in the address book is still refused", async () => {
  const res = await checkAllowed("254799999999@c.us", {
    allowed: ["contacts"], addressBook: book("254712345678"),
  });
  assert.strictEqual(res.allowed, false);
  assert.strictEqual(res.reason, "not_in_allowlist");
});

test("contacts and hand-typed numbers can be mixed", async () => {
  const opts = { allowed: ["contacts", "254700000001"], addressBook: book("254712345678") };
  assert.strictEqual((await checkAllowed("254700000001@c.us", opts)).allowed, true);
  assert.strictEqual((await checkAllowed("254712345678@c.us", opts)).allowed, true);
});

test("an empty address book means nobody, not everybody", async () => {
  const res = await checkAllowed("254712345678@c.us", { allowed: ["contacts"], addressBook: book() });
  assert.strictEqual(res.allowed, false);
  assert.strictEqual(res.reason, "address_book_empty");
});

test("an unreadable address book fails closed", async () => {
  const res = await checkAllowed("254712345678@c.us", {
    allowed: ["contacts"],
    addressBook: () => { throw new Error("broken json"); },
  });
  assert.strictEqual(res.allowed, false);
  assert.strictEqual(res.reason, "address_book_unreadable");
});

test("the empty allowlist still means everyone, which is still the documented risk", async () => {
  assert.strictEqual((await checkAllowed("254799999999@c.us", { allowed: [] })).allowed, true);
});

// ---- "anyone": answering people who are not saved ----

test("anyone lets through a sender who is in no list at all", async () => {
  const res = await checkAllowed("254799999999@c.us", { allowed: ["anyone"], allowGroups: false });
  assert.strictEqual(res.allowed, true);
  assert.strictEqual(res.reason, "anyone", "said so explicitly, not as an empty-list accident");
});

test("anyone still refuses groups", async () => {
  const res = await checkAllowed("254799999999-149708@g.us", { allowed: ["anyone"], allowGroups: false });
  assert.strictEqual(res.allowed, false);
  assert.strictEqual(res.reason, "groups_off");
});

test("anyone never reads the address book, so a broken one costs nothing", async () => {
  const res = await checkAllowed("254799999999@c.us", {
    allowed: ["anyone", "contacts"],
    addressBook: () => { throw new Error("broken json"); },
  });
  assert.strictEqual(res.allowed, true, "the book no longer decides anything");
});

test("an @lid is still resolved under anyone, so the saved name is found", async () => {
  const res = await checkAllowed("112257593794569@lid", {
    allowed: ["anyone"],
    resolveContact: async () => ({ number: "+254 776 599915" }),
  });
  assert.strictEqual(res.allowed, true);
  assert.strictEqual(res.number, "254776599915",
    "the backend matches the contact by this number to honour the mode set for them");
});

test("an @lid that cannot be resolved is answered rather than dropped under anyone", async () => {
  const res = await checkAllowed("112257593794569@lid", {
    allowed: ["anyone"],
    resolveContact: async () => { throw new Error("offline"); },
  });
  assert.strictEqual(res.allowed, true, "failing to resolve a name must not cost the reply");
});


// ---- what counts as a conversation at all ----

test("a status update is never a conversation, even under anyone", async () => {
  // A friend posting a story used to reach the agent, which wrote a reply the
  // library then tried to post AS a status — and that throws on current
  // WhatsApp builds, taking the send path down with it.
  const res = await checkAllowed("status@broadcast", { allowed: ["anyone"], allowGroups: false });
  assert.strictEqual(res.allowed, false);
  assert.strictEqual(res.reason, "not_a_conversation");
});

test("channels and broadcast lists are refused the same way", async () => {
  for (const id of ["abc@newsletter", "1234@broadcast", "@c.us", "nonsense"]) {
    const res = await checkAllowed(id, { allowed: ["anyone"], allowGroups: true });
    assert.strictEqual(res.allowed, false, `${id} should not be a conversation`);
  }
});

test("real conversations are still allowed under anyone", async () => {
  for (const id of ["254712345678@c.us", "112257593794569@lid"]) {
    assert.strictEqual((await checkAllowed(id, { allowed: ["anyone"] })).allowed, true, id);
  }
});
