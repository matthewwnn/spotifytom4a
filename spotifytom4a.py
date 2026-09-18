"""Exportify CSV to M4A, with best-effort matching and optional lyrics."""
import argparse
import csv
import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CSV_FILE = Path.home() / "CSV_FILE_DIR_HERE"
OUTPUT_DIR = Path.home() / "OUTPUT_DIR_HERE"
MAX_WORKERS = 2
VERSIONS = ("live", "remix", "acoustic", "instrumental", "cover", "slowed", "reverb",
            "sped up", "nightcore", "radio edit", "clean", "censored", "karaoke", "extended")


def norm(text):
    text = unicodedata.normalize("NFKD", text or "").casefold()
    return re.sub(r"[^\w]+", " ", "".join(c for c in text if not unicodedata.combining(c))).strip()


def core_title(title):
    title = re.sub(r"[([][^)\]]*\b(?:feat|ft|with|featuring)\b[^)\]]*[)\]]", "", title, flags=re.I)
    return re.split(r"\s+(?:feat\.?|ft\.?|featuring)\s+", title, flags=re.I)[0].strip()


def artists(row):
    return [a.strip() for a in row["Artist Name(s)"].split(";") if a.strip()]


def duration(row):
    try:
        value = float(row.get("Duration (ms)") or row.get("Track Duration (ms)") or 0) / 1000
        return value if value > 0 else None
    except (ValueError, TypeError):
        return None


def contains(needle, text):
    return bool(needle) and f" {needle} " in f" {text} "


def similarity(a, b):
    return (1.0 if contains(a, b) else SequenceMatcher(None, a, b).ratio()) if a and b else 0.0


def rank(info, row):
    title = norm(info.get("title"))
    wanted = norm(core_title(row["Track Name"]))
    channel = norm(info.get("channel") or info.get("uploader"))
    evidence = " ".join([title, channel, norm(info.get("artist")), norm(info.get("creator"))])
    tm = max(similarity(wanted, title), similarity(wanted, norm(info.get("track"))))
    am = [contains(norm(a), evidence) for a in artists(row)]
    artist_score = (1 if am[0] else .65) if any(am) else 0
    score = 50 * tm + 25 * artist_score
    reasons = [f"title={tm:.2f}", f"artist={artist_score:.2f}"]
    delta = None
    actual, expected = info.get("duration"), duration(row)
    if isinstance(actual, (int, float)) and actual > 0 and expected:
        delta = abs(actual - expected)
        score += 15 if delta <= 3 else 10 if delta <= 8 else 4 if delta <= 15 else -min(25, delta / expected * 60)
        reasons.append(f"duration difference={delta:.1f}s")
    else:
        reasons.append("duration unknown")
    score += 5 if channel.endswith(" topic") else 0
    score += 3 if contains("official audio", title) else 0
    extras = [v for v in VERSIONS if contains(v, title) and not contains(v, norm(row["Track Name"]))]
    missing = [v for v in VERSIONS if contains(v, norm(row["Track Name"])) and not contains(v, title)]
    if missing:
        score -= 10 * len(missing)
        reasons.append("requested version not labeled: " + ", ".join(missing))
    for version in extras:
        score -= 18 if version in {"cover", "karaoke", "instrumental"} else 10
    if re.search(r"\breaction\b|\btutorial\b|\bone hour\b|\b1 hour\b", title):
        score -= 35
        extras.append("non-song/compilation")
    if extras:
        reasons.append("extra versions: " + ", ".join(extras))
    if str(row.get("Explicit", row.get("Explicit?", ""))).lower() == "true" and set(extras) & {"clean", "censored"}:
        score -= 8
    strong = tm >= .85 and artist_score >= .65 and delta is not None and delta <= 8 and not extras and not missing and score >= 80
    return {**info, "score": round(score, 2), "confidence": "strong" if strong else "uncertain",
            "duration_difference": round(delta, 2) if delta is not None else None, "reasons": "; ".join(reasons)}


def run(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if p.stderr:
        logging.info("Tool output: %s", p.stderr.strip())
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[-2500:] or f"Exit code {p.returncode}")
    return p.stdout


def base(args, source):
    cmd = ["yt-dlp", "--ignore-config", "--no-cache-dir", "--socket-timeout", "25", "--retries", "2",
           "--extractor-retries", "2", "--retry-sleep", "http:exp=1:8"]
    if args.cookies_browser and source == "youtube":
        cmd += ["--cookies-from-browser", args.cookies_browser]
    return cmd


