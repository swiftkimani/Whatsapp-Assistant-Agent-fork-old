# The host's memory, and how to read the swap number

_Measured 2026-11-05 on the development laptop (macOS, Apple silicon). Nothing
in here was measured on `vmi3165735`: that machine is not reachable from where
this was written, and every number below says which command produced it so it
can be produced again there._

## Read this part first: 82% swap is probably not a problem

The monitor has reported swap at 89%, then 87%, 84%, 83%, 82% over one day.
That shape — slowly down, never up — is what **recovery** looks like, not
pressure.

Linux writes a page to swap when it needs the RAM for something else. It does
**not** bring that page back on its own. The page returns only when something
touches it, and a page that nothing has touched for a week is exactly the page
that got swapped in the first place. So "swap used" behaves like a
high-water mark: it records the worst hour the machine has had since the last
reboot, and it drains only as those pages are either faulted back in or thrown
away when their process exits. A host can sit at 82% for weeks with nothing
wrong, because the 82% is not costing RAM and not costing time — nothing is
reading it.

A percentage on its own is also not actionable. 82% of a 512 MiB swap file is
420 MiB; 82% of 4 GiB is 3.4 GiB. The size has to come with it.

**The numbers that say whether there is pressure right now** are the swap
in/out *rate* and how much memory is available without swapping. If nothing is
moving and memory is available, the high-water mark is a scar and the right
action is to leave it alone.

## The commands, and what each answer means

Run these on the host, over SSH.

### 1. The size behind the percentage

```bash
free -m
swapon --show
```

`free -m`: the number to read is **`available`**, not `free`. `free` excludes
the page cache, which the kernel will hand back the moment anything asks;
`available` is the honest estimate of what can be allocated without swapping.
`swapon --show` gives swap's SIZE and USED, which turns the monitor's
percentage into megabytes.

### 2. Is anything actually moving? (the important one)

```bash
vmstat 5 12
```

One line every 5 seconds for a minute. **Ignore the first line** — it is an
average since boot, which is where the high-water mark hides. Read the `si`
and `so` columns, in KiB/s:

| What you see | What it means | What to do |
|---|---|---|
| `si` and `so` both 0 for the whole minute | No paging at all. Whatever the monitor says about swap *used*, there is no current memory pressure. | Nothing. The 82% is history. |
| `so` steadily above 0 | The kernel is still evicting pages: the working set is larger than the RAM it is being given. | This is the case worth acting on. |
| `si` steadily above 0 | Pages are being read back out of swap — processes are stalling on memory they need. Thrashing. | Act now; this is what makes every request slow. |
| Occasional single-digit blips | Normal on any busy host. | Nothing. |

### 3. How much time is being lost to memory (best single signal)

```bash
cat /proc/pressure/memory
```

Pressure Stall Information, on any kernel since 4.20. `some avg60` is the
percentage of the last 60 seconds in which at least one task was stalled
waiting for memory; `full` is the percentage in which *everything* was stalled.
`some` under about 1 is a quiet machine. `some` in the tens, or `full` above 0
at all, is real pressure and matches what "slow at everything" feels like.

If the file does not exist, use the delta instead:

```bash
grep -E 'pswpin|pswpout|pgmajfault' /proc/vmstat; sleep 60
grep -E 'pswpin|pswpout|pgmajfault' /proc/vmstat
```

These are counters since boot, so a single reading says nothing — the
difference over the minute is the answer. `pgmajfault` is the one that costs
wall-clock time: a major fault is a read from disk that a process is waiting on.

### 4. Who is in swap

```bash
for f in /proc/[0-9]*/status; do
  awk '/^Name:/{n=$2} /^VmSwap:/{if ($2+0 > 1024) printf "%9d kB  %s\n", $2, n}' "$f"
done | sort -rn | head -20
```

`VmSwap` per process, over 1 MiB, biggest first. This is how to find out whether
the swapped-out pages belong to this app at all, or to Coolify, or to something
else entirely.

### 5. Per container (cgroup v2)

```bash
docker stats --no-stream
# then, for one container:
id=$(docker ps --filter name=bridge -q)
dir=$(find /sys/fs/cgroup -name "*$(docker inspect -f '{{.Id}}' "$id")*" -maxdepth 4 | head -1)
cat "$dir"/memory.current "$dir"/memory.peak "$dir"/memory.swap.current
```

