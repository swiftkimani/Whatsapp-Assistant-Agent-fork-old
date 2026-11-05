// One turn per burst, not one turn per message.
//
// People do not send one message and wait. They send "Very good", then
// "arie or kim?" eight seconds later, then a photo. Each of those used to fire
// its own webhook the instant it arrived, so n messages became n concurrent
// agent runs, all reading the same stored history: the second run did not know
// the first message existed, the runs raced to send, and the one that won
// answered the OLDEST message. In a real transcript the live question —
// "arie or kim?" — was never answered at all.
//
// So collect per chat, wait for the person to stop typing, and hand back one
// turn. This is also the single biggest thing this bridge does for the safety
// of the account: three replies inside one second is what automation looks
// like from WhatsApp's side, and losing the account is the worst outcome in
// the system. One considered reply after a human-length pause is what a person
// looks like. Fewer sends, better spaced — a safety property, not only a
// correctness one.
//
// Free of whatsapp-web.js, axios and real timers, so the rules can be
// exercised directly. Run the tests with: npm test

const DEFAULTS = {
  // Long enough to cover the gap between two thoughts typed on a phone, short
  // enough that nobody wonders whether the message arrived.
  quietMs: 9000,
  // Somebody who never stops typing still deserves an answer. Measured from
  // the first message of the burst, so the wait has a ceiling either way.
  ceilingMs: 30000,
  // A flood must not grow unbounded in memory, and a turn nobody can read is
  // no use to the model. Hitting this flushes at once.
  maxMessages: 10,
};

/** A number from the environment, or the default when it is absent or junk. */
function bounded(raw, fallback, min) {
  if (raw === undefined || raw === null || String(raw).trim() === "") return fallback;
  const value = Number(raw);
  return Number.isFinite(value) && value >= min ? value : fallback;
}

/**
 * Read the knobs from an environment. Separate from the class so the defaults
 * can be tested without touching process.env.
 *
 * BURST_QUIET_MS    how long a chat must be silent before its turn goes out
 * BURST_CEILING_MS  the hard ceiling on that wait, from the first message
 * BURST_MAX_MESSAGES  flush at once once a burst reaches this many messages
 *
 * BURST_QUIET_MS=0 is the way to turn coalescing off: every message is then
 * its own turn, as it was before this module existed.
 */
function configFrom(env = {}) {
  return {
    quietMs: bounded(env.BURST_QUIET_MS, DEFAULTS.quietMs, 0),
    ceilingMs: bounded(env.BURST_CEILING_MS, DEFAULTS.ceilingMs, 0),
    maxMessages: Math.floor(bounded(env.BURST_MAX_MESSAGES, DEFAULTS.maxMessages, 1)),
  };
}

/** The last message that has something in `key`, or "". */
function lastWith(messages, key) {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const value = messages[i] && messages[i][key];
    if (value) return String(value);
  }
  return "";
}

class BurstBuffer {
  /**
   * @param {object} [opts]
   * @param {number} [opts.quietMs]      overrides the environment
   * @param {number} [opts.ceilingMs]
   * @param {number} [opts.maxMessages]
   * @param {object} [opts.env]          where the defaults are read from
   * @param {() => number} [opts.now]    injectable clock, for tests
   * @param {(fn: Function, ms: number) => any} [opts.setTimer] injectable timer
   * @param {(handle: any) => void} [opts.clearTimer]
   * @param {(turn: object) => any} [opts.onFlush] called with the finished turn
   * @param {(chatId: string, first: object) => any} [opts.onOpen] called once
   *        when a chat starts collecting. Whatever it returns is carried to the
   *        flush as `turn.open`, untouched — index.js uses it to hold a
   *        "typing…" session open for as long as the person is writing.
   * @param {(err: Error) => void} [opts.onError]
   */
  constructor(opts = {}) {
    const fromEnv = configFrom(opts.env || process.env);
    this.limits = {
      quietMs: opts.quietMs === undefined ? fromEnv.quietMs : opts.quietMs,
      ceilingMs: opts.ceilingMs === undefined ? fromEnv.ceilingMs : opts.ceilingMs,
      maxMessages: opts.maxMessages === undefined ? fromEnv.maxMessages : opts.maxMessages,
    };
    this.now = opts.now || Date.now;
    this.setTimer = opts.setTimer || ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = opts.clearTimer || ((handle) => clearTimeout(handle));
    this.onFlush = opts.onFlush || null;
    this.onOpen = opts.onOpen || null;
    this.onError = opts.onError
      || ((err) => console.error("Error answering a burst:", (err && err.stack) || err));
    this.chats = new Map();
  }

  /** How many chats are mid-burst. An idle bridge holds nothing. */
  get size() {
    return this.chats.size;
  }

  /** How many messages are waiting for one chat. */
  pending(chatId) {
    const entry = this.chats.get(chatId);
    return entry ? entry.messages.length : 0;
  }

