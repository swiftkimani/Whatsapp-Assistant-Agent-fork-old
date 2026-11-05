#!/usr/bin/env python3
"""What transcription costs here: memory, and accuracy on the real language mix.

Two things, because they are the two questions this project keeps having to
answer about a voice note, and they are asked of the same code.

MEMORY (the default) -- what one faster-whisper model size costs this
container. Run it on the host the numbers are about. Nothing in
docker-compose.yaml's memory reasoning can be checked from a laptop: glibc's
allocator, which is where the retention the compose comments argue about comes
from, is not the one macOS uses, and the model is loaded by the same
ctranslate2 wheel either way.

    python3 scripts/measure_whisper.py --sizes tiny base small
    python3 scripts/measure_whisper.py --sizes base --audio note.ogg

Each size is measured in a fresh subprocess, because a process that has already
loaded one model has already grown the arenas the next one would reuse. Three
numbers per size: resident after the import, resident after the load, and what
came back after the model was dropped -- that last one is the one that decides
whether MODEL_IDLE_MINUTES buys anything real.

QUALITY (--score) -- word error rate on sixteen WhatsApp-shaped clips, six
Kenyan English, five Kiswahili, five Sheng, through whichever provider you
name. Same clips, same metric, so the two providers can actually be compared
rather than argued about.

    python3 scripts/measure_whisper.py --score whisper --sizes tiny base small
    python3 scripts/measure_whisper.py --score gemini --pause 4   # GEMINI_API_KEY
    python3 scripts/measure_whisper.py --score both

Honest about the corpus: the sixteen reference sentences behind the table in
docs/host-memory.md were never committed. CORPUS below is a rebuilt set of the
same shape and the same recipe -- macOS `say`, re-encoded to mono Opus in Ogg
at 16 kbit/s and 16 kHz, the way WhatsApp ships a voice note -- and two of its
sentences are the exact ones quoted in that document. So numbers from one run
are directly comparable with each other, and roughly comparable with that
table, not line for line. Synthetic speech is also the kindest audio a model
ever gets: --clips with real recordings is the better measurement, and the one
you can only make with audio you are entitled to send to a provider.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MIB = 1024 * 1024
REPO_ROOT = Path(__file__).resolve().parent.parent
# Run from anywhere: this script lives one level down from the package it loads.
sys.path.insert(0, str(REPO_ROOT))

# Where synthesised clips are kept between runs, so a second run costs nothing.
# Under data/, which .gitignore excludes: this repository is public and these
# are build artefacts, not source.
CLIPS_DIR = REPO_ROOT / "data" / "stt-corpus"

# (clip id, language group, reference text). Six Kenyan English, five
# Kiswahili, five Sheng -- the mix these voice notes actually arrive in. The
# two marked sentences are the ones docs/host-memory.md quotes; the rest were
# written to the same shape. The English lines deliberately keep the two words
# that broke before ("Kindly", "Benard") and some spoken numbers, which is what
# the number-word normalisation below is for.
CORPUS: tuple[tuple[str, str, str], ...] = (
    ("en-1", "english", "Kindly confirm whether the meeting is still on for tomorrow morning."),
    ("en-2", "english", "Benard said he will send the invoice before the end of the week."),
    ("en-3", "english", "I am stuck in traffic along Thika Road, I will be about twenty minutes late."),
    ("en-4", "english", "Please tell her I called and that I will try again this evening."),
    ("en-5", "english", "The training starts at nine, so let us meet at the gate by eight thirty."),
    ("en-6", "english", "I have sent the documents to your email, check and let me know."),
    # quoted in docs/host-memory.md
    ("sw-1", "kiswahili", "Habari Arianna, tafadhali mwambie Benard nitachelewa dakika kumi."),
    # quoted in docs/host-memory.md
    ("sw-2", "kiswahili", "Naomba unitumie ujumbe kesho asubuhi kuhusu mkutano."),
    ("sw-3", "kiswahili", "Niko njiani sasa, nitafika ofisini saa tatu."),
    ("sw-4", "kiswahili", "Sijui kama utakuja leo jioni, nipigie simu tafadhali."),
    ("sw-5", "kiswahili", "Asante sana kwa msaada wako, tuonane baadaye."),
    ("sheng-1", "sheng", "Niaje manze, uko wapi? Nakuja hapo kwa base."),
    ("sheng-2", "sheng", "Buda, wacha nikuambie, hiyo deal ni noma sana."),
    ("sheng-3", "sheng", "Vipi, utakuja mtaa leo ama tuonane kesho?"),
    ("sheng-4", "sheng", "Manze nimechelewa, nitafika hapo baadaye kidogo."),
    ("sheng-5", "sheng", "Poa, nitatuma hiyo meseji kwa WhatsApp sasa hivi."),
)
LANGUAGES = ("english", "kiswahili", "sheng")

# Spoken numbers come back as digits from one model and as words from another,
# and that is a formatting difference, not a mistake. Everything a clip in
# CORPUS can say, plus the tens, which is as far as this needs to go.
_NUMBER_WORDS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
    "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten",
    "11": "eleven", "12": "twelve", "13": "thirteen", "14": "fourteen",
    "15": "fifteen", "16": "sixteen", "17": "seventeen", "18": "eighteen",
    "19": "nineteen", "20": "twenty", "30": "thirty", "40": "forty",
    "50": "fifty", "60": "sixty", "70": "seventy", "80": "eighty", "90": "ninety",
}
_NOT_WORD = re.compile(r"[^\w\s]+", re.UNICODE)


def rss_bytes() -> int:
    """Current resident set size, from the kernel rather than from Python."""
    if sys.platform == "linux":
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        return 0
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                         capture_output=True, text=True, check=True)
    return int(out.stdout.strip()) * 1024


def peak_bytes() -> int:
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Darwin reports bytes, Linux kilobytes.
    return peak if sys.platform == "darwin" else peak * 1024


def weights_on_disk(size: str) -> int:
    """model.bin in the HF cache: the int8 weights, before any allocator."""
    home = Path(os.getenv("HF_HOME") or (Path.home() / ".cache" / "huggingface"))
    hub = home / "hub" if (home / "hub").is_dir() else home
    found = list(hub.glob(f"models--*faster-whisper-{size}/snapshots/*/model.bin"))
    return found[0].stat().st_size if found else 0


def measure_one(size: str, audio: list[str]) -> dict:
    from app import stt

    baseline = rss_bytes()
    t0 = time.monotonic()
    name, _ = stt._acquire_model(size)
    load_seconds = time.monotonic() - t0
    stt._release_model(name)
    loaded = rss_bytes()

    transcripts, seconds = [], []
    for path in audio:
        data = Path(path).read_bytes()
        t0 = time.monotonic()
        transcripts.append(stt.transcribe_bytes(data, language="en", model_size=size))
        seconds.append(round(time.monotonic() - t0, 2))
    after_use = rss_bytes()

    # What the eviction in app/stt.py gives back, run the way app/stt.py runs
    # it: backdate the last use past the idle window and let _evict_idle do the
    # rest, malloc_trim included.
    stt._last_used[size] = time.monotonic() - (stt.idle_window_seconds() + 60)
    with stt._lock:
        stt._evict_idle(time.monotonic())
    gc.collect()
    evicted = rss_bytes()

    return {
        "size": size,
        "weights_on_disk_mib": round(weights_on_disk(size) / MIB, 1),
        "rss_before_load_mib": round(baseline / MIB, 1),
        "rss_after_load_mib": round(loaded / MIB, 1),
        "load_cost_mib": round((loaded - baseline) / MIB, 1),
        "rss_after_transcribing_mib": round(after_use / MIB, 1),
        "rss_after_eviction_mib": round(evicted / MIB, 1),
        "returned_by_eviction_mib": round((after_use - evicted) / MIB, 1),
        "peak_rss_mib": round(peak_bytes() / MIB, 1),
        "load_seconds": round(load_seconds, 2),
        "transcribe_seconds": seconds,
        "transcripts": transcripts,
        "platform": sys.platform,
    }


# ---------------------------------------------------------------- quality ---

def normalise(text: str) -> list[str]:
    """The words of a transcript, with everything that is not a mistake removed.

    Case, punctuation and digit-versus-word are formatting, and two providers
    disagree about all three without either being wrong. What is left is the
    words, which is what a word error rate is supposed to count.
    """
    words = _NOT_WORD.sub(" ", (text or "").lower()).split()
    return [_NUMBER_WORDS.get(word, word) for word in words]


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Edit distance over words, divided by the number of reference words.

    Above 1.0 is possible and is the whole point of measuring it: it means the
    transcript needs more corrections than the reference has words, which is
    what Kiswahili does to Whisper at every model size.
    """
    want, got = normalise(reference), normalise(hypothesis)
    if not want:
        return 0.0
    # Two rows of the Levenshtein table rather than all of them: the clips are
    # one sentence each, but there is no reason to hold n*m integers for it.
    previous = list(range(len(got) + 1))
    for i, want_word in enumerate(want, start=1):
        current = [i]
        for j, got_word in enumerate(got, start=1):
            current.append(min(previous[j] + 1,              # deletion
                               current[j - 1] + 1,           # insertion
                               previous[j - 1] + (want_word != got_word)))
        previous = current
    return previous[-1] / len(want)


