import asyncio
import os
import queue
import secrets
import shutil
import tempfile
import threading
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import multiprocessing as mp

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from yt_dlp import YoutubeDL


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    FINISHED = "finished"
    ERROR = "error"


@dataclass
class Job:
    id: str
    created_at: float = field(default_factory=time.time)
    status: JobStatus = JobStatus.QUEUED
    url: str = ""
    mode: str = "store"  # store | download
    manual_timestamps: str = ""
    album_override: str = ""
    artist_override: str = ""

    step: str = "queued"
    progress: float = 0.0  # 0..1, coarse
    events: List[str] = field(default_factory=list)
    ytdlp: Dict[str, Any] = field(default_factory=dict)
    _last_ytdlp_log_ts: float = 0.0
    _last_ytdlp_update_ts: float = 0.0

    output_dir: Optional[str] = None
    zip_path: Optional[str] = None
    error: Optional[str] = None


JOBS: Dict[str, Job] = {}
JOBS_LOCK = threading.Lock()

ALLOWED_HOSTS = {
    "youtube.com",
    "youtu.be",
    "music.youtube.com",
}


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _now_ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _safe_filename(name: str, max_len: int = 180) -> str:
    # Keep ASCII-ish filenames predictable across platforms (roughly similar to the bash script).
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = name.strip().replace("\n", " ").replace("\r", " ")
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f")
    for bad in ['\\', '/', ':', '*', '?', '"', "<", ">", "|"]:
        name = name.replace(bad, "")
    name = name.strip().strip(".")
    if not name:
        name = "untitled"
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name


def _parse_hms_to_seconds(s: str) -> float:
    s = s.strip()
    if not s:
        raise ValueError("empty timestamp")
    parts = s.split(":")
    if len(parts) == 2:
        mm, ss = parts
        hh = "0"
    elif len(parts) == 3:
        hh, mm, ss = parts
    else:
        raise ValueError(f"invalid timestamp: {s}")
    return int(hh) * 3600 + int(mm) * 60 + float(ss)


