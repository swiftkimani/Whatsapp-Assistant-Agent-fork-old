const assert = require("node:assert/strict");
const { test } = require("node:test");

const { SendQueue } = require("./send_queue");

const at = (t) => { const clock = { t }; return clock; };

test("two sends are never closer together than the minimum gap", () => {
  const clock = at(0);
  const q = new SendQueue({ now: () => clock.t, random: () => 0 });
  q.record();
  clock.t += 500;                       // the model came back fast
  assert.ok(q.delayFor(0) >= 2500, "should still wait out the rest of the gap");
});

test("a longer reply takes longer to send, the way writing does", () => {
  const q = new SendQueue({ now: () => 0, random: () => 0 });
  assert.ok(q.delayFor(200) > q.delayFor(5));
});

test("the wait is never the same twice", () => {
  // Uniform timing is the tell. Jitter is the point of this class.
  const values = [0.1, 0.9].map((r) => new SendQueue({ now: () => 0, random: () => r }).delayFor(10));
  assert.notStrictEqual(values[0], values[1]);
});

test("a burst is stopped by the per-minute ceiling", () => {
  const clock = at(0);
  const q = new SendQueue({ now: () => clock.t, random: () => 0, perMinuteCap: 5 });
  for (let i = 0; i < 5; i++) { q.record(); clock.t += 10; }
  assert.strictEqual(q.overCap(), "minute");
});

test("the ceiling lifts once the window has passed", () => {
  const clock = at(0);
  const q = new SendQueue({ now: () => clock.t, random: () => 0, perMinuteCap: 2 });
  q.record(); q.record();
  assert.strictEqual(q.overCap(), "minute");
  clock.t += 61_000;
  assert.strictEqual(q.overCap(), "");
});

test("the daily ceiling outlasts the hourly one", () => {
  const clock = at(0);
  const q = new SendQueue({ now: () => clock.t, random: () => 0,
                            perMinuteCap: 1e6, perHourCap: 1e6, perDayCap: 3 });
  for (let i = 0; i < 3; i++) { q.record(); clock.t += 60 * 60 * 1000; }
  assert.strictEqual(q.overCap(), "day", "three sends spread over three hours still hit a daily cap");
});

test("sends older than a day stop counting", () => {
  const clock = at(0);
  const q = new SendQueue({ now: () => clock.t, random: () => 0, perDayCap: 2 });
  q.record(); q.record();
  clock.t += 25 * 60 * 60 * 1000;
  assert.strictEqual(q.overCap(), "");
});