def synthesise(reference: str, path: Path, voice: str | None) -> None:
    """One clip, shaped the way WhatsApp ships a voice note.

    `say` writes AIFF; the re-encode to mono Opus in Ogg at 16 kbit/s and
    16 kHz is what makes the measurement about the audio a voice note really
    is rather than about studio-quality speech. Both tools are macOS/Homebrew
    and host-only, which this whole script already is.
    """
    aiff = path.with_suffix(".aiff")
    spoken = ["say", "-o", str(aiff)] + (["-v", voice] if voice else []) + [reference]
    subprocess.run(spoken, check=True, capture_output=True)
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff),
                        "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "16k",
                        str(path)], check=True, capture_output=True)
    finally:
        aiff.unlink(missing_ok=True)


def build_corpus(clips_dir: Path, voice: str | None) -> list[tuple[str, str, Path]]:
    """The clips, synthesising any that are not there yet."""
    for tool in ("say", "ffmpeg"):
        if shutil.which(tool) is None:
            raise SystemExit(f"--score needs {tool} on PATH to build the corpus "
                             f"(or pass --clips with your own recordings)")
    clips_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    for clip_id, language, reference in CORPUS:
        path = clips_dir / f"{clip_id}.ogg"
        if not path.exists():
            synthesise(reference, path, voice)
        clips.append((reference, language, path))
    return clips