def search(query, source, args):
    prefix = "ytsearch" if source == "youtube" else "scsearch"
    try:
        output = run(base(args, source) + ["--flat-playlist", "--dump-json", "--quiet", f"{prefix}{args.candidates}:{query}"])
        found = []
        for line in output.splitlines():
            if not line.strip():
                continue
            info = json.loads(line)
            if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
                continue
            url = info.get("webpage_url") or info.get("url")
            if source == "youtube" and info.get("id"):
                url = "https://www.youtube.com/watch?v=" + info["id"]
            if url and url.startswith("https://"):
                found.append({**info, "source": source, "candidate_url": url})
        return found
    except (RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        logging.warning("Search failed (%s, %s): %s", source, query, exc)
        return []


def ranked(candidates, row):
    unique = {c["candidate_url"]: c for c in candidates}
    return sorted((rank(c, row) for c in unique.values()), key=lambda c: c["score"], reverse=True)


def find_candidates(row, args):
    query = f"{core_title(row['Track Name'])} {' '.join(artists(row)[:2])}"
    found = ranked(search(query + " official audio", "youtube", args), row)
    if not found or found[0]["confidence"] == "uncertain":
        found += search(f"{core_title(row['Track Name'])} {artists(row)[0]}", "youtube", args)
        if not args.no_soundcloud:
            found += search(query, "soundcloud", args)
    return ranked(found, row)


def track_key(row):
    return row.get("Track URI", "").strip() or "|".join([row["Track Name"], row["Artist Name(s)"], row.get("Album Name", ""), str(duration(row))])


def output_path(row, directory):
    stem = f"{row['Track Name']} - {artists(row)[0]}"
    stem = re.sub(r'[\\/*?:"<>|\x00-\x1f%]', "_", stem).strip(" .")[:130].rstrip(" .")
    identity = hashlib.sha256(track_key(row).encode()).hexdigest()[:10]
    return directory / f"{stem} [{identity}].m4a"


def valid_audio(path):
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        info = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                               "stream=codec_type:format=duration", "-of", "json", str(path)], 30))
        return bool(info.get("streams")) and float(info.get("format", {}).get("duration", 0)) > 0
    except (RuntimeError, ValueError, subprocess.TimeoutExpired):
        return False


def download(candidate, row, destination, args):
    with tempfile.TemporaryDirectory(prefix="spotifytom4a-") as temp:
        audio = Path(temp) / "audio.m4a"
        cmd = base(args, candidate["source"]) + ["--no-playlist", "-f", "bestaudio[ext=m4a]/bestaudio",
            "--extract-audio", "--audio-format", "m4a", "--concurrent-fragments", "1", "--fragment-retries", "2",
            "--abort-on-unavailable-fragments", "--match-filter", "!is_live", "--embed-thumbnail", "--add-metadata",
            "--no-progress", "-o", str(Path(temp) / "audio.%(ext)s"), candidate["candidate_url"]]
        try:
            run(cmd, 600)
        except RuntimeError as exc:
            # Keep audio only for an identifiable optional postprocessing failure.
            if "Postprocessing" not in str(exc) or not valid_audio(audio):
                raise
            logging.warning("Preserving audio despite postprocessing failure: %s", exc)
        if not valid_audio(audio):
            raise RuntimeError("No valid audio output")
        tagged = Path(temp) / "tagged.m4a"
        try:
            run(["ffmpeg", "-y", "-v", "error", "-i", str(audio), "-map", "0", "-c", "copy",
                 "-metadata", "title=" + row["Track Name"], "-metadata", "artist=" + "; ".join(artists(row)),
                 "-metadata", "album=" + row.get("Album Name", ""),
                 "-metadata", "comment=Source: " + candidate["candidate_url"], str(tagged)])
            if valid_audio(tagged):
                audio = tagged
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            logging.warning("Could not apply Spotify tags: %s", exc)
        shutil.move(str(audio), str(destination))


def embed_lyrics(destination, text):
    """FFmpeg's MP4 muxer writes lyrics to the iTunes-compatible ©lyr atom."""
    with tempfile.TemporaryDirectory(prefix="spotifytom4a-lyrics-") as folder:
        output = Path(folder) / "tagged.m4a"
        # Stream copy preserves audio quality and existing artwork/metadata.
        run(["ffmpeg", "-y", "-v", "error", "-i", str(destination), "-map", "0", "-c", "copy",
             "-metadata", "lyrics=" + text, str(output)])
        info = json.loads(run(["ffprobe", "-v", "error", "-show_entries", "format_tags=lyrics", "-of", "json", str(output)]))
        if info.get("format", {}).get("tags", {}).get("lyrics") != text or not valid_audio(output):
            raise RuntimeError("Embedded lyrics verification failed; original audio preserved")
        shutil.move(str(output), str(destination))


def lyrics(row, destination):
    if not duration(row):
        return "skipped: missing duration"
    params = urlencode({"track_name": core_title(row["Track Name"]), "artist_name": artists(row)[0],
                        "album_name": row.get("Album Name", ""), "duration": round(duration(row))})
    try:
        req = Request("https://lrclib.net/api/get?" + params, headers={"User-Agent": "spotifytom4a/2.0", "Accept": "application/json"})
        with urlopen(req, timeout=12) as response:
            data = json.load(response)
        if data.get("instrumental"):
            return "instrumental"
        text = (data.get("plainLyrics") or "").strip()
        if not text and data.get("syncedLyrics"):
            text = re.sub(r"\[[^\]]*\]", "", data["syncedLyrics"]).strip()
        if not text:
            return "not found"
        embed_lyrics(destination, text)
        return "embedded"
    except Exception as exc:
        logging.info("Lyrics unavailable for %s: %s", row["Track Name"], exc)
        return "unavailable"


