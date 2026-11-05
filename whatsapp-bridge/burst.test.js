const assert = require("node:assert/strict");
const { test } = require("node:test");

const { BurstBuffer, configFrom, DEFAULTS } = require("./burst");

// A clock and a timer wheel, so a 30-second ceiling costs no seconds to test.
function harness() {
  const state = { t: 0, next: 1, timers: new Map() };
  return {
    get t() { return state.t; },
    get liveTimers() { return state.timers.size; },
    now: () => state.t,
    setTimer: (fn, ms) => {
      const handle = state.next;
      state.next += 1;
      state.timers.set(handle, { fn, due: state.t + ms });
      return handle;
    },
    clearTimer: (handle) => { state.timers.delete(handle); },
    // Run forward, firing due timers in order — including ones set while
    // firing, which is how a rescheduled burst behaves.
    advance(ms) {
      const target = state.t + ms;
      for (;;) {
        let soonest = null;
        for (const [handle, timer] of state.timers) {
          if (timer.due <= target && (soonest === null || timer.due < soonest.timer.due)) {
            soonest = { handle, timer };
          }
        }
        if (soonest === null) break;
        state.timers.delete(soonest.handle);
        state.t = Math.max(state.t, soonest.timer.due);
        soonest.timer.fn();
      }
      state.t = target;
    },
  };
}

// A buffer wired to the fake clock, collecting the turns it flushes.
function buffered(opts = {}) {
  const clock = harness();
  const turns = [];
  const buffer = new BurstBuffer({
    env: {},
    quietMs: 9000,
    ceilingMs: 30000,
    maxMessages: 10,
    ...opts,
    now: clock.now,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    onFlush: (turn) => { turns.push(turn); },
  });
  return { buffer, turns, clock };
}

const text = (id, body, chatId = "kawira@c.us") => ({ chatId, id, body });

test("two messages in one chat become a single turn carrying both, in order", () => {
  const { buffer, turns, clock } = buffered();
  buffer.add(text("m1", "Very good"));
  clock.advance(4000);                      // she thinks for four seconds
  buffer.add(text("m2", "arie or kim?"));
  assert.strictEqual(turns.length, 0, "nothing goes out while she is still writing");
  clock.advance(9000);
  assert.strictEqual(turns.length, 1, "one burst, one turn");
  assert.strictEqual(turns[0].count, 2);
  assert.strictEqual(turns[0].body, "Very good\narie or kim?");
  assert.strictEqual(turns[0].reason, "quiet");
});

test("a third message arriving during the window extends the wait", () => {
  const { buffer, turns, clock } = buffered();
  buffer.add(text("m1", "one"));
  clock.advance(5000);
  buffer.add(text("m2", "two"));
  clock.advance(5000);                      // m1's window would have closed at 9s
  assert.strictEqual(turns.length, 0, "the second message pushed the flush back");
  buffer.add(text("m3", "three"));
  clock.advance(8000);
  assert.strictEqual(turns.length, 0, "and so did the third");
  clock.advance(1500);
  assert.strictEqual(turns.length, 1);
  assert.strictEqual(turns[0].body, "one\ntwo\nthree");
});

test("the hard ceiling answers somebody who never stops typing", () => {
  const { buffer, turns, clock } = buffered({ maxMessages: 100 });
  for (let i = 0; i < 7; i += 1) {
    buffer.add(text(`m${i}`, `line ${i}`));
    clock.advance(5000);                    // a message every five seconds
  }
  assert.strictEqual(turns.length, 1, "the ceiling, not the quiet window, flushed this");
  assert.strictEqual(turns[0].reason, "ceiling");
  assert.strictEqual(turns[0].flushedAt, turns[0].startedAt + 30000);
});

test("two chats are buffered independently and never merge", () => {
  const { buffer, turns, clock } = buffered();
  buffer.add(text("k1", "How are you?", "kawira@c.us"));
  buffer.add(text("j1", "are you around?", "james@c.us"));
  clock.advance(3000);
  buffer.add(text("k2", "arie or kim?", "kawira@c.us"));
  clock.advance(9000);
  assert.strictEqual(turns.length, 2);
  const byChat = new Map(turns.map((turn) => [turn.chatId, turn]));
  assert.strictEqual(byChat.get("james@c.us").body, "are you around?");
  assert.strictEqual(byChat.get("kawira@c.us").body, "How are you?\narie or kim?");
});

test("the count ceiling flushes immediately, without waiting for quiet", () => {
  const { buffer, turns, clock } = buffered({ maxMessages: 3 });
  buffer.add(text("m1", "a"));
  buffer.add(text("m2", "b"));
  const returned = buffer.add(text("m3", "c"));
  assert.strictEqual(turns.length, 1, "the third message closed the turn on the spot");
  assert.strictEqual(turns[0].reason, "count");
  assert.strictEqual(returned, turns[0], "add() hands back the turn it completed");
  assert.strictEqual(clock.t, 0, "no clock time passed");
});

test("a media-bearing message keeps its media when folded into a turn", () => {
  const { buffer, turns, clock } = buffered();
  const photo = { base64: "ZmFrZQ==", mimetype: "image/jpeg", type: "image" };
  buffer.add(text("m1", "look at this"));
  buffer.add({ chatId: "kawira@c.us", id: "m2", body: "", media: photo });
  clock.advance(9000);
  assert.strictEqual(turns.length, 1);
  assert.deepStrictEqual(turns[0].media, photo);
  assert.strictEqual(turns[0].body, "look at this");
});

