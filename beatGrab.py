#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0"
)
SITE = "https://www.beatstars.com"
API = "https://main.v2.beatstars.com"
ALGOLIA_URL = (
    "https://nmmgzjq6qi-dsn.algolia.net/1/indexes/"
    "public_prod_inventory_track_index_bycustom/query"
    "?x-algolia-agent=Algolia%20for%20JavaScript%20(4.12.0)%3B%20Browser"
)
ALGOLIA_APP = "NMMGZJQ6QI"
ALGOLIA_KEY = "b3513eb709fe8f444b4d5c191b63ea47"
INVALID_FS_CHARS = re.compile(r'[<>:"/\\\\|?*\x00-\x1f]')
SCRIPT_DIR = Path(__file__).resolve().parent
INIT_MARKER = SCRIPT_DIR / ".beatgrab_initialized"
BEAT_URL_RE = re.compile(r"/beat/.*?-(\d+)/?$", re.I)
TRACK_ID_RE = re.compile(r"^(?:TK)?(\d+)$", re.I)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def default_headers(*, json_body: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": SITE,
        "Referer": SITE + "/",
        "Connection": "keep-alive",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
        headers["x-algolia-api-key"] = ALGOLIA_KEY
        headers["x-algolia-application-id"] = ALGOLIA_APP
    return headers


def http_request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60,
) -> tuple[bytes, str, str]:
    req = Request(
        url,
        data=data,
        headers=headers or default_headers(),
        method="POST" if data is not None else "GET",
    )
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        final = resp.geturl()
        return body, ctype, final


def http_json(url: str, *, data: dict[str, Any] | None = None) -> Any:
    raw = None
    headers = default_headers(json_body=data is not None)
    if data is not None:
        raw = json.dumps(data).encode("utf-8")
    body, _, _ = http_request(url, data=raw, headers=headers, timeout=45)
    return json.loads(body.decode("utf-8", errors="replace"))


def print_intro() -> None:
    print()
    print("=" * 40)
    print("              beatGrab")
    print("     BeatStars profile/beat grabber")
    print("     made by www.drumkits.site")
    print("=" * 40)
    print()
    print("Note: the site player uses HLS .ts splits;")
    print("this script downloads the full stream file")
    print("from BeatStars' stream API instead.")
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


def prompt_target() -> str:
    try:
        return input("Artist permalink or beat URL/id: ").strip()
    except EOFError:
        return ""


def sanitize_filename(name: str, track_id: int, used: set[str]) -> str:
    cleaned = INVALID_FS_CHARS.sub("", name).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = f"track-{track_id}"
    base = cleaned
    candidate = base
    if candidate.lower() in used:
        candidate = f"{base} [{track_id}]"
    used.add(candidate.lower())
    return candidate


def extract_track_id(value: str) -> str | None:
    value = value.strip()
    m = BEAT_URL_RE.search(urlparse(value).path if "://" in value else value)
    if m:
        return m.group(1)
    m = TRACK_ID_RE.match(value)
    if m:
        return m.group(1)
    if "beatstars.com/beat/" in value:
        m = BEAT_URL_RE.search(value)
        if m:
            return m.group(1)
    return None


def normalize_permalink(value: str) -> str:
    value = value.strip().strip("/")
    if "://" in value:
        path = urlparse(value).path.strip("/")
        parts = [p for p in path.split("/") if p]
        if parts and parts[0] != "beat":
            return parts[0]
        return parts[-1] if parts else ""
    return value.split("/")[0]


def get_track(track_id: str | int) -> dict[str, Any]:
    data = http_json(f"{API}/beat?id={track_id}&fields=details")
    details = data.get("response", {}).get("data", {}).get("details") or {}
    if not details.get("track_id"):
        raise SystemExit(f"Track not found: {track_id}")
    musician = details.get("musician") or {}
    artwork = details.get("artwork") or {}
    return {
        "id": int(details["track_id"]),
        "title": (details.get("title") or f"track-{track_id}").strip(),
        "artist": (musician.get("display_name") or "unknown").strip(),
        "permalink": (musician.get("permalink") or "unknown").strip(),
        "bpm": details.get("bpm"),
        "stream_url": details.get("stream_url")
        or details.get("stream_ssl_url")
        or f"{API}/stream?id={track_id}&return=audio",
        "hls_url": details.get("stream_hls_url"),
        "artwork": artwork.get("original") or artwork.get("default"),
        "duration": details.get("duration"),
    }


def get_artist_user_id(permalink: str) -> int:
    data = http_json(f"{API}/musician?permalink={permalink}")
    profile = data.get("response", {}).get("data", {}).get("profile") or {}
    user_id = profile.get("user_id")
    if not user_id:
        raise SystemExit(f"Artist not found: {permalink}")
    return int(user_id)


