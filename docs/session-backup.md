# Backing up the WhatsApp session and the database

_Written 2026-11-05. The script is `scripts/wa_backup.sh`. The restore was
tested locally; what was and was not tested is at the bottom, and it matters._

## What is at stake

The `wa-session` volume holds `.wwebjs_auth`, the linked WhatsApp session.

**It is a credential.** Anyone who holds a copy can read this account's
messages and send messages as it. Not "could impersonate the owner with
effort" — can simply be the account.

**It is also the only copy of something this server cannot recreate.** WhatsApp
now demands a passkey ceremony to link a new companion device, and a passkey
needs a real browser and a real authenticator. A headless container has
neither, so it cannot link itself. The session that runs on the server had to
be created on the owner's laptop in a visible browser and carried in.
`whatsapp-bridge/index.js` says the same thing at the top of its puppeteer
config, and its `KeepProfileLocalAuth` class exists for one step of this same
risk: whatsapp-web.js deletes the entire profile when WhatsApp reports the
device logged out, so that class renames it instead of letting
`fs.rm(userDataDir, {recursive: true, force: true})` run.

Lose the volume and there is no recovery path from the server. The account's
link is simply gone until someone sits down at a laptop with the phone.

So: two rules, and they are not negotiable by convenience.

1. A copy goes nowhere public. Not a git repository, not an S3 bucket, not a
   chat, not an email, not a shared drive. `scripts/wa_backup.sh` writes mode
   700 directories and mode 600 files, and it **refuses to write into a git
   work tree**. It uploads nothing, anywhere, and no flag makes it.
2. A copy belongs somewhere you control and can find: an encrypted disk, a
   password manager's file attachment, a drive that lives at home. Think of
   where you would keep a spare key to the house.

## What gets backed up

| | Why |
|---|---|
| `.wwebjs_auth` (the whole Chromium profile) | The session. Cannot be recreated on the server. |
| `data/assistant.db` | The held queue, the message log, the notes, the per-contact modes, the settings. Recreatable only as an empty database. |

Not backed up: the `model-cache` volume (Whisper re-downloads its weights from
Hugging Face), and the images (rebuilt from the repo).

Nothing in the profile is worth leaving out, and that is measured rather than
assumed. The live profile is 415 MB, and 236 MB of it is
`Default/IndexedDB` — which is where WhatsApp Web keeps both the session keys
and the chat store. The browser caches that *could* be pruned (`Cache`,
`Code Cache`, `Service Worker`) come to 97 MB, under a quarter, and pruning
them would buy a backup that nothing here can prove still links. Verifying a
pruned profile means starting a second WhatsApp client against a copy of the
live session, and two clients on one session is a plausible way to get the
device logged out — the exact disaster this file exists to survive. So: the
whole profile, every time.

```bash
# the profile, and its archive
$ du -sm whatsapp-bridge/.wwebjs_auth
415	whatsapp-bridge/.wwebjs_auth
$ tar -czf - .wwebjs_auth | wc -c
257863680          # 245 MiB, 12 s on the laptop
```

245 MiB per backup. `--keep` bounds how many are kept; the default is 3, so
about 750 MiB. Check the host has it before scheduling anything.

## The one thing that cannot be fudged: cold or hot

**The session cannot be safely copied while the bridge is running.** It is a
live Chromium profile, and the credential lives in a LevelDB store with its own
log, manifest and lock files. A copy taken mid-write can restore into a profile
Chromium will not open, and there is no way to discover that except by trying
it on the day it matters.

So `scripts/wa_backup.sh backup` stops the bridge first, copies, and starts it
again. It does that itself, and it installs a trap so that a failure in the
middle still starts the container.

**What that costs the owner:** the archive itself took 12 s on the laptop, and
will take longer on the VPS, which has fewer and slower cores and a 2-CPU cap
on that container. Add the bridge's own restart — Chromium launching, WhatsApp
Web loading and re-syncing from the saved session. Budget **two to three
minutes** of the assistant being offline. Messages sent during the gap are
delivered when it reconnects, the way they are to a phone that was switched
off, so the cost is a delay in answering rather than a lost message — that is
how the design is meant to behave, and nothing here measured it.

`--hot` skips the stop. It exists because a hot copy beats no copy when the
alternative is losing the volume, and for no other reason. It prints a warning,
it records `consistency: hot` in the manifest, `verify` repeats the warning
when it reads that line, and GNU tar's "file changed as we read it" exit
status is reported rather than swallowed. A hot copy is a maybe. Replace it
with a cold one as soon as you can take the three minutes.

