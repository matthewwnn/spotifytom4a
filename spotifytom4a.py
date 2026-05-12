import csv
import re
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

CSV_FILE = Path.home() / "CSV_FILE_DIR_HERE"
OUTPUT_DIR = Path.home() / "OUTPUT_DIR_HERE"

MAX_WORKERS = 5


def clean(text):
    return re.sub(r'[\\/*?:"<>|]', "", text)


def download(row):
    track = row["Track Name"].strip()
    artist = row["Artist Name(s)"].strip()

    filename = clean(f"{artist} - {track}.%(ext)s")

    query = f"{artist} - {track} topic"

    cmd = [
        "yt-dlp",
        f"ytsearch1:{query}",

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

        #faster downloads
        "--concurrent-fragments", "4",

        #output
        "-o", str(OUTPUT_DIR / filename),

        #don't spam console
        "--quiet",
        "--no-warnings",
    ]

    print(f"Downloading: {artist} - {track}")

    try:
        subprocess.run(cmd, check=True)
    except:
        print(f"FAILED: {artist} - {track}")


with open(CSV_FILE, newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
    pool.map(download, rows)

print("Done.")
