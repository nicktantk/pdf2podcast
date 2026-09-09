"""Let browser media elements stream one authorized recording and seek by byte range."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs


PLAYBACK_TTL = 12 * 60 * 60
AUDIO_ROUTE = re.compile(r"^/api/audio/jobs/(pdf-[a-f0-9]{16})/audio$")


def playback_signature(path: str, expires: int) -> str:
    secret = os.environ.get("PDF2PODCAST_MOBILE_TOKEN", "")
    if not secret:
        raise ValueError("Playback signing requires the mobile token.")
    message = f"pdf2podcast-playback-v1\n{path}\n{expires}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def playback_path(job_id: str) -> str:
    path = f"/api/audio/jobs/{job_id}/audio"
    if not AUDIO_ROUTE.fullmatch(path):
        raise ValueError("Invalid playback job id.")
    expires = int(time.time()) + PLAYBACK_TTL
    return f"{path}?expires={expires}&signature={playback_signature(path, expires)}"


def authorized_playback(path: str, query: str) -> bool:
    if not AUDIO_ROUTE.fullmatch(path):
        return False
    try:
        values = parse_qs(query, max_num_fields=8)
        expiry = values.get("expires", [])
        signature = values.get("signature", [])
        if len(expiry) != 1 or len(signature) != 1:
            return False
        if not re.fullmatch(r"[0-9]{1,12}", expiry[0]):
            return False
        expires = int(expiry[0])
        if expires <= time.time():
            return False
        supplied = signature[0]
        if not re.fullmatch(r"[a-f0-9]{64}", supplied):
            return False
        return hmac.compare_digest(supplied, playback_signature(path, expires))
    except (TypeError, ValueError):
        return False


def byte_range(value: str, size: int) -> tuple[int, int]:
    match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", value.strip())
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError("Invalid byte range.")
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("Empty suffix range.")
        return max(0, size - suffix), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError("Unsatisfiable byte range.")
    return start, end


def send_audio(handler, path: Path) -> None:
    with path.open("rb") as source:
        stat = os.fstat(source.fileno())
        size = stat.st_size
        etag = f'"{stat.st_mtime_ns:x}-{size:x}"'
        requested = handler.headers.get("Range")
        if handler.headers.get("If-Range", etag) != etag:
            requested = None
        start, end = 0, size - 1
        if requested:
            try:
                start, end = byte_range(requested, size)
            except ValueError:
                handler.send_response(416)
                handler.cors()
                handler.send_header("Content-Range", f"bytes */{size}")
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return
        handler.send_response(206 if requested else 200)
        handler.cors()
        handler.send_header("Content-Type", "audio/mpeg")
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("ETag", etag)
        handler.send_header("Cache-Control", "private, no-store")
        handler.send_header("Content-Disposition", 'inline; filename="audio.mp3"')
        handler.send_header("Content-Length", str(end - start + 1))
        if requested:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        handler.end_headers()
        source.seek(start)
        remaining = end - start + 1
        try:
            while remaining > 0:
                block = source.read(min(64 * 1024, remaining))
                if not block:
                    break
                handler.wfile.write(block)
                remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            # Seeking or selecting another episode cancels the previous range request.
            return