`docker stats` shows current usage against the limit. The cgroup files are
better: **`memory.peak` is the high-water mark for that container**, which is
the only number that can honestly settle whether a memory limit is set too
high, and `memory.swap.current` says how much of that container's memory is in
swap rather than RAM.

### 6. Proving the number is stale (optional, and not free)

```bash
free -m            # check `available` is larger than swap `used`
sudo swapoff -a && sudo swapon -a
```

This faults every swapped page back into RAM and then re-enables an empty swap,
which resets the high-water mark to 0. **Only do it when `available` is
comfortably larger than swap `used`**: `swapoff` has to find room for
everything it reads back, and if it cannot, it either fails after a long stall
or the OOM killer picks something to kill. It proves the point, but `vmstat`
answers the same question for free.

## What changed here, and the numbers behind it

### Whisper: the default model size is now `base`, not `small`

`scripts/measure_whisper.py` is in the repo for this; it measures a fresh
subprocess per size so one load does not warm the arenas for the next.

```
$ venv/bin/python3 scripts/measure_whisper.py --sizes tiny base small \
    --audio clip-en.ogg clip-sw.ogg

size     on disk  load cost  after use    freed  load s  transcribe s
---------------------------------------------------------------------
tiny       72.0     234.9     348.5    -0.0    1.05    0.35, 0.36
base      138.5     271.5     338.0    -0.6     0.9    0.73, 0.91
small     461.1     561.5     699.3    -7.8     1.6    1.43, 1.57

(MiB, measured on darwin; 'after use' is total process RSS)
```

The exact figure is the one that does not depend on the platform: the int8
weights on disk, **461.1 MiB for `small` against 138.5 MiB for `base`**, a
322.6 MiB reduction in what has to be resident between voice notes. Process
peak RSS went from 742 MiB to 398 MiB in that run. Two runs of the same command
put the peak-RSS gap between `small` and `base` at 201 MiB and 344 MiB — macOS
RSS reporting is noisy around compressed memory, which is why the weights are
the number quoted and the peak is quoted as a range.

Transcription also roughly halves: 1.5 s per five-second clip at `small`,
0.82 s at `base`.

### What that costs in quality

Sixteen clips, read by macOS `say` and re-encoded the way WhatsApp ships a
voice note (mono Opus in Ogg, 16 kbit/s, 16 kHz): six Kenyan English, five
Kiswahili, five Sheng. Word error rate, after normalising case, punctuation and
number words:

| | Kenyan English | Kiswahili | Sheng |
|---|---|---|---|
| tiny | 0.02 | 1.34 | 0.97 |
| base | 0.02 | 1.30 | 0.90 |
| small | **0.01** | 1.26 | 0.85 |

`language="en"`, which is what `STT_LANGUAGE` defaults to and therefore what
production does.

Read the English column first. Across 72 words of English, `small` made one
error and `base` made two — both of them a name (`Benard` heard as `Bennett`,
which `small` got wrong as well). No English clip lost its meaning at any size.
That is what the 322.6 MiB buys.

Now read the other two columns. **Kiswahili and Sheng fail at every size.** A
WER above 1.0 means the transcript has more errors than the reference has
words. At `small`:

```
ref: Habari Arianna, tafadhali mwambie Benard nitachelewa dakika kumi.
got: Abari Arianna, taffa dalimu ambi e benarnita kelewa dakikakumi.

ref: Naomba unitumie ujumbe kesho asubuhi kuhusu mkutano.
got: Now I'm going to be in the kitchen and I'm going to use a kitchen.
```

The second one is a hallucination — forced to decode Swahili as English,
Whisper invents fluent English that has nothing to do with the audio, and it
does that at `small` exactly as it does at `base` (where the same clip came
back as "so I'll see you in the next video"). **Model size is not the lever for
Kiswahili.** Anyone who wants that fixed is looking for a different model or a
different service, not a bigger Whisper.

One real, if small, loss: `app/lang.py`'s `looks_swahili()` has to find Swahili
words in the transcript to route a reply to the right voice. On the ten
Swahili and Sheng clips it agreed with the reference 4 times out of 10 at
`small`, 3 at `base`, 2 at `tiny`. So `base` gives up one clip in ten of a
decision that is already wrong six times in ten.