def load_corpus(clips_dir: Path, refs_file: Path) -> list[tuple[str, str, Path]]:
    """Real recordings instead of synthesised ones.

    refs_file is tab-separated: filename, language group, reference text. A
    line whose file is missing is a typo worth stopping for, not a clip worth
    skipping -- a corpus quietly three clips short is a wrong number.
    """
    clips = []
    for number, line in enumerate(refs_file.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            raise SystemExit(f"{refs_file}:{number}: expected filename<TAB>language<TAB>reference")
        name, language, reference = parts[0].strip(), parts[1].strip().lower(), parts[2].strip()
        path = clips_dir / name
        if not path.exists():
            raise SystemExit(f"{refs_file}:{number}: no such clip: {path}")
        clips.append((reference, language, path))
    return clips


def transcribe_with(provider: str, audio: bytes, size: str | None) -> str:
    """One clip through one provider, called the way production calls it."""
    if provider == "gemini":
        from app import stt_gemini

        # "auto" rather than STT_LANGUAGE: letting it detect the language is
        # the thing being measured. GEMINI_STT_LANGUAGE_CODES still overrides.
        return stt_gemini.transcribe(audio, language="auto")
    from app import stt

    return stt.transcribe_bytes(audio, language=os.getenv("STT_LANGUAGE", "en"),
                                model_size=size, vad=False)


def score(provider: str, clips: list[tuple[str, str, Path]], size: str | None,
          pause: float) -> dict:
    """Word error rate per language group, plus what it cost and what failed.

    A clip the provider refused is counted, not scored: sixteen clips with ten
    of them rate-limited away would otherwise produce a confident average of
    six. The count is printed beside the rates for exactly that reason.
    """
    rates: dict[str, list[float]] = {language: [] for language in LANGUAGES}
    held: list[str] = []
    started = time.monotonic()
    for index, (reference, language, path) in enumerate(clips):
        if pause and index:
            time.sleep(pause)
        try:
            heard = transcribe_with(provider, path.read_bytes(), size)
        except Exception as err:   # noqa: BLE001 - one bad clip must not end the run
            held.append(f"{path.name}: {type(err).__name__}: {err}")
            continue
        rates.setdefault(language, []).append(word_error_rate(reference, heard))
    return {
        "provider": provider,
        "size": size if provider == "whisper" else "-",
        "wer": {language: (round(sum(got) / len(got), 2) if got else None)
                for language, got in rates.items()},
        "clips_scored": sum(len(got) for got in rates.values()),
        "clips_held": len(held),
        "held": held,
        "seconds": round(time.monotonic() - started, 1),
    }


def drop_model(size: str) -> None:
    """Give one scored size's weights back before the next one loads.

    The same path app/stt.py takes between voice notes, driven the way
    measure_one drives it: backdate the last use past the idle window and let
    the eviction do the rest. Without this, scoring three sizes holds three
    sets of weights for the whole run.
    """
    from app import stt

    if size not in stt._models:
        return
    stt._last_used[size] = time.monotonic() - (stt.idle_window_seconds() + 60)
    with stt._lock:
        stt._evict_idle(time.monotonic())


def print_scores(rows: list[dict]) -> None:
    languages = [language for language in LANGUAGES
                 if any(row["wer"].get(language) is not None for row in rows)]
    head = f"{'provider':<9} {'size':<6}" + "".join(f" {language:>10}" for language in languages) \
        + f" {'scored':>7} {'held':>5} {'seconds':>8}"
    print(head)
    print("-" * len(head))
    for row in rows:
        cells = "".join(f" {row['wer'][language]:>10}" if row["wer"].get(language) is not None
                        else f" {'-':>10}" for language in languages)
        print(f"{row['provider']:<9} {row['size']:<6}{cells} "
              f"{row['clips_scored']:>7} {row['clips_held']:>5} {row['seconds']:>8}")
    print("\n(word error rate, lower is better; above 1.0 means more errors than words)")
    for row in rows:
        for line in row["held"]:
            print(f"held  {row['provider']}/{row['size']}  {line}")


def run_scoring(args: argparse.Namespace) -> int:
    providers = ["whisper", "gemini"] if args.score == "both" else [args.score]
    if "gemini" in providers:
        from app import stt_gemini

        # Checked before anything is synthesised: nobody should build sixteen
        # clips and then be told the key is missing.
        if not stt_gemini.api_key():
            raise SystemExit("--score gemini needs GEMINI_API_KEY in the environment")

    clips_dir = Path(args.clips) if args.clips else CLIPS_DIR
    if args.refs:
        clips = load_corpus(clips_dir, Path(args.refs))
    elif args.clips:
        raise SystemExit("--clips needs --refs: a clip with no reference cannot be scored")
    else:
        clips = build_corpus(clips_dir, args.voice)
    print(f"{len(clips)} clips from {clips_dir}\n")

    # One size unless asked otherwise: the memory mode's three-size default is
    # about memory, and here each extra size is another set of weights resident
    # in this one process for the rest of the run.
    sizes = args.sizes or [os.getenv("WHISPER_MODEL_SIZE", "base")]
    rows = []
    for provider in providers:
        if provider == "gemini":
            rows.append(score(provider, clips, None, args.pause))
            continue
        for size in sizes:
            rows.append(score(provider, clips, size, args.pause))
            drop_model(size)
    print_scores(rows)
    print(json.dumps(rows))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    # No default here: the two modes want different ones, and argparse cannot
    # tell "the default" from "the same thing, typed".
    ap.add_argument("--sizes", nargs="+")
    ap.add_argument("--audio", nargs="*", default=[])
    ap.add_argument("--score", choices=["whisper", "gemini", "both"],
                    help="score word error rate instead of measuring memory")
    ap.add_argument("--clips", help="directory of your own recordings (needs --refs)")
    ap.add_argument("--refs", help="filename<TAB>language<TAB>reference, one clip per line")
    ap.add_argument("--voice", help="macOS `say` voice for the synthesised corpus")
    ap.add_argument("--pause", type=float, default=0.0,
                    help="seconds between clips; try 4 if the free tier rate-limits you")
    ap.add_argument("--only", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.only:
        print(json.dumps(measure_one(args.only, args.audio)))
        return 0
    if args.score:
        return run_scoring(args)

    repo = Path(__file__).resolve().parent.parent
    rows = []
    for size in args.sizes or ["tiny", "base", "small"]:
        done = subprocess.run([sys.executable, __file__, "--only", size, "--audio", *args.audio],
                              cwd=repo, capture_output=True, text=True)
        if done.returncode != 0:
            print(f"{size}: failed\n{done.stderr}", file=sys.stderr)
            return 1
        rows.append(json.loads(done.stdout.strip().splitlines()[-1]))

    head = f"{'size':<6} {'on disk':>9} {'load cost':>10} {'after use':>10} {'freed':>8} {'load s':>7} {'transcribe s':>13}"
    print(head)
    print("-" * len(head))
    for r in rows:
        secs = ", ".join(str(s) for s in r["transcribe_seconds"]) or "-"
        print(f"{r['size']:<6} {r['weights_on_disk_mib']:>8} {r['load_cost_mib']:>9} "
              f"{r['rss_after_transcribing_mib']:>9} {r['returned_by_eviction_mib']:>7} "
              f"{r['load_seconds']:>7} {secs:>13}")
    print(f"\n(MiB, measured on {rows[0]['platform']}; 'after use' is total process RSS)")
    print(json.dumps(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