## The database, and why not `cp`

`app/db.py` puts every connection in WAL mode. That means the committed state
of the database at any moment is spread across three files: `assistant.db`,
`assistant.db-wal` and `assistant.db-shm`. Copy the first alone and you get a
database missing everything committed since the last checkpoint. Copy all three
while a writer is mid-commit and you get a torn set. `cp` is a coin toss, and
it is a coin toss that only shows up when you restore.

The script uses **`VACUUM INTO`**:

```sql
VACUUM INTO '/path/to/backup/assistant.db'
```

SQLite reads a consistent snapshot inside a read transaction — which is
precisely what WAL lets it do without blocking the writer — and writes one
self-contained file with no WAL to carry alongside it. One statement, available
from the `sqlite3` CLI and from Python's stdlib, so it needs nothing installed
that is not already in the backend image.

The backup API (`Connection.backup()`, or `.backup` in the CLI) is equally
correct and has one advantage: it restarts itself if a write lands mid-copy.
`VACUUM INTO` was chosen because it is a single statement, it defragments on
the way out, and this database is small enough (81 KB today) that the
restart-on-write case is theoretical. Either is right; `cp` is not.

## Taking a backup

On the host, where the volumes are:

```bash
cd /path/to/the/repo
./scripts/wa_backup.sh backup --out /root/wa-backups
./scripts/wa_backup.sh verify /root/wa-backups/<stamp>
```

It finds both containers by what they have mounted — `/app/whatsapp-bridge/
.wwebjs_auth` and `/app/data` — so Coolify's volume-name prefixes do not have
to be known or typed. `tar`, `gzip` and `python3` all come from the services'
own images, so the host needs nothing but Docker.

