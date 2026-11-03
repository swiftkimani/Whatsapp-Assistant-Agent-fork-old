const assert = require("node:assert/strict");
const { test } = require("node:test");

const { qrToSvg, QUIET_ZONE } = require("./qr_svg");

// What WhatsApp actually hands over: four comma-separated base64 segments.
const REAL = `2@${"A".repeat(60)},${"B".repeat(43)}=,${"C".repeat(43)}=,${"D".repeat(43)}=`;

test("a real-length WhatsApp payload encodes", () => {
  const svg = qrToSvg(REAL);
  assert.match(svg, /^<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg"/);
  assert.match(svg, /<path d="M/, "there should be dark modules to draw");
});

test("the quiet zone is included, because a QR without margin will not scan", () => {
  const span = Number(qrToSvg(REAL).match(/viewBox="0 0 (\d+)/)[1]);
  // The matrix itself is odd-sized; the span is that plus a margin each side.
  assert.ok(span > QUIET_ZONE * 2, "span should exceed the margins alone");
  assert.strictEqual((span - QUIET_ZONE * 2) % 2, 1, "a QR matrix is odd-sized");
});

test("it scales without changing what is encoded", () => {
  const small = qrToSvg(REAL, { size: 120 });
  const large = qrToSvg(REAL, { size: 480 });
  assert.match(small, /width="120"/);
  assert.match(large, /width="480"/);
  const path = (s) => s.match(/<path d="([^"]+)"/)[1];
  assert.strictEqual(path(small), path(large), "only the pixel size differs");
});

test("the same code always draws the same picture", () => {
  assert.strictEqual(qrToSvg(REAL), qrToSvg(REAL));
});

test("a longer code needs a bigger matrix", () => {
  const span = (s) => Number(qrToSvg(s).match(/viewBox="0 0 (\d+)/)[1]);
  assert.ok(span(REAL + "x".repeat(400)) > span(REAL));
});

test("nothing to encode is an error rather than a blank square", () => {
  // A blank square would look like a QR that simply will not scan.
  assert.throws(() => qrToSvg(""), /nothing to encode/);
  assert.throws(() => qrToSvg(null), /nothing to encode/);
});

test("the markup carries no raw payload for a shoulder-surfer to read", () => {
  assert.ok(!qrToSvg(REAL).includes(REAL), "the code itself must not appear as text");
});
