#!/usr/bin/env python3
"""Aeon-style leech thumbnails for HStream-TG.

Priority (same idea as Aeon TelegramUploader):
  1. User custom thumb  →  thumbnails/{user_id}.jpg  (/thumb)
  2. Mid-video frame    →  ffmpeg scale 640 (Aeon get_video_thumbnail)
  3. Series poster      →  downloaded cover
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("hstream-tg")

THUMB_DIR = Path("thumbnails")


def _ffmpeg() -> Optional[str]:
    for name in ("ffmpeg", "xtra"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _ffprobe() -> Optional[str]:
    for name in ("ffprobe", "ffmpeg", "xtra"):
        p = shutil.which(name)
        if p:
            return p
    return None


def user_thumb_path(user_id: int) -> Path:
    return THUMB_DIR / f"{user_id}.jpg"


def create_user_thumb(photo_path: Path, user_id: int) -> Optional[Path]:
    """Save a photo as the user's permanent leech thumbnail."""
    ff = _ffmpeg()
    if not ff:
        logger.warning("ffmpeg not found – cannot save user thumb")
        return None
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    out = user_thumb_path(user_id)
    try:
        subprocess.run(
            [
                ff, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(photo_path),
                "-vf", "scale=320:-1",
                "-q:v", "5",
                str(out),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        if out.exists() and out.stat().st_size > 0:
            return out
    except Exception as e:
        logger.warning("create_user_thumb failed: %s", e)
    return None


def download_poster_thumb(poster_url: str, dest_dir: Path) -> Optional[Path]:
    if not poster_url:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    raw = dest_dir / "poster_raw"
    out = dest_dir / "poster_thumb.jpg"
    ff = _ffmpeg()
    try:
        with requests.get(poster_url, stream=True, timeout=30) as r:
            if r.status_code != 200:
                return None
            with open(raw, "wb") as f:
                for chunk in r.iter_content(8192):
                    if chunk:
                        f.write(chunk)
        if ff:
            subprocess.run(
                [
                    ff, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(raw),
                    "-vf", "scale=320:-1",
                    "-q:v", "5",
                    str(out),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            raw.unlink(missing_ok=True)
        else:
            out = raw
        if out.exists() and out.stat().st_size > 0:
            return out
    except Exception as e:
        logger.warning("Poster thumb failed: %s", e)
    return None


def _video_duration(video_path: Path) -> float:
    probe = _ffprobe()
    if not probe:
        return 0.0
    try:
        r = subprocess.run(
            [
                probe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    ff = _ffmpeg()
    if not ff:
        return 0.0
    try:
        r = subprocess.run(
            [ff, "-i", str(video_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr or "")
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + s
    except Exception:
        pass
    return 0.0


def extract_video_thumb(video_path: Path, dest_dir: Path) -> Optional[Path]:
    """Mid-point frame, scale 640 — matches Aeon get_video_thumbnail."""
    ff = _ffmpeg()
    if not ff:
        logger.warning("ffmpeg not found – skip video thumb")
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{video_path.stem}_thumb.jpg"
    duration = _video_duration(video_path)
    ss = max(1.0, duration / 2.0) if duration > 0 else 5.0
    if duration > 0 and ss >= duration:
        ss = max(0.5, duration * 0.4)
    try:
        subprocess.run(
            [
                ff, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{ss:.2f}",
                "-i", str(video_path),
                "-vf", "scale=640:-1",
                "-vframes", "1",
                "-q:v", "5",
                str(out),
            ],
            check=True,
            capture_output=True,
            timeout=90,
        )
        if out.exists() and out.stat().st_size > 0:
            return out
    except Exception as e:
        logger.warning("Video thumb extract failed for %s: %s", video_path.name, e)
    try:
        subprocess.run(
            [
                ff, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(video_path),
                "-vf", "scale=640:-1",
                "-vframes", "1",
                "-q:v", "5",
                str(out),
            ],
            check=True,
            capture_output=True,
            timeout=90,
        )
        if out.exists() and out.stat().st_size > 0:
            return out
    except Exception as e:
        logger.warning("Video thumb fallback failed: %s", e)
    return None


def resolve_doc_thumb(
    video_path: Path,
    user_id: int,
    series_thumb: Optional[Path] = None,
    work_dir: Optional[Path] = None,
) -> Optional[str]:
    """user custom → mid-video frame → series poster."""
    ut = user_thumb_path(user_id)
    if ut.exists() and ut.stat().st_size > 0:
        return str(ut)

    work = work_dir or video_path.parent
    vthumb = extract_video_thumb(video_path, work)
    if vthumb:
        return str(vthumb)

    if series_thumb and series_thumb.exists():
        return str(series_thumb)
    return None