  /**
   * Take one incoming message.
   *
   * @param {object} item
   * @param {string} item.chatId  which conversation this belongs to
   * @param {string} item.id      the message id, for quoting
   * @param {string} [item.body]
   * @param {object|null} [item.media]  {base64, mimetype, type}, already
   *        downloaded; opaque here beyond "this message carries media"
   * @param {string} [item.number]    the resolved phone number
   * @param {string} [item.pushName]  the contact's display name
   * @returns {object|null} the turn, when this message completed one on the
   *        spot (the count ceiling, or a second media payload); otherwise null
   *        and the turn follows on a timer.
   */
  add(item) {
    const chatId = String((item && item.chatId) || "");
    if (!chatId) throw new Error("burst: a message needs a chatId");

    const now = this.now();
    let entry = this.chats.get(chatId);

    // A turn carries at most ONE media payload. Merging several would mean
    // deciding which photo the backend's single media field holds, and the
    // honest answer is that it cannot hold two — so the second one closes the
    // turn that is open and starts its own. Text still joins a media message
    // (a photo and the "what do you think?" that follows it are one question,
    // and answering them separately is the very bug this module exists to
    // fix), and no media is ever dropped on the floor.
    let closed = null;
    if (entry && item && item.media && entry.messages.some((m) => m.media)) {
      closed = this.flush(chatId, "media");
      entry = undefined;
    }

    if (!entry) entry = this._open(chatId, item, now);
    entry.messages.push(item);

    if (entry.messages.length >= this.limits.maxMessages) {
      return this.flush(chatId, "count");
    }
    this._schedule(entry, now);
    return closed;
  }

  /**
   * Hand back one chat's collected messages as a single turn. Safe to call for
   * a chat that is not collecting: it returns null.
   */
  flush(chatId, reason = "quiet") {
    const entry = this.chats.get(chatId);
    if (!entry) return null;

    // Dropped from the map, and its timer cleared, BEFORE the hook runs: a
    // message arriving while the agent is thinking must start a fresh burst
    // rather than join one that has already gone out, and an onFlush that
    // throws must not leave an entry behind for an idle chat.
    this.chats.delete(chatId);
    if (entry.timer !== null) this.clearTimer(entry.timer);

    const turn = this._turn(entry, reason);
    if (this.onFlush) {
      try {
        const settled = this.onFlush(turn);
        if (settled && typeof settled.then === "function") {
          settled.catch((err) => this.onError(err));
        }
      } catch (err) {
        this.onError(err);
      }
    }
    return turn;
  }

  _open(chatId, item, now) {
    const entry = { chatId, messages: [], startedAt: now, timer: null, open: null };
    if (this.onOpen) {
      // Never allowed to reject. This hook is cosmetic ("typing…") and a
      // failure there must not cost a reply, which is the property index.js
      // already had and must keep.
      try {
        entry.open = Promise.resolve(this.onOpen(chatId, item)).catch(() => null);
      } catch {
        entry.open = null;
      }
    }
    this.chats.set(chatId, entry);
    return entry;
  }

  _schedule(entry, now) {
    if (entry.timer !== null) this.clearTimer(entry.timer);
    const quietDue = now + this.limits.quietMs;
    const ceilingDue = entry.startedAt + this.limits.ceilingMs;
    // Whichever comes first: the person stopping, or the ceiling on waiting.
    const quietWins = quietDue <= ceilingDue;
    const due = quietWins ? quietDue : ceilingDue;
    entry.reason = quietWins ? "quiet" : "ceiling";
    entry.timer = this.setTimer(
      () => this.flush(entry.chatId, entry.reason),
      Math.max(0, due - now),
    );
  }

  _turn(entry, reason) {
    const messages = entry.messages;
    // The one media-bearing message, if there is one. add() guarantees at most
    // one per turn.
    const carrier = messages.find((m) => m && m.media) || null;
    return {
      chatId: entry.chatId,
      messages,
      count: messages.length,
      // Joined with newlines, because that is how the thread reads on her
      // phone: two lines, one after the other. The model should see what a
      // person would see, not a run-on sentence it has to guess the seams of.
      body: messages.map((m) => String((m && m.body) || "").trim()).filter(Boolean).join("\n"),
      // The LAST message is the one being answered, so it is the one to quote.
      // Quoting the first is how "arie or kim?" came to be answered with a
      // reply to "Very good".
      lastId: lastWith(messages, "id"),
      media: carrier ? carrier.media : null,
      number: lastWith(messages, "number"),
      // Sent once for the whole burst, not once per message.
      pushName: lastWith(messages, "pushName"),
      reason,
      startedAt: entry.startedAt,
      flushedAt: this.now(),
      open: entry.open,
    };
  }
}

module.exports = { BurstBuffer, DEFAULTS, configFrom };