### Why `base` and not `tiny`

`tiny` scored the same 0.02 on English here and would save another 66 MiB. It
is not the default because **these clips are clean synthetic speech, the
kindest audio Whisper ever gets.** Nothing measurable on this laptop stands in
for a bad line, a noisy room, or a Kenyan accent that no installed voice can
imitate, and `tiny` is where Whisper's error and hallucination rates are known
to climb fastest as audio gets worse. It also already showed the one
content-changing English error in the set (`Kindly` heard as `Finally`).
66 MiB is a cheap price for that margin. If the host is ever genuinely short of
a hundred megabytes, `WHISPER_MODEL_SIZE=tiny` is one field in Coolify.

### `STT_LANGUAGE` stays `en`

Letting Whisper choose (`auto`) was measured too, and it is a wash: at `base`
it moved Kiswahili from 1.30 to 0.96 and made Sheng *worse*, 0.90 to 1.04; at
`small` and `tiny` it changed almost nothing. It also costs a second decode
pass whenever `pick_language` overrides the first guess — 12.1 s against 10.3 s
for the same 16 clips. Not worth a change on that evidence.

### `DASHBOARD_WHISPER_SIZE` is now empty by default

`app/stt.py` caches one loaded model per distinct size. Two different sizes
means two sets of weights resident at the same time. `app/dashboard.py` reads
this variable as `os.getenv(...) or None` and `app/stt.py` answers a `None`
with `WHISPER_MODEL_SIZE`, so leaving it empty makes the dashboard and the
webhook share one model. `tests/test_lazy_models.py` pins that path so a later
change cannot quietly double the memory.

### `MODEL_IDLE_MINUTES` is set to 5 in compose

The cost is measured: reloading `base` from the model cache took 0.9 s. Two
limits worth being honest about.

- `_evict_idle()` only runs when a transcription arrives, so one voice note
  followed by a quiet week still holds its model for the week. The window helps
  the burst-then-quiet-then-burst case and nothing else. Wiring
  `stt._evict_idle()` into `/health` — which the Docker healthcheck already hits
  every 30 seconds — is the one small change that would turn this from a best
  case into a guarantee. It is in `app/main.py`, which this change did not own.
- Handing the pages back to the OS is `malloc_trim()` doing its best on glibc.
  Measured here, an evict-and-reload returned **nothing**: the `freed` column
  above is 0.0, -0.6 and -7.8 MiB. macOS is not glibc and `malloc_trim` is a
  no-op there, so this says nothing about the host — run
  `scripts/measure_whisper.py` on the VPS to find out what it gives back there.

### The backend image: 474 MiB of ffmpeg that nothing could ever call

`Dockerfile` installed `ffmpeg` on the grounds that faster-whisper decodes
through it. It does not. `faster_whisper/audio.py` decodes with PyAV, and its
own docstring says so: *"PyAV bundles the FFmpeg libraries so there is no
additional system dependencies. FFmpeg does not need to be installed on the
system."*

Verified rather than taken on trust — a WhatsApp-shaped clip transcribed with
no ffmpeg binary reachable at all:

```
$ env -i PATH="" HOME="$HOME" venv/bin/python3 -c "
import sys, shutil; sys.path.insert(0, '.')
print('ffmpeg on PATH:', shutil.which('ffmpeg'))
from app.stt import transcribe_bytes
print(transcribe_bytes(open('clip.ogg','rb').read(), language='en', model_size='small'))"
ffmpeg on PATH: None
Can you send me the invoice for last month before Friday?
```

There is also no `subprocess`, `Popen` or `os.system` call anywhere in `app/`
or in `faster_whisper`, so nothing in that image could have reached the binary
even if it wanted to — and `app/dashboard.html` already tells the owner there
is no ffmpeg on the server when he tries to send an animated sticker.

How much it was costing, by Debian's own `Installed-Size` fields over the
`Depends` closure (`python:3.12-slim` is `debian:trixie-slim`, and its
`var/lib/dpkg/status` lists 87 packages, read straight out of the registry
layers):