def _parse_manual_timestamps(text: str) -> List[Tuple[float, str]]:
    # Accept common formats:
    # - "00:00 Title"
    # - "00:00 | Title"
    # - "0:00 - Title"
    items: List[Tuple[float, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ["|", "-", "\t"]:
            if sep in line:
                left, right = line.split(sep, 1)
                t = left.strip()
                title = right.strip()
                break
        else:
            # First "word" is time, rest is title
            parts = line.split(None, 1)
            if len(parts) != 2:
                raise ValueError(f"invalid line (expected 'TIME TITLE'): {raw}")
            t, title = parts[0].strip(), parts[1].strip()
        items.append((_parse_hms_to_seconds(t), title))

    if not items:
        raise ValueError("no timestamps provided")

    items.sort(key=lambda x: x[0])
    # De-dupe identical times by keeping the last title.
    dedup: Dict[float, str] = {}
    for t, title in items:
        dedup[t] = title
    return sorted(dedup.items(), key=lambda x: x[0])


def _pick_downloaded_files(work_dir: Path) -> Tuple[Path, Optional[Path]]:
    # yt-dlp uses the outtmpl base for multiple related artifacts.
    source_candidates = sorted(work_dir.glob("source.*"))
    thumb = None
    non_images = [p for p in source_candidates if p.suffix.lower() not in [".jpg", ".jpeg", ".png", ".webp"]]
    if non_images:
        source = max(non_images, key=lambda p: p.stat().st_size)
    else:
        raise RuntimeError("could not find downloaded source audio in work dir")

    images = [p for p in source_candidates if p.suffix.lower() in [".jpg", ".jpeg", ".png", ".webp"]]
    if images:
        thumb = max(images, key=lambda p: p.stat().st_size)
    return source, thumb


def _ensure_cover_jpg(ffmpeg_bin: str, cover_path: Path, out_dir: Path, timeout_s: float) -> Path:
    if cover_path.suffix.lower() in [".jpg", ".jpeg"]:
        return cover_path
    out_path = out_dir / "cover.jpg"
    # Convert whatever thumbnail format yt-dlp gave us (often webp) into something MP4 likes.
    import subprocess

    subprocess.run(
        [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-i", str(cover_path), str(out_path)],
        check=True,
        timeout=max(1, int(timeout_s)),
    )
    return out_path


def _extract_chapters(info: Dict[str, Any], manual: str) -> List[Dict[str, Any]]:
    duration = info.get("duration")
    if duration is None:
        raise RuntimeError("could not determine video duration")

    if manual and manual.strip():
        items = _parse_manual_timestamps(manual)
        chapters = []
        for idx, (start, title) in enumerate(items):
            end = items[idx + 1][0] if idx + 1 < len(items) else float(duration)
            chapters.append({"start_time": float(start), "end_time": float(end), "title": title})
        return chapters

    chapters = info.get("chapters") or []
    if not chapters:
        raise RuntimeError("no chapters found; provide manual timestamps")

    normalized = []
    for idx, ch in enumerate(chapters):
        start = float(ch.get("start_time") or 0.0)
        end = ch.get("end_time")
        if end is None:
            end = chapters[idx + 1]["start_time"] if idx + 1 < len(chapters) else float(duration)
        normalized.append({"start_time": float(start), "end_time": float(end), "title": ch.get("title") or f"Track {idx+1}"})
    return normalized


def _job_event(job_id: str, msg: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.events.append(msg)
        # Keep memory bounded.
        if len(job.events) > 300:
            job.events = job.events[-300:]


def _job_update(job_id: str, *, step: Optional[str] = None, progress: Optional[float] = None) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        if step is not None:
            job.step = step
        if progress is not None:
            job.progress = max(0.0, min(1.0, float(progress)))


def _job_ytdlp_update(job_id: str, data: Dict[str, Any]) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.ytdlp = data
        job._last_ytdlp_update_ts = time.time()


def _format_bytes(n: Optional[float]) -> str:
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


def _format_eta(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "?"
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _validate_media_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("missing url")

    p = urlparse(raw)
    if p.scheme not in ("http", "https"):
        raise ValueError("url must start with http:// or https://")

    host = (p.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("invalid url host")

    # Allow only YouTube.
    if host == "youtube.com" or host.endswith(".youtube.com"):
        return raw
    if host in ALLOWED_HOSTS:
        return raw

    raise ValueError("only YouTube URLs are allowed")


def _cleanup_old_jobs(max_keep: int = 10) -> None:
    """
    Keep only the N most recent jobs in memory and delete temp intermediates
    for older finished/error jobs. Never touches PLAYLIST_DL_OUTPUT_DIR.
    """
    tmp_base = Path(tempfile.gettempdir()) / "playlist-dl"
    remove_ids: List[str] = []

    with JOBS_LOCK:
        jobs_sorted = sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)
        keep_ids = {j.id for j in jobs_sorted[:max_keep]}
        for j in jobs_sorted[max_keep:]:
            if j.id in keep_ids:
                continue
            # Don't delete intermediates for active jobs.
            if j.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                continue
            remove_ids.append(j.id)

        for job_id in remove_ids:
            JOBS.pop(job_id, None)

    for job_id in remove_ids:
        shutil.rmtree(tmp_base / job_id, ignore_errors=True)


def _ytdlp_worker(
    url: str,
    work_dir: str,
    cookiefile: Optional[str],
    out_q: "mp.Queue[Dict[str, Any]]",
) -> None:
    """
    Runs yt-dlp in a separate process so the parent can enforce a hard timeout.
    Communicates via out_q.
    """

    def qput(msg: Dict[str, Any]) -> None:
        try:
            out_q.put(msg)
        except Exception:
            # Parent likely died; best effort.
            pass

    def hook(d: Dict[str, Any]) -> None:
        status = d.get("status")
        if status not in ["downloading", "finished", "error"]:
            return

        qput(
            {
                "type": "progress",
                "status": status,
                "downloaded_bytes": d.get("downloaded_bytes") or 0,
                "total_bytes": d.get("total_bytes") or d.get("total_bytes_estimate") or 0,
                "speed": d.get("speed"),
                "eta": d.get("eta"),
                "elapsed": d.get("elapsed"),
                "fragment_index": d.get("fragment_index"),
                "fragment_count": d.get("fragment_count"),
            }
        )

    class _Logger:
        def debug(self, msg: str) -> None:
            # Avoid leaking local file paths via verbose yt-dlp debug logs.
            return

        def warning(self, msg: str) -> None:
            if msg:
                qput({"type": "event", "msg": f"yt-dlp warning: {msg}"})

        def error(self, msg: str) -> None:
            if msg:
                qput({"type": "event", "msg": f"yt-dlp error: {msg}"})

    try:
        wd = Path(work_dir)
        wd.mkdir(parents=True, exist_ok=True)
        ydl_opts: Dict[str, Any] = {
            "outtmpl": str(wd / "source.%(ext)s"),
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "writethumbnail": True,
            "writeinfojson": True,
            "progress_hooks": [hook],
            "logger": _Logger(),
            # Fail faster in common stuck-network cases.
            "socket_timeout": 20,
            "retries": 3,
            "fragment_retries": 3,
        }
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile

        qput({"type": "event", "msg": "Starting yt-dlp..."})
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        qput({"type": "done", "info": info})
    except Exception as e:
        qput({"type": "error", "error": str(e)})


def _ytdlp_download_with_timeout(
    *,
    job_id: str,
    url: str,
    work_dir: Path,
    cookiefile: Optional[str],
    deadline: float,
) -> Dict[str, Any]:
    ctx = mp.get_context("spawn")
    out_q: "mp.Queue[Dict[str, Any]]" = ctx.Queue()
    p = ctx.Process(target=_ytdlp_worker, args=(url, str(work_dir), cookiefile, out_q), daemon=True)
    p.start()

    info: Optional[Dict[str, Any]] = None
    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                try:
                    p.terminate()
                except Exception:
                    pass
                raise RuntimeError("job timed out during download")

            try:
                msg = out_q.get(timeout=min(1.0, remaining))
            except queue.Empty:
                if not p.is_alive():
                    break
                continue

            mtype = msg.get("type")
            if mtype == "event":
                _job_event(job_id, str(msg.get("msg") or ""))
                continue
            if mtype == "error":
                raise RuntimeError(f"yt-dlp failed: {msg.get('error')}")
            if mtype == "done":
                info = msg.get("info")
                break
            if mtype == "progress":
                status = msg.get("status")
                downloaded = msg.get("downloaded_bytes") or 0
                total = msg.get("total_bytes") or 0
                speed = msg.get("speed")
                eta = msg.get("eta")
                pct = (downloaded / total) if total else None

                _job_ytdlp_update(job_id, {k: msg.get(k) for k in msg.keys() if k != "type"})

                if status == "downloading":
                    pct_text = f"{pct * 100:.1f}%" if pct is not None else "??%"
                    speed_text = f"{_format_bytes(speed)}/s" if speed else "?/s"
                    eta_text = _format_eta(eta)
                    _job_update(job_id, step=f"downloading ({pct_text}, {speed_text}, eta {eta_text})")
                    if pct is not None:
                        _job_update(job_id, progress=min(0.60, 0.60 * pct))

                    with JOBS_LOCK:
                        job = JOBS.get(job_id)
                        now = time.time()
                        if job and now - job._last_ytdlp_log_ts >= 1.0:
                            job._last_ytdlp_log_ts = now
                            job.events.append(
                                f"yt-dlp: {pct_text} ({_format_bytes(downloaded)} / {_format_bytes(total) if total else '?'}), {speed_text}, eta {eta_text}"
                            )
                            if len(job.events) > 300:
                                job.events = job.events[-300:]
                elif status == "finished":
                    _job_event(job_id, "yt-dlp: download finished")
                    _job_update(job_id, step="download finished", progress=0.60)
    finally:
        try:
            p.join(timeout=2)
        except Exception:
            pass
        if p.is_alive():
            try:
                p.terminate()
            except Exception:
                pass

    if not info:
        raise RuntimeError("yt-dlp exited unexpectedly")
    return info


def _run_ffmpeg_split(
    *,
    ffmpeg_bin: str,
    source_path: Path,
    cover_path: Optional[Path],
    out_dir: Path,
    album: str,
    artist: str,
    chapters: List[Dict[str, Any]],
    job_id: str,
    deadline: float,
) -> None:
    import subprocess

    out_dir.mkdir(parents=True, exist_ok=True)
    cover_jpg = None
    if cover_path and cover_path.exists():
        cover_jpg = _ensure_cover_jpg(ffmpeg_bin, cover_path, out_dir, max(1.0, deadline - time.time()))

    total = len(chapters)
    for i, ch in enumerate(chapters, start=1):
        title = str(ch.get("title") or f"Track {i}")
        start = float(ch["start_time"])
        end = float(ch["end_time"])
        if end <= start:
            continue

        # Map splitting into ~65%-98% of the progress bar.
        base = 0.65
        span = 0.33
        _job_update(
            job_id,
            step=f"processing track {i}/{total}: {title}",
            progress=base + span * ((i - 1) / max(1, total)),
        )
        safe_title = _safe_filename(title)
        out_path = out_dir / f"{i:02d} - {safe_title}.m4a"

        # Use atrim (decode+encode) for sample-accurate boundaries (avoids packet-boundary overlap).
        af = f"atrim=start={start}:end={end},asetpts=PTS-STARTPTS"
        cmd = [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source_path),
            "-filter:a",
            af,
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-movflags",
            "+faststart",
            "-metadata",
            f"album={album}",
            "-metadata",
            f"artist={artist}",
            "-metadata",
            f"title={title}",
            "-metadata",
            f"track={i}",
        ]

        if cover_jpg:
            # Re-run with an attached picture stream.
            cmd = [
                ffmpeg_bin,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source_path),
                "-i",
                str(cover_jpg),
                "-map",
                "0:a:0",
                "-map",
                "1:v:0",
                "-filter:a",
                af,
                "-c:a",
                "aac",
                "-b:a",
                "256k",
                "-c:v",
                "mjpeg",
                "-disposition:v:0",
                "attached_pic",
                "-movflags",
                "+faststart",
                "-metadata",
                f"album={album}",
                "-metadata",
                f"artist={artist}",
                "-metadata",
                f"title={title}",
                "-metadata",
                f"track={i}",
            ]

        cmd.append(str(out_path))
        remaining = deadline - time.time()
        if remaining <= 0:
            raise RuntimeError("job timed out during processing")
        subprocess.run(cmd, check=True, timeout=max(1, int(remaining)))

    _job_update(job_id, step="finalizing", progress=0.98)


def _zip_dir(src_dir: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src_dir.rglob("*")):
            if p.is_file():
                zf.write(p, arcname=str(p.relative_to(src_dir)))


def _run_job(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.status = JobStatus.RUNNING
        job.step = "starting"
        job.progress = 0.0

    tmp_base = Path(tempfile.gettempdir()) / "playlist-dl"
    job_base = tmp_base / job_id
    work_dir = job_base / "work"
    out_dir = job_base / "out"
    work_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        timeout_s = int(os.environ.get("PLAYLIST_DL_JOB_TIMEOUT_SECONDS", "7200"))
        deadline = time.time() + max(60, timeout_s)

        ffmpeg_bin = os.environ.get("FFMPEG_BIN", "ffmpeg")
        if Path(ffmpeg_bin).exists():
            ffmpeg_bin_path = Path(ffmpeg_bin)
            if not os.access(str(ffmpeg_bin_path), os.X_OK):
                raise RuntimeError(f"FFMPEG_BIN is not executable: {ffmpeg_bin}")
        else:
            if not shutil.which(ffmpeg_bin):
                raise RuntimeError(f"ffmpeg not found (set FFMPEG_BIN or install ffmpeg): {ffmpeg_bin}")

        output_root = os.environ.get("PLAYLIST_DL_OUTPUT_DIR")
        if not output_root:
            output_root = str(Path.cwd() / "outputs")
        output_root = os.path.abspath(os.path.expanduser(output_root))

        cookiefile = os.environ.get("YTDLP_COOKIES") or None

        _job_event(job_id, "Preparing yt-dlp...")
        _job_update(job_id, step="downloading", progress=0.02)
        info = _ytdlp_download_with_timeout(
            job_id=job_id,
            url=job.url,
            work_dir=work_dir,
            cookiefile=cookiefile,
            deadline=deadline,
        )

        source_path, thumb_path = _pick_downloaded_files(work_dir)
        _job_event(job_id, f"Downloaded: {source_path.name}")
        if thumb_path:
            _job_event(job_id, f"Thumbnail: {thumb_path.name}")

        album = (job.album_override or info.get("title") or "Album").strip()
        artist = (job.artist_override or info.get("uploader") or info.get("channel") or "Unknown").strip()
        safe_album = _safe_filename(album)

        chapters = _extract_chapters(info, job.manual_timestamps)
        _job_event(job_id, f"Tracks: {len(chapters)}")

        _job_update(job_id, step="splitting", progress=0.65)
        _run_ffmpeg_split(
            ffmpeg_bin=ffmpeg_bin,
            source_path=source_path,
            cover_path=thumb_path,
            out_dir=out_dir,
            album=album,
            artist=artist,
            chapters=chapters,
            job_id=job_id,
            deadline=deadline,
        )

        if time.time() > deadline:
            raise RuntimeError("job timed out during finalization")

        # Store on server or prepare download zip
        if job.mode == "store":
            dest_root = Path(output_root)
            dest_root.mkdir(parents=True, exist_ok=True)

            dest_dir = dest_root / safe_album
            if dest_dir.exists():
                dest_dir = dest_root / f"{safe_album}-{_now_ts()}-{job_id}"

            # Move to the output root to avoid duplicating potentially large audio files.
            shutil.move(str(out_dir), str(dest_dir))
            with JOBS_LOCK:
                job.output_dir = str(dest_dir)
            _job_event(job_id, "Saved on server.")
        else:
            zip_path = job_base / f"{safe_album or 'album'}.zip"
            _zip_dir(out_dir, zip_path)
            shutil.rmtree(out_dir, ignore_errors=True)
            with JOBS_LOCK:
                job.zip_path = str(zip_path)
            _job_event(job_id, "ZIP ready")

        with JOBS_LOCK:
            job.status = JobStatus.FINISHED
            job.step = "done"
            job.progress = 1.0
        # Best-effort cleanup of large intermediates.
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as e:
        with JOBS_LOCK:
            job.status = JobStatus.ERROR
            job.error = str(e)
            job.step = "error"
        _job_event(job_id, f"ERROR: {e}")


app = FastAPI(title="playlist-dl")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        "index.html",
        {
            "request": request,
        },
    )


@app.post("/api/jobs")
async def create_job(
    url: str = Form(...),
    mode: str = Form("store"),
    manual_timestamps: str = Form(""),
    album: str = Form(""),
    artist: str = Form(""),
) -> JSONResponse:
    try:
        url = _validate_media_url(url)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if mode not in ["store", "download"]:
        return JSONResponse({"error": "invalid mode"}, status_code=400)

    # Best-effort cleanup of old intermediates (never touches PLAYLIST_DL_OUTPUT_DIR).
    _cleanup_old_jobs(max_keep=9)

    # Simple, URL-safe job id.
    job_id = secrets.token_urlsafe(8)
    job = Job(
        id=job_id,
        url=url,
        mode=mode,
        manual_timestamps=manual_timestamps or "",
        album_override=album or "",
        artist_override=artist or "",
        step="queued",
    )
    with JOBS_LOCK:
        JOBS[job_id] = job

    _job_event(job_id, "Queued")
    _cleanup_old_jobs(max_keep=10)
    asyncio.create_task(asyncio.to_thread(_run_job, job_id))
    return JSONResponse({"job_id": job_id})


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(
            {
                "job_id": job.id,
                "status": job.status.value,
                "step": job.step,
                "progress": job.progress,
                "events": job.events[-80:],
                "ytdlp": job.ytdlp,
                "stored": bool(job.output_dir),
                "zip_ready": bool(job.zip_path),
                "error": job.error,
            }
        )


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str) -> FileResponse:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="not found")
        if job.status != JobStatus.FINISHED or not job.zip_path:
            raise HTTPException(status_code=409, detail="zip not ready")
        zip_path = job.zip_path

    path = Path(zip_path)
    return FileResponse(
        str(path),
        media_type="application/zip",
        filename=path.name,
    )