test("a second media payload closes the open turn instead of merging", () => {
  const { buffer, turns, clock } = buffered();
  const photo = { base64: "b25l", mimetype: "image/jpeg", type: "image" };
  const voice = { base64: "dHdv", mimetype: "audio/ogg", type: "voice" };
  buffer.add({ chatId: "kawira@c.us", id: "m1", body: "this one", media: photo });
  buffer.add({ chatId: "kawira@c.us", id: "m2", body: "and this", media: voice });
  assert.strictEqual(turns.length, 1, "the photo went out rather than being overwritten");
  assert.deepStrictEqual(turns[0].media, photo);
  assert.strictEqual(turns[0].reason, "media");
  clock.advance(9000);
  assert.strictEqual(turns.length, 2, "the voice note gets its own turn");
  assert.deepStrictEqual(turns[1].media, voice);
});

test("the turn reports the id of the LAST message, which is the one to quote", () => {
  const { buffer, turns, clock } = buffered();
  buffer.add(text("first-id", "Very good"));
  buffer.add(text("last-id", "arie or kim?"));
  clock.advance(9000);
  assert.strictEqual(turns[0].lastId, "last-id");
});

test("the contact's name and number are carried once for the whole burst", () => {
  const { buffer, turns, clock } = buffered();
  buffer.add({ chatId: "kawira@lid", id: "m1", body: "hi", number: "", pushName: "" });
  buffer.add({ chatId: "kawira@lid", id: "m2", body: "you there?", number: "254700111222", pushName: "Kawira" });
  clock.advance(9000);
  assert.strictEqual(turns[0].pushName, "Kawira");
  assert.strictEqual(turns[0].number, "254700111222");
});

test("nothing is retained once a chat has been flushed", () => {
  const { buffer, turns, clock } = buffered();
  buffer.add(text("m1", "hello"));
  assert.strictEqual(buffer.pending("kawira@c.us"), 1);
  clock.advance(9000);
  assert.strictEqual(turns.length, 1);
  assert.strictEqual(buffer.size, 0, "an idle bridge holds no chats");
  assert.strictEqual(buffer.pending("kawira@c.us"), 0);
  assert.strictEqual(clock.liveTimers, 0, "and no timers");
});

test("a message arriving after a flush starts a fresh burst", () => {
  const { buffer, turns, clock } = buffered();
  buffer.add(text("m1", "first question"));
  clock.advance(9000);
  buffer.add(text("m2", "second question"));
  clock.advance(9000);
  assert.strictEqual(turns.length, 2);
  assert.strictEqual(turns[1].body, "second question", "the flushed turn was not reused");
  assert.strictEqual(turns[1].count, 1);
});

test("the chat starts collecting once, and what that hook returns reaches the flush", () => {
  const clock = harness();
  const opened = [];
  const stop = () => {};
  const turns = [];
  const buffer = new BurstBuffer({
    env: {}, quietMs: 9000, ceilingMs: 30000,
    now: clock.now, setTimer: clock.setTimer, clearTimer: clock.clearTimer,
    onOpen: (chatId) => { opened.push(chatId); return stop; },
    onFlush: (turn) => { turns.push(turn); },
  });
  buffer.add(text("m1", "a"));
  buffer.add(text("m2", "b"));
  assert.deepStrictEqual(opened, ["kawira@c.us"], "typing… starts when she starts, once");
  clock.advance(9000);
  return turns[0].open.then((handle) => {
    assert.strictEqual(handle, stop, "the flush path is what clears it");
  });
});

test("a hook that throws costs the typing indicator, not the reply", () => {
  const { turns, clock } = (() => {
    const c = harness();
    const t = [];
    const b = new BurstBuffer({
      env: {}, quietMs: 9000,
      now: c.now, setTimer: c.setTimer, clearTimer: c.clearTimer,
      onOpen: () => { throw new Error("getChat failed"); },
      onFlush: (turn) => { t.push(turn); },
    });
    b.add(text("m1", "hello"));
    return { turns: t, clock: c };
  })();
  clock.advance(9000);
  assert.strictEqual(turns.length, 1);
  assert.strictEqual(turns[0].open, null);
});

test("a flush handler that rejects is reported, not swallowed", () => {
  const clock = harness();
  const errors = [];
  const buffer = new BurstBuffer({
    env: {}, quietMs: 9000,
    now: clock.now, setTimer: clock.setTimer, clearTimer: clock.clearTimer,
    onFlush: () => Promise.reject(new Error("backend down")),
    onError: (err) => errors.push(err.message),
  });
  buffer.add(text("m1", "hello"));
  clock.advance(9000);
  return Promise.resolve().then(() => {
    assert.deepStrictEqual(errors, ["backend down"]);
  });
});

test("the knobs come from the environment, and junk falls back to the default", () => {
  assert.deepStrictEqual(configFrom({ BURST_QUIET_MS: "4000", BURST_MAX_MESSAGES: "4" }), {
    quietMs: 4000, ceilingMs: DEFAULTS.ceilingMs, maxMessages: 4,
  });
  assert.deepStrictEqual(configFrom({ BURST_QUIET_MS: "soon", BURST_CEILING_MS: "-1" }), DEFAULTS);
  assert.deepStrictEqual(configFrom({}), DEFAULTS);
});

test("quiet window zero turns coalescing off: one message, one turn", () => {
  const { buffer, turns, clock } = buffered({ quietMs: 0 });
  buffer.add(text("m1", "a"));
  clock.advance(0);
  buffer.add(text("m2", "b"));
  clock.advance(0);
  assert.strictEqual(turns.length, 2);
  assert.strictEqual(turns[0].body, "a");
  assert.strictEqual(turns[1].body, "b");
});

test("a chat id is required: a message with nowhere to go is a bug, not a silent drop", () => {
  const { buffer } = buffered();
  assert.throws(() => buffer.add({ id: "m1", body: "hi" }), /chatId/);
});