| apt install | packages added | Installed-Size added |
|---|---|---|
| `ffmpeg` | 236 | 488 MiB |
| `curl` | 22 | 14 MiB |
| both | 237 | 488 MiB |

`libllvm19` at 123.7 MiB, `mesa-libgallium` at 40.6, `libicu76` at 36.5,
`samba-libs` at 25.3, `libflite1` at 26.9 — a speech synthesiser and a Windows
file-sharing library, in a container that transcribes voice notes. Keeping
`curl` for the healthcheck and dropping `ffmpeg` takes roughly **474 MiB** out
of the image. The exact delta needs a `docker build`, and there is no Docker
daemon on this laptop to run one.

Replacing `curl` with a Python one-liner would save the other 14 MiB and spend
a fresh interpreter every 30 seconds instead. On a host short of memory that is
the wrong way round, so curl stays.

### The Python packages are not where the weight is

```bash
pip download --only-binary :all: --platform manylinux_2_28_x86_64 \
  --python-version 3.12 --implementation cp --abi cp312 -r requirements.txt
```

146 MiB across 73 wheels, of which ctranslate2 (40.8), av (33.1), onnxruntime
(22.7) and numpy (16.0) are 113 MiB — and Whisper needs all four. `openai` and
`edge-tts` are imported lazily by `app/tts.py`, so they cost image bytes and no
resident memory. There is nothing here worth removing, and a multi-stage build
would have nothing to stage away: the build installs no compiler, every wheel
is binary, and `PIP_NO_CACHE_DIR=1` leaves no cache behind.

### The bridge image is already as small as it honestly gets

Chromium is the image. By the same method on bookworm, which is what
`node:24-bookworm-slim` is, `chromium` + `fonts-liberation` is 229 packages and
730 MiB — 267 MiB of it `chromium` itself — and every one of those 229 is a
`Depends`. The build is already two-stage (git stays in the deps stage),
already `--no-install-recommends`, already `npm ci --omit=dev`, and
`package.json` has no `devDependencies` to omit. `whatsapp-bridge/*.js` does
ship 98 KiB of `*.test.js` into the runtime image, which is untidy and not
worth a change.

## What was deliberately not changed

**The bridge's 3072m limit.** It looks like the obvious thing to cut and it is
not, because **a limit reserves nothing**. Lowering it to 2048m frees exactly
zero bytes unless the bridge's working set is between the two numbers — and in
that case the kernel kills the container instead, mid-WhatsApp-sync, which is
the one failure here that can loop. Under cgroup v2 a tighter ceiling can even
push the reported swap figure *up*, because nearing `memory.max` forces reclaim
and reclaim is what writes anonymous pages to swap. Nothing on this laptop can
measure Chromium's real working set: there is no WhatsApp session to drive it
and no Docker daemon to run it under. `memory.peak` from §5 above, watched for
a week, is what would settle it — and even then the saving would be zero.

**The backend's 1536m limit and its 1024m reservation.** Same reasoning. The
accounted figure halved (135 MiB at boot plus ~272 MiB for `base`, against
~800 MiB before), and the ceiling still does its job, which is capping a
runaway rather than rationing.

**`requirements.txt`, `.env`, `.env.example`, `app/main.py`.** Owned elsewhere.

## Two things that still need a human

1. **`WHISPER_MODEL_SIZE=small` is almost certainly saved in Coolify.** The
   compose file only supplies a *default*; a value saved in Coolify's
   Environment Variables beats it. The repo's own `.env` pins
   `WHISPER_MODEL_SIZE=small` and `DASHBOARD_WHISPER_SIZE=small`, and Coolify's
   field was generated from the old default, so the host will keep loading
   `small` until those fields are set to `base` and blank. Checked with:
   ```bash
   docker compose config | grep WHISPER   # reads .env; shows small
   docker compose --env-file /dev/null config | grep WHISPER   # shows base
   ```
2. **`.dockerignore` does not exclude the repository root's `.wwebjs_auth/`** —
   only `whatsapp-bridge/.wwebjs_auth/`. That root directory is 106 MB of a
   second Chromium profile, and it is sent to the Docker daemon on every single
   build. It never lands in a layer (nothing `COPY`s it), but it is credential
   material in the build context for no reason. One line in `.dockerignore`
   fixes it; that file was not in this change's scope.