def process(row, args):
    path = output_path(row, args.output)
    result = {"track": row["Track Name"], "artist": row["Artist Name(s)"], "track_uri": row.get("Track URI", ""),
              "status": "failed", "file": str(path), "lyrics": "disabled"}
    if valid_audio(path):
        result["status"] = "already exists"
        if not args.no_lyrics:
            try:
                tags = json.loads(run(["ffprobe", "-v", "error", "-show_entries", "format_tags=lyrics", "-of", "json", str(path)]))
                has_lyrics = bool(tags.get("format", {}).get("tags", {}).get("lyrics"))
            except (RuntimeError, ValueError, subprocess.TimeoutExpired):
                has_lyrics = False
            result["lyrics"] = "already embedded" if has_lyrics else lyrics(row, path)
        return result
    print(f"Searching: {row['Track Name']}", flush=True)
    candidates = find_candidates(row, args)
    tried, errors = set(), []
    for pass_number in range(2):
        for c in candidates[:args.max_attempts]:
            if c["candidate_url"] in tried:
                continue
            tried.add(c["candidate_url"])
            try:
                download(c, row, path, args)
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
                errors.append(str(exc))
                logging.warning("Download failed %s: %s", c["candidate_url"], exc)
                continue
            result.update(status="downloaded", match={k: c.get(k) for k in
                          ("title", "source", "candidate_url", "score", "confidence", "duration_difference", "reasons")})
            if not args.no_lyrics:
                result["lyrics"] = lyrics(row, path)
            return result
        if args.no_soundcloud:
            break
        # Always give untried SoundCloud candidates a chance after YouTube failures.
        sc = [c for c in candidates if c["source"] == "soundcloud" and c["candidate_url"] not in tried]
        if not sc and pass_number == 0:
            sc = search(f"{core_title(row['Track Name'])} {artists(row)[0]}", "soundcloud", args)
        candidates = ranked(sc, row)
    result["error"] = errors[-1] if errors else "No usable search results"
    return result


def read_rows(path):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not {"Track Name", "Artist Name(s)"}.issubset(reader.fieldnames or []):
            raise ValueError("CSV must include Track Name and Artist Name(s)")
        rows, seen, duplicates = [], set(), 0
        for line, row in enumerate(reader, 2):
            if not row.get("Track Name", "").strip() or not row.get("Artist Name(s)", "").strip():
                raise ValueError(f"CSV row {line} is missing a track or artist")
            key = track_key(row)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            rows.append(row)
        return rows, duplicates


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", type=Path, default=CSV_FILE)
    p.add_argument("--output", type=Path, default=OUTPUT_DIR)
    p.add_argument("--workers", type=int, default=MAX_WORKERS)
    p.add_argument("--candidates", type=int, default=5)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--limit", type=int)
    p.add_argument("--cookies-browser", help="Optional, e.g. firefox")
    p.add_argument("--no-soundcloud", action="store_true")
    p.add_argument("--no-lyrics", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Search/rank only, no audio or lyrics downloads")
    args = p.parse_args()
    if min(args.workers, args.candidates, args.max_attempts) < 1 or (args.limit is not None and args.limit < 1):
        p.error("workers, candidates, max-attempts and limit must be positive")
    for program in (["yt-dlp"] if args.dry_run else ["yt-dlp", "ffmpeg", "ffprobe"]):
        if not shutil.which(program):
            p.error(f"{program} is not on PATH")
    try:
        rows, duplicates = read_rows(args.csv.expanduser())
    except (OSError, ValueError) as exc:
        p.error(str(exc))
    rows = rows[:args.limit] if args.limit else rows
    args.output = args.output.expanduser().resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    print(f"Processing {len(rows)} unique tracks; ignored {duplicates} duplicate entries.")
    def worker(row):
        if args.dry_run:
            return {"track": row["Track Name"], "status": "preview", "candidates":
                    [{k: c.get(k) for k in ("title", "source", "candidate_url", "score", "confidence", "duration_difference", "reasons")}
                     for c in find_candidates(row, args)]}
        return process(row, args)
    failures = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(worker, row): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                logging.exception("Unexpected error for %s", row["Track Name"])
                result = {"track": row["Track Name"], "status": "failed", "error": str(exc)}
            failures += result["status"] == "failed"
            if args.dry_run:
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            else:
                print(f"{result['status']}: {result['track']} | lyrics: {result.get('lyrics', 'unavailable')}", flush=True)
    print(f"Done: {len(rows) - failures} completed, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
