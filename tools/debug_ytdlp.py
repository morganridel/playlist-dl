#!/usr/bin/env python3
import argparse
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict

from yt_dlp import YoutubeDL


def format_bytes(n: float) -> str:
    if not n or n <= 0:
        return "0 B"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    if i == 0:
        return f"{int(v)} {units[i]}"
    return f"{v:.1f} {units[i]}"


def format_eta(seconds: float) -> str:
    if seconds is None or seconds < 0:
        return "?"
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Debug yt-dlp download with the same options as the web app.")
    ap.add_argument("url")
    ap.add_argument("--out-dir", default="./_debug_ytdlp", help="Directory to write downloaded files")
    ap.add_argument("--cookies", default=os.environ.get("YTDLP_COOKIES", ""), help="Path to cookies.txt (optional)")
    args = ap.parse_args()

    ffmpeg_bin = os.environ.get("FFMPEG_BIN", "ffmpeg")
    if Path(ffmpeg_bin).exists():
        if not os.access(ffmpeg_bin, os.X_OK):
            print(f"FFMPEG_BIN is not executable: {ffmpeg_bin}", file=sys.stderr)
            return 2
    else:
        if not shutil.which(ffmpeg_bin):
            print(f"ffmpeg not found (set FFMPEG_BIN or install ffmpeg): {ffmpeg_bin}", file=sys.stderr)
            return 2

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    last_line_ts = 0.0

    def hook(d: Dict[str, Any]) -> None:
        nonlocal last_line_ts
        status = d.get("status")
        now = time.time()
        if status == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            speed = d.get("speed") or 0
            eta = d.get("eta")
            if now - last_line_ts >= 0.25:
                last_line_ts = now
                if total:
                    pct = 100.0 * (downloaded / total)
                    msg = f"{pct:6.2f}%  {format_bytes(downloaded)} / {format_bytes(total)}  {format_bytes(speed)}/s  eta {format_eta(eta)}"
                else:
                    msg = f"??%     {format_bytes(downloaded)} / ?           {format_bytes(speed)}/s  eta {format_eta(eta)}"
                print(msg, flush=True)
        elif status == "finished":
            print("download finished", flush=True)
        elif status == "error":
            print("download error", flush=True)

    ydl_opts: Dict[str, Any] = {
        "outtmpl": str(out_dir / "source.%(ext)s"),
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": False,
        "verbose": True,
        "writethumbnail": True,
        "writeinfojson": True,
        "progress_hooks": [hook],
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
    }
    if args.cookies:
        ydl_opts["cookiefile"] = args.cookies

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(args.url, download=True)
        print(f"title: {info.get('title')}")
        print(f"uploader: {info.get('uploader') or info.get('channel')}")
        print(f"duration: {info.get('duration')}")
        print(f"chapters: {len(info.get('chapters') or [])}")

    print(f"wrote files to: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

