#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)
SITE = "https://traktrain.com"
INVALID_FS_CHARS = re.compile(r'[<>:"/\\\\|?*\x00-\x1f]')
SCRIPT_DIR = Path(__file__).resolve().parent
INIT_MARKER = SCRIPT_DIR / ".trakgrab_initialized"


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def http_get(url: str, *, referer: str | None = None, timeout: float = 60) -> bytes:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_profile_html(slug: str) -> str:
    url = f"{SITE}/{slug}"
    try:
        return http_get(url, referer=SITE + "/").decode("utf-8", errors="replace")
    except HTTPError as exc:
        if exc.code == 404:
            raise SystemExit(f"Artist not found: {SITE}/{slug}") from exc
        raise SystemExit(f"HTTP {exc.code} fetching {url}") from exc
    except URLError as exc:
        raise SystemExit(f"Failed to reach Traktrain: {exc.reason}") from exc


def extract_aws_base(html: str) -> str:
    match = re.search(r"AWS_BASE_URL\s*=\s*['\"]([^'\"]+)['\"]", html)
    if not match:
        raise SystemExit("Could not find AWS_BASE_URL on the profile page (site layout may have changed).")
    return match.group(1)


def parse_tracks(html: str, slug: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    tracks: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    for el in soup.find_all(attrs={"data-player-info": True}):
        raw = el.get("data-player-info")
        if not raw:
            continue
        try:
            info = json.loads(unescape(raw))
        except json.JSONDecodeError:
            continue

        producer = (info.get("producerLink") or "").strip("/")
        if producer != slug:
            continue

        src = info.get("src")
        name = (info.get("name") or "").strip()
        track_id = info.get("id")
        if not src or not name or track_id is None:
            continue
        if track_id in seen_ids:
            continue
        seen_ids.add(track_id)

        tracks.append(
            {
                "id": track_id,
                "name": name,
                "src": src,
                "bpm": info.get("bpm"),
            }
        )

    return tracks


def sanitize_filename(name: str, track_id: int, used: set[str]) -> str:
    cleaned = INVALID_FS_CHARS.sub("", name).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = f"track-{track_id}"

    base = cleaned
    candidate = f"{base}.mp3"
    if candidate.lower() in used:
        candidate = f"{base} [{track_id}].mp3"
    used.add(candidate.lower())
    return candidate


def download_track(
    track: dict[str, Any],
    *,
    base_url: str,
    out_dir: Path,
    filename: str,
    skip_existing: bool,
) -> str:
    dest = out_dir / filename

    if skip_existing and dest.exists() and dest.stat().st_size > 0:
        return f"skip  {filename}"

    song_url = base_url + track["src"]
    try:
        data = http_get(song_url, referer=SITE + "/", timeout=120)
    except (HTTPError, URLError, TimeoutError) as exc:
        return f"FAIL  {filename} ({exc})"

    if len(data) < 1000:
        return f"FAIL  {filename} (response too small: {len(data)} bytes)"

    dest.write_bytes(data)
    kb = len(data) / 1024
    return f"ok    {filename} ({kb:.0f} KiB)"


def run_downloads(
    tracks: list[dict[str, Any]],
    *,
    base_url: str,
    out_dir: Path,
    skip_existing: bool,
    jobs: int,
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Saving to {out_dir.resolve()}")
    workers = max(1, min(jobs, len(tracks)))
    print(f"Downloading {len(tracks)} track(s) with {workers} worker(s)\n")

    used_names: set[str] = set()
    planned: list[tuple[int, dict[str, Any], str]] = []
    for i, track in enumerate(tracks, 1):
        filename = sanitize_filename(track["name"], track["id"], used_names)
        planned.append((i, track, filename))

    ok = fail = skipped = 0
    total = len(planned)

    def _job(item: tuple[int, dict[str, Any], str]) -> tuple[int, str, str]:
        idx, track, filename = item
        result = download_track(
            track,
            base_url=base_url,
            out_dir=out_dir,
            filename=filename,
            skip_existing=skip_existing,
        )
        return idx, track["name"], result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_job, item) for item in planned]
        for fut in as_completed(futures):
            idx, label, result = fut.result()
            print(f"[{idx}/{total}] {label}")
            print(f"  {result}")
            if result.startswith("ok"):
                ok += 1
            elif result.startswith("skip"):
                skipped += 1
            else:
                fail += 1

    print(f"\nDone. downloaded={ok} skipped={skipped} failed={fail}")
    return 1 if fail else 0


def print_intro() -> None:
    print()
    print("=" * 40)
    print("              trakGrab")
    print("     Traktrain MP3 grabber")
    print("     made by www.drumkits.site")
    print("=" * 40)
    print()


def is_first_run() -> bool:
    return not INIT_MARKER.exists()


def mark_initialized() -> None:
    INIT_MARKER.write_text("1\n", encoding="utf-8")


