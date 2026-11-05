#!/usr/bin/env bash
#
# Backup and restore for the two things here that cannot be rebuilt:
#
#   1. .wwebjs_auth -- the linked WhatsApp session. THIS IS A CREDENTIAL.
#      Anyone who holds a copy can read and answer messages as the owner.
#      WhatsApp wants a passkey ceremony to link a new companion device, which
#      needs a real browser and an authenticator, so the server cannot link
#      itself: this directory is the only copy of something that has to be made
#      on a laptop and carried in. Treat a backup of it exactly as you would
#      treat the phone.
#   2. data/assistant.db -- the held queue, the message log, the notes, the
#      per-contact modes. In WAL mode, so `cp` is not a safe way to copy it.
#
# Nothing here uploads anything. The copies land in a directory on the machine
# you run this on, mode 700, files mode 600, and getting them somewhere safe is
# a decision for a human who understands that putting them in a bucket, a chat
# or a repository hands over the account.
#
# Usage:
#   scripts/wa_backup.sh backup  [--out DIR] [--keep N] [--hot]
#   scripts/wa_backup.sh backup  --session-dir DIR --db FILE --cold [--out DIR]
#   scripts/wa_backup.sh verify  BACKUP_DIR
#   scripts/wa_backup.sh restore BACKUP_DIR [--session-dir DIR] [--db FILE]
#
# With no --session-dir/--db it works through Docker: it finds the two
# containers by what they have mounted, so Coolify's volume-name prefixes do
# not matter. With them it works on plain paths, which is how the laptop's copy
# of the session gets backed up and how the restore is tested without a server.
#
# Read docs/session-backup.md before the first restore.

set -euo pipefail
umask 077

SESSION_IN_CONTAINER=/app/whatsapp-bridge/.wwebjs_auth
DATA_IN_CONTAINER=/app/data
DB_NAME=assistant.db

OUT_ROOT=${WA_BACKUP_DIR:-$HOME/wa-backups}
KEEP=3
CONSISTENCY=""        # cold | hot
SESSION_DIR=""        # set => filesystem mode for the session
DB_FILE=""            # set => filesystem mode for the database
BRIDGE_CONTAINER=""
BRIDGE_STOPPED_BY_US=""

