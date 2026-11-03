// Turns a WhatsApp link code into a QR you can actually scan.
//
// The bridge used to print the QR as terminal art, which is unreadable in a
// log viewer and rotates about every twenty seconds. The same string drawn as
// an SVG can be shown on the dashboard and scanned off the screen, which is
// the only way to link a server that cannot run a visible browser.
//
// No new dependency: qrcode-terminal already carries a QR encoder, and this
// reads the module matrix out of it rather than its ASCII output.

const QRCode = require("qrcode-terminal/vendor/QRCode");
const QRErrorCorrectLevel = require("qrcode-terminal/vendor/QRCode/QRErrorCorrectLevel");

const QUIET_ZONE = 4;   // modules of margin the spec requires for a reliable read

/**
 * @param {string} text the QR payload, as WhatsApp gives it
 * @param {object} [opts]
 * @param {number} [opts.size] pixel width/height of the finished square
 * @returns {string} a standalone SVG document
 */
function qrToSvg(text, { size = 320 } = {}) {
  const payload = String(text || "");
  if (!payload) throw new Error("nothing to encode");

  // -1 lets the encoder pick the smallest version that fits. Level L is what
  // WhatsApp Web itself uses, and a lower level keeps the modules large enough
  // to survive being photographed off a screen.
  const qr = new QRCode(-1, QRErrorCorrectLevel.L);
  qr.addData(payload);
  qr.make();

  const count = qr.getModuleCount();
  const span = count + QUIET_ZONE * 2;

  // One path for every dark module, as a single d attribute: far smaller than
  // a rect per module, and it scales to any size without resampling.
  let d = "";
  for (let row = 0; row < count; row++) {
    for (let col = 0; col < count; col++) {
      if (qr.isDark(row, col)) d += `M${col + QUIET_ZONE} ${row + QUIET_ZONE}h1v1h-1z`;
    }
  }

  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" ` +
    `viewBox="0 0 ${span} ${span}" shape-rendering="crispEdges" role="img" ` +
    `aria-label="WhatsApp linking code">` +
    `<rect width="${span}" height="${span}" fill="#fff"/>` +
    `<path d="${d}" fill="#000"/></svg>`
  );
}

module.exports = { qrToSvg, QUIET_ZONE };
