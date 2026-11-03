// Who is allowed to reach the assistant. This is the security boundary of the
// whole system: everything that gets past it is transcribed, stored, and sent
// to a model, so it is worth being able to test in isolation.
//
// Kept free of module-level state so the rules can be exercised directly;
// index.js reads the environment and passes the result in.

/**
 * @param {string} chatId        e.g. "2547...@c.us", "...@lid", "...@g.us"
 * @param {object} opts
 * @param {string[]} opts.allowed  allowlisted numbers, and/or the word
 *        "contacts" meaning everyone in the address book, or the word
 *        "anyone" meaning every direct chat, saved or not
 * @param {boolean} opts.allowGroups
 * @param {() => string[]} [opts.addressBook] numbers from contacts.json,
 *        only called when "contacts" is listed
 * @param {() => Promise<{number?: string}|null>} [opts.resolveContact]
 *        resolves an @lid privacy id to a real contact; only called for @lid
 * @returns {Promise<{allowed: boolean, number: string, kind: string, reason: string}>}
 */
async function checkAllowed(chatId, { allowed, allowGroups, resolveContact, addressBook } = {}) {
  const from = String(chatId || "");
  const entries = (allowed || []).map((n) => String(n).trim()).filter(Boolean);

  // "contacts" stands for everyone in the address book. Hand-typing numbers
  // does not survive contact sync, and the alternative people reach for —
  // leaving this empty — answers complete strangers.
  const usesAddressBook = entries.some((e) => e.toLowerCase() === "contacts");
  // "anyone" answers people who are not in the address book at all. Spelled
  // out rather than left to the empty list, which means the same thing by
  // accident: forgetting to set this must not be what opens the door.
  const usesAnyone = entries.some((e) => e.toLowerCase() === "anyone");
  const list = entries
    .filter((e) => !["contacts", "anyone"].includes(e.toLowerCase()))
    .map((n) => n.replace(/\D/g, ""))
    .filter(Boolean);
  // "anyone" already covers the address book, so it is not read at all: a
  // broken contacts.json would otherwise fail closed over a list that no
  // longer decides anything.
  if (usesAddressBook && !usesAnyone && typeof addressBook === "function") {
    try {
      for (const number of addressBook()) {
        const digits = String(number).replace(/\D/g, "");
        if (digits) list.push(digits);
      }
    } catch {
      // An unreadable contacts.json must not quietly widen the allowlist.
      return { allowed: false, number: "", kind: "unknown", reason: "address_book_unreadable" };
    }
  }
  const kind = from.endsWith("@g.us") ? "group" : from.endsWith("@lid") ? "lid" : "direct";

  // Drop the device suffix BEFORE stripping punctuation. On multi-device
  // accounts WhatsApp sends "2547...:12@c.us", and removing non-digits first
  // would splice the "12" onto the number and silently never match.
  let number = from.split("@")[0].split(":")[0].replace(/\D/g, "");

  if (kind === "group" && !allowGroups) {
    return { allowed: false, number, kind, reason: "groups_off" };
  }

  if (kind === "lid" && typeof resolveContact === "function") {
    try {
      const contact = await resolveContact();
      if (contact && contact.number) number = String(contact.number).replace(/\D/g, "");
    } catch {
      // Still worth resolving under "anyone": the number is what lets the
      // backend recognise a saved contact and honour the mode set for them.
      // But it no longer decides whether the message is allowed, so failing
      // to resolve one costs that name, not the reply.
      if (!usesAnyone) return { allowed: false, number, kind, reason: "lid_unresolved" };
    }
  }

  if (usesAnyone) return { allowed: true, number, kind, reason: "anyone" };

  if (entries.length === 0) return { allowed: true, number, kind, reason: "allowlist_empty" };
  if (list.length === 0) {
    // "contacts" was asked for and the address book is empty: nobody, rather
    // than everybody. Failing open here would be the worst possible default.
    return { allowed: false, number, kind, reason: "address_book_empty" };
  }

  // Compare on the last 9 digits as well as the whole number. The same person
  // reaches you as 254712345678, 0712345678 or +254 712 345 678 depending on
  // how the sender's phone stored it, and all three are the same human.
  const tail = (n) => n.slice(-9);
  const hit = list.some((n) => n === number || (number.length >= 9 && tail(n) === tail(number)));
  return { allowed: hit, number, kind, reason: hit ? "allowed" : "not_in_allowlist" };
}

module.exports = { checkAllowed };
