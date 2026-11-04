// Spacing and ceilings for everything this account sends.
//
// WhatsApp bans accounts for automated messaging, and the tells are not
// subtle: replies that land in the same few hundred milliseconds every time,
// dozens of them at once to people who have never written before, at four in
// the morning. A human's reply times are spread across minutes and hours.
//
// Every send goes through here — the agent's own replies, approved drafts,
// dashboard sends and the self-log — so there is one place that decides how
// fast this account is allowed to talk, and one place to change it.
//
// Free of timers and of the client so the rules can be tested directly.

const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const DEFAULTS = {
  minGapMs: 3000,        // never two sends closer together than this
  jitterMs: 9000,        // ... plus up to this, so the spacing is never uniform
  perMinuteCap: 8,
  perHourCap: 60,
  perDayCap: 300,
  typingMsPerChar: 60,   // a longer message takes longer to write
  typingCeilingMs: 25000,
};

class SendQueue {
  /**
   * @param {object} [opts] overrides for the limits above
   * @param {() => number} [opts.now] injectable clock, for tests
   * @param {() => number} [opts.random] injectable jitter, for tests
   */
  constructor(opts = {}) {
    this.limits = { ...DEFAULTS, ...opts };
    this.now = opts.now || Date.now;
    this.random = opts.random || Math.random;
    this.sentAt = [];      // timestamps, newest last
    this.lastSend = null;   // null, not 0: a clock that starts at zero is still a real time
    this.chain = Promise.resolve();
  }

  _prune(now) {
    const cutoff = now - DAY;
    while (this.sentAt.length && this.sentAt[0] < cutoff) this.sentAt.shift();
  }

  _countSince(now, window) {
    return this.sentAt.filter((t) => t > now - window).length;
  }

  /** Is the account over a ceiling right now? Returns a reason, or "". */
  overCap(now = this.now()) {
    this._prune(now);
    if (this._countSince(now, MINUTE) >= this.limits.perMinuteCap) return "minute";
    if (this._countSince(now, HOUR) >= this.limits.perHourCap) return "hour";
    if (this._countSince(now, DAY) >= this.limits.perDayCap) return "day";
    return "";
  }

  /**
   * How long to wait before the next send, in ms.
   * @param {number} textLength so a long reply takes longer, as writing does
   */
  delayFor(textLength = 0, now = this.now()) {
    const { minGapMs, jitterMs, typingMsPerChar, typingCeilingMs } = this.limits;
    const sinceLast = this.lastSend === null ? Infinity : now - this.lastSend;
    const gap = Math.max(0, minGapMs - sinceLast);
    const typing = Math.min(textLength * typingMsPerChar, typingCeilingMs);
    return Math.round(gap + typing + this.random() * jitterMs);
  }

  /** Record a send. Call this only when one actually went out. */
  record(now = this.now()) {
    this.lastSend = now;
    this.sentAt.push(now);
    this._prune(now);
  }
}

module.exports = { SendQueue, DEFAULTS, MINUTE, HOUR, DAY };