def prompt_first_run() -> bool:
    while True:
        try:
            answer = input("Continue? [Y/N]: ").strip().lower()
        except EOFError:
            return False
        if answer in ("y", "yes"):
            mark_initialized()
            print()
            return True
        if answer in ("n", "no"):
            return False
        print("Please enter Y or N.")


def prompt_artist() -> str:
    try:
        raw = input("What is the artist name? traktrain.com/").strip().strip("/")
    except EOFError:
        return ""
    return raw


def normalize_artist(raw: str) -> str:
    artist = raw.strip().strip("/")
    if "/" in artist or "traktrain.com" in artist:
        artist = artist.rstrip("/").split("/")[-1]
    return artist


def print_track_preview(tracks: list[dict[str, Any]]) -> None:
    print(f"\nTracks on this profile ({len(tracks)}):\n")
    width = len(str(len(tracks)))
    for i, track in enumerate(tracks, 1):
        bpm = track.get("bpm")
        bpm_label = f"[{bpm} bpm]" if bpm not in (None, "", 0) else "[-- bpm]"
        print(f"  {i:>{width}}. {bpm_label:<12} {track['name']}")
    print()


def prompt_mode() -> str | None:
    print("Download mode:")
    print("  [1] All tracks")
    print("  [2] Single track")
    while True:
        try:
            choice = input("> ").strip().lower()
        except EOFError:
            return None
        if choice in ("1", "all", "a"):
            return "all"
        if choice in ("2", "single", "s"):
            return "single"
        if choice in ("", "q", "quit", "exit"):
            return None
        print("Enter 1 for all tracks, or 2 for a single track.")


def prompt_track_index(tracks: list[dict[str, Any]]) -> int | None:
    print_track_preview(tracks)
    upper = len(tracks)
    while True:
        try:
            raw = input(f"Enter track number (1-{upper}): ").strip().lower()
        except EOFError:
            return None
        if raw in ("", "q", "quit", "exit"):
            return None
        try:
            num = int(raw)
        except ValueError:
            print(f"Enter a number from 1 to {upper}.")
            continue
        if 1 <= num <= upper:
            return num
        print(f"Enter a number from 1 to {upper}.")


def resolve_selection(
    tracks: list[dict[str, Any]],
    *,
    track_num: int | None,
    want_all: bool,
    interactive: bool,
) -> list[dict[str, Any]] | None:
    if track_num is not None:
        if not (1 <= track_num <= len(tracks)):
            print(
                f"Track number {track_num} is out of range (1-{len(tracks)}).",
                file=sys.stderr,
            )
            return None
        print_track_preview(tracks)
        print(f"Selected [{track_num}] {tracks[track_num - 1]['name']}\n")
        return [tracks[track_num - 1]]

    if want_all:
        return tracks

    if interactive and sys.stdin.isatty():
        mode = prompt_mode()
        if mode is None:
            print("Cancelled.")
            return None
        if mode == "all":
            return tracks
        idx = prompt_track_index(tracks)
        if idx is None:
            print("Cancelled.")
            return None
        print(f"Selected [{idx}] {tracks[idx - 1]['name']}\n")
        return [tracks[idx - 1]]

    return tracks


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download streamable MP3s from a Traktrain producer profile."
    )
    parser.add_argument(
        "artist",
        nargs="?",
        help="Producer slug from traktrain.com/<slug>",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("_output"),
        help="Output directory (default: ./_output)",
    )
    parser.add_argument(
        "--no-skip",
        action="store_true",
        help="Re-download even if the file already exists",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Download all tracks (skip mode menu)",
    )
    parser.add_argument(
        "-t",
        "--track",
        type=int,
        metavar="N",
        help="Download a single track by 1-based list number",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=8,
        metavar="N",
        help="Parallel download workers (default: 8)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = parse_args(argv)

    cli_artist = normalize_artist(args.artist or "")
    interactive_entry = not cli_artist

    if interactive_entry:
        print_intro()
        if is_first_run():
            if not prompt_first_run():
                print("Bye.")
                return 0
        artist = normalize_artist(prompt_artist())
    else:
        artist = cli_artist

    if not artist:
        print("Artist slug required.", file=sys.stderr)
        return 2

    print(f"Connecting to {SITE}/{artist} ...")
    html = fetch_profile_html(artist)
    base_url = extract_aws_base(html)
    tracks = parse_tracks(html, artist)

    if not tracks:
        print("No tracks found for this producer on the profile page.")
        return 1

    print(f"Found {len(tracks)} track(s).")

    selected = resolve_selection(
        tracks,
        track_num=args.track,
        want_all=args.all,
        interactive=interactive_entry or (not args.all and args.track is None and sys.stdin.isatty()),
    )
    if not selected:
        return 1 if args.track is not None else 0

    jobs = args.jobs if args.jobs > 0 else 8
    return run_downloads(
        selected,
        base_url=base_url,
        out_dir=args.output / artist,
        skip_existing=not args.no_skip,
        jobs=jobs,
    )


if __name__ == "__main__":
    raise SystemExit(main())