die() { printf 'wa_backup: %s\n' "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }

usage() {
  cat <<'TXT'
scripts/wa_backup.sh backup  [--out DIR] [--keep N] [--hot]
scripts/wa_backup.sh backup  --session-dir DIR --db FILE --cold [--out DIR]
scripts/wa_backup.sh verify  BACKUP_DIR
scripts/wa_backup.sh restore BACKUP_DIR [--session-dir DIR --db FILE]

Copies the linked WhatsApp session and data/assistant.db somewhere they can be
restored from. The session is a CREDENTIAL: whoever holds a copy can answer
messages as this account, and the server cannot re-link itself, so a copy is
also the only way back. Nothing here uploads anything.

With no --session-dir/--db it works through Docker and finds the containers by
what they have mounted. With both, it works on plain paths -- which is how the
laptop's copy gets backed up and how a restore is tested without a server.

Read docs/session-backup.md before the first restore.
TXT
}

need() { command -v "$1" >/dev/null 2>&1 || die "need $1 on PATH"; }

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

# ---------------------------------------------------------------- docker mode

# The container that has PATH mounted, and the name of the volume behind it.
# Printed as "container<TAB>volume<TAB>image"; empty if nothing matches.
find_mount() {
  local path=$1 id
  for id in $(docker ps -aq); do
    docker inspect "$id" --format '
      {{- $want := "'"$path"'" -}}
      {{- range .Mounts}}{{if eq .Destination $want}}{{$.Name}}	{{.Name}}	{{$.Config.Image}}{{end}}{{end}}'
  done | sed 's|^/||' | grep -v '^[[:space:]]*$' | head -1
}

# tar, gzip and python3 all come from the service's own image, so this adds no
# dependency to the host and no second place where versions can drift. --user 0
# because the volumes are owned by uid 10001 and the host user is not it.
in_volume() {
  local volume=$1 image=$2; shift 2
  docker run --rm --user 0 -v "$volume:/vol" -v "$WORK:/work" "$image" "$@"
}

start_bridge_again() {
  if [ -n "$BRIDGE_STOPPED_BY_US" ]; then
    printf '  starting %s again\n' "$BRIDGE_CONTAINER"
    docker start "$BRIDGE_CONTAINER" >/dev/null || \
      printf 'wa_backup: COULD NOT START %s -- start it by hand now\n' "$BRIDGE_CONTAINER" >&2
    BRIDGE_STOPPED_BY_US=""
  fi
}

# ------------------------------------------------------------------- database

# VACUUM INTO, not cp, and not the three files cp would have to get right.
#
# app/db.py puts every connection in WAL mode, so at any moment the committed
# state of this database is spread across assistant.db, assistant.db-wal and
# assistant.db-shm. Copying the first one alone gives a database missing
# everything committed since the last checkpoint; copying all three while a
# writer is mid-commit gives a torn set. VACUUM INTO sidesteps both: SQLite
# reads a consistent snapshot inside a read transaction -- which WAL lets it do
# without blocking the writer -- and writes one self-contained file with no WAL
# to carry. The backup API (Connection.backup / .backup in the CLI) is equally
# correct and restarts itself if a write lands mid-copy; VACUUM INTO is one
# statement and defragments on the way out, so it is the one used here.
snapshot_db_to() {
  local target=$1
  local sql="VACUUM INTO '$target'"
  if [ -n "$DB_FILE" ]; then
    [ -f "$DB_FILE" ] || die "no database at $DB_FILE"
    if command -v sqlite3 >/dev/null 2>&1; then
      sqlite3 "$DB_FILE" "$sql"
    else
      python3 -c 'import sqlite3,sys; sqlite3.connect(sys.argv[1]).execute(sys.argv[2])' \
        "$DB_FILE" "$sql"
    fi
  else
    in_volume "$DATA_VOLUME" "$DATA_IMAGE" python3 -c \
      'import sqlite3,sys; sqlite3.connect(sys.argv[1]).execute(sys.argv[2])' \
      "/vol/$DB_NAME" "VACUUM INTO '/work/$DB_NAME'"
  fi
}

integrity_of() {
  local db=$1
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$db" 'PRAGMA integrity_check; SELECT "tables=" || count(*) FROM sqlite_master WHERE type="table";'
  else
    python3 - "$db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(c.execute("PRAGMA integrity_check").fetchone()[0])
print("tables=%d" % c.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
PY
  fi
}

# --------------------------------------------------------------------- backup

do_backup() {
  local stamp dest
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  dest="$OUT_ROOT/$stamp"

  # A credential does not go in a repository, however convenient the path is.
  # Asked of the nearest directory that exists rather than of $OUT_ROOT: git -C
  # fails on a path that is not there yet, and a failing check reads as a pass.
  # That is not hypothetical — it let a first version of this write a session
  # into the repo it was meant to keep it out of.
  local probe=$OUT_ROOT
  while [ ! -d "$probe" ] && [ "$probe" != "/" ] && [ "$probe" != "." ]; do
    probe=$(dirname "$probe")
  done
  if git -C "$probe" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    die "$OUT_ROOT is inside the git work tree at $(git -C "$probe" rev-parse --show-toplevel). Pick somewhere else: this is a credential."
  fi
  mkdir -p "$dest"
  chmod 700 "$OUT_ROOT" "$dest"
  WORK=$dest

  if [ -z "$SESSION_DIR" ] || [ -z "$DB_FILE" ]; then
    need docker
    local found
    found=$(find_mount "$SESSION_IN_CONTAINER") || true
    [ -n "$found" ] || die "no container has $SESSION_IN_CONTAINER mounted. Pass --session-dir instead."
    BRIDGE_CONTAINER=$(printf '%s' "$found" | cut -f1)
    SESSION_VOLUME=$(printf '%s' "$found" | cut -f2)
    SESSION_IMAGE=$(printf '%s' "$found" | cut -f3)
    found=$(find_mount "$DATA_IN_CONTAINER") || true
    [ -n "$found" ] || die "no container has $DATA_IN_CONTAINER mounted. Pass --db instead."
    DATA_VOLUME=$(printf '%s' "$found" | cut -f2)
    DATA_IMAGE=$(printf '%s' "$found" | cut -f3)
    note "bridge container $BRIDGE_CONTAINER, session volume $SESSION_VOLUME"
    note "data volume $DATA_VOLUME"
  fi

  # The session is a live Chromium profile: the credential sits in
  # Default/IndexedDB, which is a LevelDB store with its own log and lock
  # files. A copy taken while Chromium is writing can restore into a profile
  # Chromium will not open, and there is no way to find that out except by
  # trying it on the day it matters. So the default stops the bridge first.
  if [ "$CONSISTENCY" = "hot" ]; then
    printf '\n  WARNING: hot copy. This may not restore. Use it only when the\n'
    printf '  alternative is having no copy at all, and replace it with a cold\n'
    printf '  one as soon as you can take the downtime.\n\n'
  elif [ -n "$BRIDGE_CONTAINER" ]; then
    CONSISTENCY=cold
    if [ "$(docker inspect -f '{{.State.Running}}' "$BRIDGE_CONTAINER")" = "true" ]; then
      note "stopping $BRIDGE_CONTAINER for the copy"
      trap start_bridge_again EXIT INT TERM
      docker stop "$BRIDGE_CONTAINER" >/dev/null
      BRIDGE_STOPPED_BY_US=1
    fi
  elif [ "$CONSISTENCY" != "cold" ]; then
    die "in --session-dir mode, say --cold (the bridge is not running) or --hot (accept a torn copy)"
  fi

  note "copying the session"
  # tar -czf rather than tar | gzip, in both modes, so that the exit status
  # belongs to tar: GNU tar returns 1 for "a file changed as we read it", which
  # is precisely what a hot copy of a live profile looks like and precisely
  # what must not be mistaken for success.
  local status=0
  if [ -n "$SESSION_DIR" ]; then
    [ -d "$SESSION_DIR" ] || die "no session directory at $SESSION_DIR"
    tar -czf "$dest/session.tar.gz" \
      -C "$(dirname "$SESSION_DIR")" "$(basename "$SESSION_DIR")" || status=$?
  else
    in_volume "$SESSION_VOLUME" "$SESSION_IMAGE" \
      tar -czf /work/session.tar.gz -C /vol . || status=$?
  fi
  if [ "$status" = 1 ] && [ "$CONSISTENCY" = hot ]; then
    note "tar says files changed under it while copying. That IS the hot copy"
    note "problem, not a separate one. Take a cold copy when you can."
  elif [ "$status" != 0 ]; then
    die "tar failed (exit $status) -- there is no usable session copy"
  fi

  start_bridge_again
  trap - EXIT INT TERM

  note "snapshotting the database"
  rm -f "$dest/$DB_NAME"
  snapshot_db_to "$dest/$DB_NAME"

  if [ -z "$SESSION_DIR" ] || [ -z "$DB_FILE" ]; then
    # Written by a container running as root; hand them back to whoever ran this.
    in_volume "$SESSION_VOLUME" "$SESSION_IMAGE" \
      chown -R "$(id -u):$(id -g)" /work
  fi
  chmod 600 "$dest"/*

  {
    printf 'wa_backup %s\n' "$stamp"
    printf 'host: %s\n' "$(hostname)"
    printf 'consistency: %s\n' "$CONSISTENCY"
    printf 'session source: %s\n' "${SESSION_DIR:-volume $SESSION_VOLUME ($BRIDGE_CONTAINER)}"
    printf 'database source: %s\n' "${DB_FILE:-volume $DATA_VOLUME}"
    printf '\n'
    printf 'THIS DIRECTORY IS A CREDENTIAL. session.tar.gz is a linked WhatsApp\n'
    printf 'session: whoever holds it can answer messages as this account. The\n'
    printf 'server cannot re-link itself, so this is also the only way back.\n'
    printf 'Do not put it in a repository, a bucket or a chat.\n'
    printf '\n'
    for f in session.tar.gz "$DB_NAME"; do
      printf '%s  %s  %s bytes\n' "$(sha256_of "$dest/$f")" "$f" "$(wc -c < "$dest/$f" | tr -d ' ')"
    done
    printf '\nverify:  scripts/wa_backup.sh verify %s\n' "$dest"
    printf 'restore: scripts/wa_backup.sh restore %s\n' "$dest"
  } > "$dest/manifest.txt"
  chmod 600 "$dest/manifest.txt"

  printf '\n'
  cat "$dest/manifest.txt"
  prune_old
  printf '\nNow verify it: scripts/wa_backup.sh verify %s\n' "$dest"
}

prune_old() {
  [ "$KEEP" -gt 0 ] || return 0
  local old count drop
  # Only complete backups count, and only they are pruned: a directory with no
  # manifest is either in progress or a failure, and neither should push a good
  # backup off the end.
  count=$(ls -1d "$OUT_ROOT"/*/manifest.txt 2>/dev/null | wc -l | tr -d ' ')
  drop=$((count - KEEP))
  [ "$drop" -gt 0 ] || return 0
  # head -n "$drop" rather than head -n -"$KEEP": the negative form is GNU only
  # and this also has to run on the laptop.
  old=$(ls -1d "$OUT_ROOT"/*/manifest.txt 2>/dev/null | sort | head -n "$drop")
  [ -n "$old" ] || return 0
  printf '\n  pruning to the newest %s:\n' "$KEEP"
  local m
  for m in $old; do
    note "removing $(dirname "$m")"
    rm -rf "$(dirname "$m")"
  done
}

# --------------------------------------------------------------------- verify

do_verify() {
  local dir=${1:-} ok=1
  [ -n "$dir" ] && [ -f "$dir/manifest.txt" ] || die "usage: wa_backup.sh verify BACKUP_DIR"
  printf 'verifying %s\n' "$dir"

  local line want name
  while read -r want name _; do
    [ -f "$dir/$name" ] || { printf '  MISSING %s\n' "$name"; ok=0; continue; }
    if [ "$(sha256_of "$dir/$name")" = "$want" ]; then
      note "$name checksum ok"
    else
      printf '  CHECKSUM MISMATCH %s\n' "$name"; ok=0
    fi
  done < <(grep -E '^[0-9a-f]{64}  ' "$dir/manifest.txt")

  printf '  database:\n'
  integrity_of "$dir/$DB_NAME" | sed 's/^/    /'

  printf '  session archive:\n'
  gzip -t "$dir/session.tar.gz" 2>/dev/null || { printf '    GZIP IS CORRUPT\n'; ok=0; }
  # The four things a restored profile is useless without. IndexedDB is where
  # WhatsApp Web keeps the session keys and the chat store both, which is also
  # why there is nothing big in this archive worth leaving out.
  local entries path
  # || true so that a damaged archive reaches the refusal at the bottom of this
  # function with every finding printed, instead of killing the script here on
  # tar's own exit status and leaving the reason to be guessed from zlib.
  entries=$(tar -tzf "$dir/session.tar.gz" 2>/dev/null || true)
  for path in "Default/IndexedDB" "Default/Local Storage" "Default/Preferences" "Local State"; do
    if printf '%s' "$entries" | grep -qF "$path"; then
      note "  holds $path"
    else
      printf '    MISSING %s\n' "$path"; ok=0
    fi
  done
  printf '  %s entries in the archive\n' "$(printf '%s\n' "$entries" | wc -l | tr -d ' ')"

  grep -q '^consistency: hot' "$dir/manifest.txt" && \
    printf '\n  NOTE: this was a hot copy. The checks above cannot tell you whether\n  Chromium will open the profile. Only a restore can.\n'

  [ "$ok" = 1 ] || die "this backup is not sound -- take another one"
  printf '\nsound.\n'
}

# -------------------------------------------------------------------- restore

do_restore() {
  local dir=${1:-}
  [ -n "$dir" ] && [ -f "$dir/manifest.txt" ] || die "usage: wa_backup.sh restore BACKUP_DIR"
  # A restore is the one path where a bad copy does damage, so it goes through
  # the same checks first. do_verify exits non-zero itself if anything is off.
  do_verify "$dir"
  local stamp; stamp=$(date -u +%Y%m%dT%H%M%SZ)
  WORK=$dir

  printf 'restoring %s\n' "$dir"
  printf 'Nothing is deleted: what is there now is renamed with .before-restore-%s\n\n' "$stamp"

  if [ -z "$SESSION_DIR" ]; then
    need docker
    local found
    found=$(find_mount "$SESSION_IN_CONTAINER") || true
    [ -n "$found" ] || die "no container has $SESSION_IN_CONTAINER mounted"
    BRIDGE_CONTAINER=$(printf '%s' "$found" | cut -f1)
    SESSION_VOLUME=$(printf '%s' "$found" | cut -f2)
    SESSION_IMAGE=$(printf '%s' "$found" | cut -f3)
    if [ "$(docker inspect -f '{{.State.Running}}' "$BRIDGE_CONTAINER")" = "true" ]; then
      note "stopping $BRIDGE_CONTAINER"
      docker stop "$BRIDGE_CONTAINER" >/dev/null
      BRIDGE_STOPPED_BY_US=1
    fi
    note "parking the profile that is there now"
    in_volume "$SESSION_VOLUME" "$SESSION_IMAGE" sh -c \
      "mkdir -p /vol/.before-restore-$stamp && \
       find /vol -mindepth 1 -maxdepth 1 ! -name '.before-restore-*' \
            -exec mv {} /vol/.before-restore-$stamp/ \;"
    note "unpacking the session"
    in_volume "$SESSION_VOLUME" "$SESSION_IMAGE" sh -c \
      'tar -C /vol -xzf /work/session.tar.gz && chown -R 10001:10001 /vol'
  else
    [ -d "$(dirname "$SESSION_DIR")" ] || die "no parent directory for $SESSION_DIR"
    if [ -e "$SESSION_DIR" ]; then
      note "parking $SESSION_DIR"
      mv "$SESSION_DIR" "$SESSION_DIR.before-restore-$stamp"
    fi
    note "unpacking the session"
    mkdir -p "$SESSION_DIR"
    tar -C "$SESSION_DIR" -xzf "$dir/session.tar.gz"
    # The archive holds either the directory itself or its contents, depending
    # on which mode took it; flatten the first case so the result is the same.
    if [ -d "$SESSION_DIR/$(basename "$SESSION_DIR")" ]; then
      local inner="$SESSION_DIR/$(basename "$SESSION_DIR")"
      (cd "$inner" && tar -cf - .) | (cd "$SESSION_DIR" && tar -xf -)
      rm -rf "$inner"
    fi
  fi

  if [ -z "$DB_FILE" ]; then
    local found
    found=$(find_mount "$DATA_IN_CONTAINER") || true
    [ -n "$found" ] || die "no container has $DATA_IN_CONTAINER mounted"
    local backend; backend=$(printf '%s' "$found" | cut -f1)
    DATA_VOLUME=$(printf '%s' "$found" | cut -f2)
    DATA_IMAGE=$(printf '%s' "$found" | cut -f3)
    note "stopping $backend"
    docker stop "$backend" >/dev/null || true
    # The stale -wal and -shm matter: left beside a restored database they are
    # a write-ahead log belonging to a different file, and SQLite would either
    # replay them over the restored pages or refuse the database outright.
    note "parking the database and clearing its old WAL"
    in_volume "$DATA_VOLUME" "$DATA_IMAGE" sh -c \
      "cd /vol && for f in $DB_NAME $DB_NAME-wal $DB_NAME-shm; do \
         [ -e \"\$f\" ] && mv \"\$f\" \"\$f.before-restore-$stamp\"; done; \
       cp /work/$DB_NAME /vol/$DB_NAME && chown 10001:10001 /vol/$DB_NAME"
    note "starting $backend"
    docker start "$backend" >/dev/null
  else
    local f
    for f in "$DB_FILE" "$DB_FILE-wal" "$DB_FILE-shm"; do
      [ -e "$f" ] && mv "$f" "$f.before-restore-$stamp"
    done
    cp "$dir/$DB_NAME" "$DB_FILE"
  fi

  start_bridge_again
  printf '\nRestored. What to check, in this order:\n'
  printf '  1. the bridge log says it is ready and does NOT show a QR or a pairing code\n'
  printf '  2. send yourself a message and watch it arrive\n'
  printf '  3. the dashboard lists chats\n'
  printf 'If it asks to be linked, the restore did not take -- the parked\n'
  printf '.before-restore-%s copies are still there and nothing was deleted.\n' "$stamp"
}

# ----------------------------------------------------------------------- main

cmd=${1:-}; shift || true
while [ $# -gt 0 ]; do
  case $1 in
    --out) OUT_ROOT=$2; shift 2 ;;
    --keep) KEEP=$2; shift 2 ;;
    --hot) CONSISTENCY=hot; shift ;;
    --cold) CONSISTENCY=cold; shift ;;
    --session-dir) SESSION_DIR=${2%/}; shift 2 ;;
    --db) DB_FILE=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option $1" ;;
    *) ARG=$1; shift ;;
  esac
done

# Both or neither. One of the two alone would mean taking the session off the
# laptop and the database out of the server in the same backup, which is a
# pair that cannot be restored together.
if { [ -n "$SESSION_DIR" ] && [ -z "$DB_FILE" ]; } || { [ -z "$SESSION_DIR" ] && [ -n "$DB_FILE" ]; }; then
  die "--session-dir and --db go together: pass both for path mode, neither for Docker mode"
fi

case $cmd in
  backup) do_backup ;;
  verify) do_verify "${ARG:-}" ;;
  restore) do_restore "${ARG:-}" ;;
  *) usage; exit 1 ;;
esac
