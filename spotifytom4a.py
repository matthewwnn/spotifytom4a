import csv
import json
import re
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

CSV_FILE = Path.home() / "CSV_FILE_DIR_HERE"
OUTPUT_DIR = Path.home() / "OUTPUT_DIR_HERE"

MAX_WORKERS = 5


BAD_VERSION_TERMS = [
    "clean",
    "clean version",
    "radio edit",
    "radio version",
    "censored",
    "no swearing",
    "instrumental",
    "karaoke",
    "cover",
    "live",
    "remix",
    "slowed",
    "reverb",
]

GOOD_VERSION_TERMS = [
    "official audio",
    "audio",
    "topic",
    "provided to youtube",
    "explicit",
]


def clean(text):
    return re.sub(r'[\\/*?:"<>|]', "", text)


def search_results(query, count=3):
    cmd = [
        "yt-dlp",
        f"ytsearch{count}:{query}",

        #get json search results
        "--dump-json",
        "--flat-playlist",

        #be quiet
        "--quiet",
        "--no-warnings",
    ]

    result = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True
    )

    results = []

    for line in result.stdout.splitlines():
        if line.strip():
            results.append(json.loads(line))

    return results


def score_result(info, artist, track):
    title = (info.get("title") or "").lower()
    uploader = (info.get("uploader") or "").lower()
    channel = (info.get("channel") or "").lower()

    artist_lower = artist.lower()
    track_lower = track.lower()

    score = 0

    #artist & trackname = good
    if artist_lower in title:
        score += 20

    if track_lower in title:
        score += 30

    #youtube topic channels are good
    if "topic" in uploader or "topic" in channel:
        score += 25

    for term in GOOD_VERSION_TERMS:
        if term in title:
            score += 10

    for term in BAD_VERSION_TERMS:
        if term in title:
            score -= 30

    return score


def download(row):
    track = row["Track Name"].strip()
    artist = row["Artist Name(s)"].strip()

    filename = clean(f"{artist} - {track}.%(ext)s")

    query = f'"{artist}" "{track}" "Official Audio" "Topic"'

    print(f"Searching: {artist} - {track}")

    try:
        results = search_results(query, count=5)
    except subprocess.CalledProcessError:
        print(f"FAILED SEARCH: {artist} - {track}")
        return

    if not results:
        print(f"NO RESULTS: {artist} - {track}")
        return

    best = max(
        results,
        key=lambda info: score_result(info, artist, track)
    )

    best_title = best.get("title", "Unknown title")
    best_id = best.get("id")

    if not best_id:
        print(f"NO VIDEO ID FOUND: {artist} - {track}")
        return
		
    video_url = f"https://www.youtube.com/watch?v={best_id}"

    print(f"Downloading: {artist} - {track}")
    print(f"Selected: {best_title}")

    cmd = [
        "yt-dlp",
        video_url,

        #best audio
        "-f", "bestaudio[ext=m4a]/bestaudio",

        #age verify
        "--cookies-from-browser", "firefox",

        #convert to m4a
        "--extract-audio",
        "--audio-format", "m4a",

        #metadata + thumbnail
        "--embed-thumbnail",
        "--add-metadata",

        #better matching
        "--match-filter", "!is_live",

        #faster download
        "--concurrent-fragments", "4",

        #avoid overwriting duplicate filenames
        "--no-overwrites",

        #output
        "-o", str(OUTPUT_DIR / filename),

        #don't spam console
        "--quiet",
        "--no-warnings",
    ]

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        print(f"FAILED DOWNLOAD: {artist} - {track}")


OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

with open(CSV_FILE, newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
    pool.map(download, rows)

print("Done.")