On the laptop, which is where the live session actually is today (see
`docs/CONTEXT.md`: the server is unlinked and cannot re-link itself, so the
laptop's copy is the only working one — **this is the one to back up first**):

```bash
pkill -f "node index.js"        # or stop it however you started it
./scripts/wa_backup.sh backup --cold \
  --session-dir whatsapp-bridge/.wwebjs_auth \
  --db data/assistant.db \
  --out ~/wa-backups
./scripts/wa_backup.sh verify ~/wa-backups/<stamp>
cd whatsapp-bridge && node index.js &
```

In path mode the script cannot tell whether the bridge is running, so it
insists on `--cold` or `--hot` rather than guessing.

Each backup directory holds `session.tar.gz`, `assistant.db` and a
`manifest.txt` with the SHA-256 of both, the source, whether it was cold or
hot, and the two commands to verify and restore it. The manifest also states,
in the file, that the directory is a credential.

## Verifying

```bash
./scripts/wa_backup.sh verify /root/wa-backups/20261105T113157Z
```

It checks the SHA-256 of both files against the manifest, runs
`PRAGMA integrity_check` on the database copy and counts its tables, tests the
gzip stream, and confirms the archive contains the four paths a restored
profile is useless without: `Default/IndexedDB`, `Default/Local Storage`,
`Default/Preferences` and `Local State`. It exits non-zero if any of that
fails, and `restore` runs it first and refuses to go on.

What `verify` cannot tell you is whether Chromium will open the profile. Only a
restore can.

## Restoring

`restore` deletes nothing. Everything it displaces is renamed with
`.before-restore-<stamp>`, following the precedent `KeepProfileLocalAuth`
already sets in `index.js`: park it, do not destroy it.

```bash
./scripts/wa_backup.sh restore /root/wa-backups/20261105T113157Z
```

In order, it: verifies the backup and refuses if anything is wrong; stops the
bridge; moves the current profile aside into `.before-restore-<stamp>/`;
unpacks the archive into the volume and `chown`s it to uid 10001, which is what
both images run as; stops the backend; parks `assistant.db` **and its `-wal`
and `-shm`**; copies the snapshot in; starts the backend; starts the bridge.

The `-wal` and `-shm` are not housekeeping. Left beside a restored database
they are a write-ahead log belonging to a *different* file, and SQLite would
either replay them over the restored pages or refuse the database outright.

Then check, in this order:

1. The bridge log says it is ready, and does **not** show a QR code or a
   pairing code. A request to link means the restore did not take.
2. Send yourself a message from another phone and watch it arrive.
3. The dashboard lists chats.

If it asks to be linked, nothing is lost: the parked
`.before-restore-<stamp>` copies are still in the volume, and the laptop route
in `docs/CONTEXT.md` ("Why the server cannot re-link, and where the live
session actually is") is still the way back.

To rehearse a restore without touching anything live — which is the point of
having a restore procedure at all — point it at scratch paths:

```bash
mkdir -p /tmp/rehearsal
./scripts/wa_backup.sh restore /root/wa-backups/<stamp> \
  --session-dir /tmp/rehearsal/.wwebjs_auth \
  --db /tmp/rehearsal/assistant.db
diff -r whatsapp-bridge/.wwebjs_auth /tmp/rehearsal/.wwebjs_auth
```

## What was actually tested, and what was not

Tested, on this laptop, end to end:

- **A restore produced a byte-identical tree.** Backup from a stand-in profile
  with the real layout, then restore into an empty directory, then
  `diff -r` against the original: no differences. The database restored
  identical to its snapshot (`cmp`), opened, and reported the same row counts
  as the source (`held=0 messages=3 log=1`).
- **A restore over a non-empty target parks what was there.** A junk file
  planted in the old profile was gone from the live path afterwards and present
  in `.before-restore-<stamp>`; a planted stale `assistant.db-wal` and
  `-shm` were parked, not left beside the restored database.
- **A damaged backup is refused.** One byte flipped in `session.tar.gz`:
  `restore` reported `CHECKSUM MISMATCH`, `GZIP IS CORRUPT` and the four
  missing paths, exited 1, and left the target untouched.
- **`VACUUM INTO` against the real `data/assistant.db`**, which passed
  `PRAGMA integrity_check` with 13 tables.
- **It refuses to write a credential into a git work tree** — both when the
  destination directory already exists and when it does not. The second case
  is not theoretical: the first version of this check asked `git -C` about a
  directory that had not been created yet, `git` failed, and a failing check
  read as a pass. It wrote a session into the repository. That is now asked of
  the nearest directory that exists, and both cases are tested.
- `--keep 2` pruned three backups to the newest two, and only directories with
  a complete manifest are counted or pruned.

**Not tested, and worth knowing:**

- **No Docker path was run.** There is no Docker daemon on this laptop, so
  every `docker run`, `docker stop`/`start` and volume-discovery path in the
  script is code-reviewed, not executed. The first real run on the host should
  be a `backup` followed by a `verify`, which touches nothing. Run a
  `restore --session-dir /tmp/...` rehearsal before ever running a live one.
- **No restored profile was opened by Chromium.** Proving that means starting a
  WhatsApp client against a copy of the live session, and two clients on one
  session risks the logout this whole file is about. The archive is a
  byte-identical copy of a cold profile, which is the strongest thing that can
  be said without taking that risk.
- **The three-minute downtime figure is an estimate.** The 12 s archive is
  measured; the bridge's restart and WhatsApp Web re-sync are not.

## Does Coolify's own Backups feature cover this?

Almost certainly not, and this should be checked rather than believed, because
if it did cover it then saying so would be worth more than the script.

Coolify's **Backups** feature is for databases *it* manages — a Postgres,
MySQL, MariaDB or MongoDB resource — where it runs a scheduled dump into local
storage or an S3 bucket. This app is a Docker Compose resource with three named
volumes and no managed database server, and the database here is a SQLite file
inside a volume. There is nothing for Coolify to dump, and backing up arbitrary
Docker volumes has long been a gap rather than a feature. That is an
understanding of Coolify, not something verifiable from here — the host is not
reachable.

**How to check in two minutes:** open the resource in Coolify, click
**Backups** in the sidebar. If the page only offers to add a *database* backup,
or shows nothing to back up, it does not cover the volumes and the script is
the answer. If it does list the volumes, say so loudly — a scheduled backup
Coolify runs is better than one that depends on someone remembering, and the
script then becomes the thing that proves a restore works.

Either way, two things remain true. A Coolify backup of a *running* volume
would have the same hot-copy problem described above, and a backup that lives
on the same host as the thing it protects is not a backup — the copy has to
leave the machine, by hand, to somewhere the owner controls.

## Scheduling it

Weekly is enough for the session: it changes rarely, and what changes
materially — being logged out — is the thing a backup cannot fix anyway. The
database changes daily. A reasonable split, given each backup takes the bridge
down for a couple of minutes, is a weekly cold backup of both at a quiet hour:

```cron
# 04:10 every Sunday, Nairobi time (the containers run TZ=Africa/Nairobi)
10 4 * * 0 cd /path/to/repo && ./scripts/wa_backup.sh backup --out /root/wa-backups --keep 3 >> /var/log/wa_backup.log 2>&1
```

A backup on the same host is only half of it. Copy the newest directory off the
machine — `scp -r`, to an encrypted disk — and remember on the way that what
you are carrying is the account.
