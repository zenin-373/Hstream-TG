#!/usr/bin/env python3
"""
Core download + subtitle remux logic for HStream-TG.
Adapted from Hstream-Extractor. Safe to call from async code via to_thread.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

import requests
from tqdm import tqdm


ProgressCallback = Callable[[str], None]


def ensure_dependencies(progress: Optional[ProgressCallback] = None) -> None:
    def log(msg: str) -> None:
        if progress:
            progress(msg)
        else:
            print(msg)

    log("Checking / installing Python dependencies...")
    try:
        subprocess.run(
            [
                sys.executable, "-m", "pip", "install", "--upgrade",
                "yt-dlp", "requests", "tqdm", "hanime-plugin",
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"pip install failed: {e}") from e

    for pkg in ("aria2c", "ffmpeg"):
        if subprocess.run(["which", pkg], capture_output=True).returncode != 0:
            log(f"WARNING: '{pkg}' not found in PATH – quality / speed may suffer.")
    log("Dependency check done.")


def download_video(
    url: str,
    dest: Path,
    cookies_file: Optional[Path] = None,
    progress: Optional[ProgressCallback] = None,
) -> Path:
    def log(msg: str) -> None:
        if progress:
            progress(msg)

    output_template = str(dest / "%(title)s.%(ext)s")
    format_tries = [
        "best",
        "bestvideo*+bestaudio/best",
        "best[height<=2160]",
        "best[height<=1080]",
        "best[height<=720]",
    ]

    def make_cmd(fmt: str, downloader: str) -> list[str]:
        c = [
            "yt-dlp",
            "-f", fmt,
            "--downloader", downloader,
            "--concurrent-fragments", "8",
            "-o", output_template,
            "--no-mtime",
            "--retries", "5",
            "--fragment-retries", "5",
            "--no-progress",  # we report ourselves
        ]
        if downloader == "aria2c":
            c += ["--downloader-args", "aria2c:-x 16 -s 16 -k 1M"]
        if cookies_file and cookies_file.exists():
            c.extend(["--cookies", str(cookies_file)])
        c.append(url)
        return c

    log(f"Downloading: {url}")
    last_err: Optional[Exception] = None

    for fmt in format_tries:
        for downloader in ("aria2c", "ffmpeg"):
            try:
                log(f"  trying format={fmt} · downloader={downloader}")
                subprocess.run(make_cmd(fmt, downloader), check=True, capture_output=True)
                last_err = None
                break
            except subprocess.CalledProcessError as e:
                last_err = e
                continue
        if last_err is None:
            break

    if last_err is not None:
        raise RuntimeError(f"Download failed after all format/downloader tries: {last_err}") from last_err

    files = [p for p in dest.glob("*") if p.suffix.lower() not in {".ass", ".part", ".ytdl"}]
    if not files:
        raise FileNotFoundError("No video file was produced by yt-dlp.")
    return max(files, key=lambda p: p.stat().st_ctime)


def download_subtitle(sub_url: str, sub_path: Path) -> bool:
    try:
        with requests.get(sub_url, stream=True, timeout=30) as r:
            if r.status_code != 200:
                return False
            with open(sub_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception:
        return False


def resolve_subtitle_from_page(page_url: str, cookies_header: Optional[str] = None) -> Optional[str]:
    """Scrape the episode page for the live .ass link (CDN hosts rotate)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://hstream.moe/",
    }
    if cookies_header:
        headers["Cookie"] = cookies_header

    try:
        r = requests.get(page_url, headers=headers, timeout=30)
        if r.status_code != 200:
            return None
        html = r.text
        patterns = [
            r'href=["\'](https?://[^"\']+?/eng\.ass)["\']',
            r'href=["\'](https?://[^"\']+?\.ass)["\']',
            r'(https?://[a-z0-9.-]+(?:-str\.[a-z0-9.-]+)/(?:\d{4}/)?[^\s"\']+/eng\.ass)',
        ]
        found: list[str] = []
        for pat in patterns:
            for m in re.finditer(pat, html, flags=re.I):
                u = m.group(1)
                if u not in found:
                    found.append(u)
        if not found:
            return None
        for u in found:
            if u.lower().endswith("/eng.ass") or u.lower().endswith("eng.ass"):
                return u
        return found[0]
    except Exception:
        return None


def remux_to_mkv(video_path: Path, sub_path: Path, output_mkv: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(sub_path),
            "-map", "0", "-map", "1",
            "-c", "copy",
            "-metadata:s:s:0", "language=eng",
            str(output_mkv),
        ],
        check=True,
        capture_output=True,
    )


def process_url(
    url: str,
    dest: Path,
    series_slug: Optional[str] = None,
    year: str = "2024",
    cookies_file: Optional[Path] = None,
    progress: Optional[ProgressCallback] = None,
) -> Path:
    """
    Download one episode, try to attach English .ass, return final file path.
    """
    def log(msg: str) -> None:
        if progress:
            progress(msg)

    dest.mkdir(parents=True, exist_ok=True)
    video_path = download_video(url, dest, cookies_file=cookies_file, progress=progress)
    base_name = video_path.stem
    final_mkv = dest / f"{base_name}.mkv"

    if video_path.suffix.lower() == ".mkv":
        log(f"Already MKV → {video_path.name}")
        return video_path

    ep_match = re.search(r"-(\d+)/?$", url.rstrip("/"))
    if not ep_match:
        log("Could not parse episode number – keeping original video.")
        return video_path

    ep_num = int(ep_match.group(1))
    slug_part = re.sub(r"-\d+$", "", url.rstrip("/").split("/")[-1])

    sub_path = dest / f"{base_name}.ass"
    sub_ok = False

    log("Looking for English subtitle...")
    live_sub = resolve_subtitle_from_page(url)
    if live_sub and download_subtitle(live_sub, sub_path):
        sub_ok = True
        log("Subtitle found on episode page.")

    if not sub_ok:
        candidates: list[str] = []
        if series_slug:
            candidates.append(series_slug)
        dotted = slug_part.replace("-", ".")
        titleish = ".".join(
            w if w in {"no", "wa", "wo", "ga", "ni", "de", "to", "na", "o"} else w.capitalize()
            for w in slug_part.split("-")
        )
        candidates += [
            dotted,
            titleish,
            slug_part,
            ".".join(w.capitalize() for w in slug_part.split("-")),
        ]
        # dedupe preserving order
        seen: set[str] = set()
        candidates = [c for c in candidates if not (c in seen or seen.add(c))]

        sub_hosts = [
            "https://oppai-str.shoujo-h.org",
            "https://imoto-str.ane-h.xyz",
            "https://shinobu-str.rorikon-h.xyz",
        ]
        years = []
        for y in (year, "2026", "2025", "2024", "2023", "2022", "2021"):
            if y not in years:
                years.append(y)

        log("Page scrape failed – trying known subtitle hosts...")
        for host in sub_hosts:
            for y in years:
                for slug in candidates:
                    sub_url = f"{host}/{y}/{slug}/E{ep_num:02d}/eng.ass"
                    if download_subtitle(sub_url, sub_path):
                        sub_ok = True
                        log(f"Subtitle found: {host} / {y} / {slug}")
                        break
                if sub_ok:
                    break
            if sub_ok:
                break

    if sub_ok:
        log("Remuxing video + subtitles → MKV...")
        remux_to_mkv(video_path, sub_path, final_mkv)
        sub_path.unlink(missing_ok=True)
        if video_path != final_mkv and video_path.exists():
            video_path.unlink()
        log(f"Finished: {final_mkv.name}")
        return final_mkv

    log("No subtitle found – keeping original video.")
    return video_path
