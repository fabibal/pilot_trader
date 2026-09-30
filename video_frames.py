"""YouTube chapter metadata and optional real frames, fetched by the host digest."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
MAX_FRAMES = 3


def inspect_video(video_id):
    """Keep signed stream URLs in memory only; ledgers store chapters and images."""
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError("Invalid YouTube video ID")
    cmd = [sys.executable, "-m", "yt_dlp", "--dump-single-json",
           "--skip-download", "--no-playlist", "--quiet", "--no-warnings",
           "--socket-timeout", "15", "--retries", "1",
           "-f", "bestvideo[height<=1080][vcodec^=avc1]/best[height<=1080]/bestvideo[height<=1080]"]
    deno = Path.home() / ".deno/bin/deno"
    if deno.is_file():
        cmd += ["--js-runtimes", f"deno:{deno}"]
    cmd.append(f"https://www.youtube.com/watch?v={video_id}")
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=90, check=False)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("YouTube metadata timed out") from exc
    if result.returncode:
        # Do not log the response: it can contain temporary signed stream URLs.
        raise ValueError("YouTube metadata fetch failed")
    info = json.loads(result.stdout)
    chapters = [{"title": c["title"], "start_seconds": int(c["start_time"])}
                for c in (info.get("chapters") or [])]
    return {"chapters": chapters, "duration": info.get("duration"),
            "stream_url": info.get("url")}


def extract_frames(video_id, frames, metadata, directory):
    """Best effort, bounded work. A missing frame keeps its timestamp link."""
    if not VIDEO_ID_RE.fullmatch(video_id):
        return []
    out = []
    seen = set()
    for frame in frames[:MAX_FRAMES]:
        seconds = frame.get("timestamp_seconds")
        if (type(seconds) is not int or seconds < 0 or seconds in seen
                or (metadata and metadata.get("duration") is not None
                    and seconds >= metadata["duration"])):
            continue
        seen.add(seconds)
        item = {"timestamp_seconds": seconds, "caption": frame.get("caption") or ""}
        out.append(item)
        filename = f"{video_id}_{seconds}.jpg"
        path = Path(directory) / filename
        if path.is_file():
            item["image_file"] = filename
            continue
        if not metadata or not metadata.get("stream_url"):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp.jpg")
        try:
            result = subprocess.run([
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-rw_timeout", "15000000", "-ss", str(seconds),
                "-i", metadata["stream_url"], "-frames:v", "1",
                "-vf", "scale=min(1280\\,iw):-2", "-q:v", "3", str(temporary),
            ], capture_output=True, timeout=40, check=False)
            if result.returncode or not temporary.is_file() or temporary.stat().st_size < 100:
                raise ValueError("Frame extraction failed")
            os.replace(temporary, path)
            item["image_file"] = filename
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f"    [frame unavailable] {seconds}s: {type(exc).__name__}", file=sys.stderr)
        finally:
            temporary.unlink(missing_ok=True)
    return out
