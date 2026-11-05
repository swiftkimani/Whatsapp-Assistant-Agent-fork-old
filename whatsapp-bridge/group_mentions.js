// Whether somebody in a group was talking to the assistant.
//
// In a group, "was she addressed" is the whole safety question. A group set
// to answer only when she is spoken to is the setting the owner can leave on
// without the account looking like a bot, and this is what decides it. The
// backend (app/groups.py) weighs the group's mode and its ceilings; what
// cannot be decided there is this, because an @mention and a quote live on
// the WhatsApp message rather than in its text, and only the bridge holds it.
//
// Free of whatsapp-web.js and of the client, so the rules can be exercised
// without a WhatsApp connection. Run the tests with: npm test
//
// sameAccount comes from wa_read.js rather than being written again here:
// there is one rule in this bridge for whether two ids name the same person
// — the device suffix, the wid object, the two namespaces — and a second copy
// of it would be a second thing to get wrong. wa_read.js requires nothing
// itself, so this stays a module with no WhatsApp in it.

const { sameAccount } = require("./wa_read");

/** A name is a setting, so a "." or a "*" in one is a character and not a
 *  wildcard. The set escaped is every character with a meaning in a pattern. */
function escapeRegex(text) {
  return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Her names as one whole-word, case-insensitive test, or null when there is
 * nothing to look for.
 *
 * Whole word because a group discussing "Arianna's shop" is talking to her
 * and "ariannaesque" is not. The boundary is written as a lookaround over
 * letters and digits rather than as \b, which is no boundary at all on a name
 * ending in punctuation.
 *
 * A configured name with no letter or digit in it is dropped: escaped or not,
 * a name of pure punctuation would match punctuation in every message and
 * switch the group to answering everything by accident.
 */
function namePattern(names) {
  const parts = (names || [])
    .map((name) => String(name || "").trim())
    .filter((name) => /[\p{L}\p{N}]/u.test(name))
    .map(escapeRegex);
  if (!parts.length) return null;
  return new RegExp(`(?<![\\p{L}\\p{N}_])(?:${parts.join("|")})(?![\\p{L}\\p{N}_])`, "iu");
}

/**
 * Her name and the short forms she also answers to, read the way app/llm.py
 * and app/assistant.py read them, so one setting covers both sides.
 *
 * ASSISTANT_NICKNAMES set to nothing means no nicknames, which is why an
 * absent value and an empty one are told apart here.
 */
function namesFrom(env = {}) {
  const name = String(env.ASSISTANT_NAME === undefined ? "Arianna" : env.ASSISTANT_NAME).trim();
  const nicknames = String(env.ASSISTANT_NICKNAMES === undefined ? "Arie" : env.ASSISTANT_NICKNAMES);
  return [name, ...nicknames.split(",").map((part) => part.trim())].filter(Boolean);
}

/**
 * Every id the owner is known by. WhatsApp names the same account by phone
 * number in some places and by privacy id in others, and the digits of the
 * two are unrelated — a lid is not a number with a different suffix — so
 * sameAccount() refuses to compare across the two namespaces, and rightly.
 * The only honest way to recognise her in both is to be told both.
 */
function ownerIds(me) {
  return (Array.isArray(me) ? me : [me]).filter(Boolean);
}

/** Whether one of the owner's ids is @mentioned in this message. */
function mentionsOwner(msg, me) {
  const mentioned = (msg && Array.isArray(msg.mentionedIds)) ? msg.mentionedIds : [];
  const mine = ownerIds(me);
  // mentionedIds arrives as serialized strings or as the wid objects they
  // were built from, depending on how the message reached us; sameAccount
  // reads either, which is why the comparison goes through it.
  return mentioned.some((id) => mine.some((own) => sameAccount(id, own)));
}

/**
 * Whether this message is a reply to one of the owner's own messages.
 *
 * Read off the raw serialized message, where the quote already is, in the
 * same order of precedence wa_read.js's quotedOf() uses: the quoted id's
 * fromMe is the authority, a flag beside it is second, and with neither
 * stated the only reading left is whether the author is the owner. quotedOf()
 * is not exported, and reproducing all of it to read one field would be worse
 * than reading that field — but the precedence is its, deliberately, so the
 * two cannot disagree about whose message was quoted.
 *
 * Every field is optional: this is WhatsApp's serialization rather than a
 * shape the library promises, and a missing key means "not a reply to her",
 * never a thrown error in the middle of answering.
 */
function repliesToOwner(msg, me) {
  const data = (msg && msg._data) || {};
  // msg.hasQuotedMsg is the library's own test for this exact field, so
  // asking it first would only be the same question twice.
  const quoted = data.quotedMsg;
  if (!quoted || typeof quoted !== "object") return false;
  const quotedId = (quoted.id && typeof quoted.id === "object") ? quoted.id : {};
  const stated = [quotedId.fromMe, quoted.fromMe].find((flag) => typeof flag === "boolean");
  if (typeof stated === "boolean") return stated;
  const who = data.quotedParticipant || quoted.participant || quoted.author || "";
  return ownerIds(me).some((own) => sameAccount(who, own));
}

/**
 * Was the assistant addressed in this message, and how.
 *
 * @param {object} msg  a whatsapp-web.js message, or anything with the same
 *        shape: {body, mentionedIds, _data: {quotedMsg, quotedParticipant}}
 * @param {object} opts
 * @param {string|string[]} opts.me  the owner's own id, or every id she is
 *        known by — see ownerIds() for why both forms are worth passing
 * @param {string[]} [opts.names]  overrides the names from the environment
 * @param {object} [opts.env]  where the names are read from
 * @returns {{addressed: boolean, how: "mention"|"name"|"reply"|""}}
 */
function addressedIn(msg, { me, names, env } = {}) {
  // Strongest signal first: an @mention is unambiguous, a quote names the
  // message being answered, and her name in the words is the weakest of the
  // three because people talk about her as well as to her.
  if (mentionsOwner(msg, me)) return { addressed: true, how: "mention" };
  if (repliesToOwner(msg, me)) return { addressed: true, how: "reply" };
  const pattern = namePattern(names || namesFrom(env || process.env));
  if (pattern && pattern.test(String((msg && msg.body) || ""))) {
    return { addressed: true, how: "name" };
  }
  return { addressed: false, how: "" };
}

module.exports = { addressedIn, mentionsOwner, repliesToOwner, namePattern, namesFrom, ownerIds };