def get_artist_tracks(permalink: str) -> list[dict[str, Any]]:
    user_id = get_artist_user_id(permalink)
    member_id = f"MR{user_id}"
    tracks: list[dict[str, Any]] = []
    page = 0
    while True:
        payload = {
            "query": "",
            "page": page,
            "hitsPerPage": 1000,
            "facets": ["*"],
            "analytics": False,
            "tagFilters": [],
            "facetFilters": [[f"profile.memberId:{member_id}"]],
            "maxValuesPerFacet": 1000,
            "enableABTest": False,
            "userToken": None,
            "filters": "",
            "ruleContexts": [],
        }
        data = http_json(ALGOLIA_URL, data=payload)
        hits = data.get("hits") or []
        for hit in hits:
            track_id = int(hit.get("v2Id") or 0)
            if not track_id:
                continue
            meta = hit.get("metadata") or {}
            art = ((hit.get("artwork") or {}).get("sizes") or {})
            tracks.append(
                {
                    "id": track_id,
                    "title": (hit.get("title") or f"track-{track_id}").strip(),
                    "artist": (meta.get("artistName") or permalink).strip(),
                    "permalink": permalink,
                    "bpm": meta.get("bpm"),
                    "stream_url": f"{API}/stream?id={track_id}&return=audio",
                    "hls_url": None,
                    "artwork": art.get("original"),
                    "duration": None,
                }
            )
        nb_pages = int(data.get("nbPages") or 1)
        page += 1
        if page >= nb_pages:
            break
    return tracks


def sniff_extension(data: bytes, ctype: str, final_url: str) -> str:
    low = (ctype or "").lower()
    path = urlparse(final_url).path.lower()
    if data.startswith(b"ID3") or "mpeg" in low or "mp3" in low or path.endswith(".mp3"):
        return ".mp3"
    if data.startswith(b"RIFF") or "wav" in low or path.endswith(".wav"):
        return ".wav"
    if data[:4] == b"fLaC" or "flac" in low:
        return ".flac"
    if "mp4" in low or "m4a" in low or path.endswith(".m4a"):
        return ".m4a"
    return ".mp3"


def download_track(
    track: dict[str, Any],
    *,
    out_dir: Path,
    base_name: str,
    skip_existing: bool,
) -> str:
    existing = list(out_dir.glob(base_name + ".*"))
    if skip_existing and existing and existing[0].stat().st_size > 0:
        return f"skip  {existing[0].name}"

    url = track["stream_url"]
    try:
        data, ctype, final = http_request(
            url,
            headers=default_headers(),
            timeout=180,
        )
    except (HTTPError, URLError, TimeoutError) as exc:
        return f"FAIL  {base_name} ({exc})"

    if len(data) < 1000:
        return f"FAIL  {base_name} (response too small: {len(data)} bytes)"

    ext = sniff_extension(data, ctype, final)
    dest = out_dir / f"{base_name}{ext}"
    dest.write_bytes(data)
    kb = len(data) / 1024
    return f"ok    {dest.name} ({kb:.0f} KiB)"


def run_downloads(
    tracks: list[dict[str, Any]],
    *,
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
        base_name = sanitize_filename(track["title"], track["id"], used_names)
        planned.append((i, track, base_name))

    ok = fail = skipped = 0
    total = len(planned)

    def _job(item: tuple[int, dict[str, Any], str]) -> tuple[int, str, str]:
        idx, track, base_name = item
        result = download_track(
            track,
            out_dir=out_dir,
            base_name=base_name,
            skip_existing=skip_existing,
        )
        return idx, track["title"], result

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


def print_track_preview(tracks: list[dict[str, Any]]) -> None:
    print(f"\nTracks found ({len(tracks)}):\n")
    width = len(str(len(tracks)))
    for i, track in enumerate(tracks, 1):
        bpm = track.get("bpm")
        bpm_label = f"[{bpm} bpm]" if bpm not in (None, "", 0) else "[-- bpm]"
        print(f"  {i:>{width}}. {bpm_label:<12} {track['title']}")
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
        print(f"Selected [{track_num}] {tracks[track_num - 1]['title']}\n")
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
        print(f"Selected [{idx}] {tracks[idx - 1]['title']}\n")
        return [tracks[idx - 1]]

    return tracks


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download streamable audio from BeatStars artists/beats."
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="Artist permalink, beat URL, or track id",
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
    jobs = args.jobs if args.jobs > 0 else 8

    cli_target = (args.target or "").strip()
    interactive_entry = not cli_target

    if interactive_entry:
        print_intro()
        if is_first_run():
            if not prompt_first_run():
                print("Bye.")
                return 0
        target = prompt_target()
    else:
        target = cli_target

    if not target:
        print("Artist permalink or beat URL/id required.", file=sys.stderr)
        return 2

    track_id = extract_track_id(target)
    try:
        if track_id:
            print(f"Fetching beat {track_id} ...")
            track = get_track(track_id)
            folder = track["permalink"] or "beats"
            out_dir = args.output / folder
            print(f"Found: {track['title']} — {track['artist']}")
            return run_downloads(
                [track],
                out_dir=out_dir,
                skip_existing=not args.no_skip,
                jobs=jobs,
            )

        permalink = normalize_permalink(target)
        print(f"Fetching artist @{permalink} ...")
        tracks = get_artist_tracks(permalink)
        if not tracks:
            print("No tracks found for this artist.")
            return 1
        print(f"Found {len(tracks)} track(s).")

        selected = resolve_selection(
            tracks,
            track_num=args.track,
            want_all=args.all,
            interactive=interactive_entry
            or (not args.all and args.track is None and sys.stdin.isatty()),
        )
        if not selected:
            return 1 if args.track is not None else 0

        return run_downloads(
            selected,
            out_dir=args.output / permalink,
            skip_existing=not args.no_skip,
            jobs=jobs,
        )
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
