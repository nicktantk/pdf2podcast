"""Create and serve chaptered, two-voice, audio-first PDF companions."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import queue
import random
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .playback import authorized_playback, playback_path, send_audio
from .paper_companion import (
    STRUCTURE_PROMPT, SUMMARY_STYLE_PROMPT, REFERENCE_HEADING, METADATA_HEADING,
    annotation_batches, chapters_from_plans,
    extract_document, join_lines, summary_is_concise, validate_groups,
)

import edge_tts

try:
    from pypdf import PdfReader
except ImportError:
    from PyPDF2 import PdfReader


ROOT = Path(os.environ.get("PDF2PODCAST_DATA_DIR", "~/.pdf2podcast")).expanduser()
JOBS = ROOT / "jobs"
SOURCE_VOICE = os.environ.get(
    "PDF2PODCAST_SOURCE_VOICE", "en-US-AndrewMultilingualNeural"
)
GUIDE_VOICE = os.environ.get(
    "PDF2PODCAST_GUIDE_VOICE", "en-US-AvaMultilingualNeural"
)
SCRIPT_MODEL = os.environ.get("PDF2PODCAST_SCRIPT_MODEL", "gpt-4.1-mini")
# Set to https://api.deepseek.com to use DeepSeek instead of OpenAI.
LLM_BASE_URL = os.environ.get("PDF2PODCAST_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
SCRIPT_MAX_TOKENS = int(os.environ.get("PDF2PODCAST_SCRIPT_MAX_TOKENS", "0"))
MAX_UPLOAD_BYTES = int(os.environ.get("PDF2PODCAST_MAX_UPLOAD_BYTES", 100 * 1024 * 1024))
SCRIPT_CHUNK_CHARS = int(os.environ.get("PDF2PODCAST_SCRIPT_CHUNK_CHARS", 28_000))
JOB_ID = re.compile(r"^pdf-[a-f0-9]{16}$")
UPLOADS: queue.Queue[str] = queue.Queue()
JOB_CONTROL_LOCK = threading.RLock()
TTS_CONCURRENCY = max(1, min(128, int(os.environ.get("PDF2PODCAST_TTS_CONCURRENCY", "16"))))
SCRIPT_CONCURRENCY = max(1, min(128, int(os.environ.get("PDF2PODCAST_SCRIPT_CONCURRENCY", "16"))))
TTS_CLIP_CONCURRENCY = max(1, min(128, int(os.environ.get("PDF2PODCAST_TTS_CLIP_CONCURRENCY", "32"))))
SUMMARY_CONCURRENCY = max(1, min(128, int(os.environ.get("PDF2PODCAST_SUMMARY_CONCURRENCY", "16"))))
OPENAI_CONCURRENCY = max(1, min(128, int(os.environ.get("PDF2PODCAST_OPENAI_CONCURRENCY", "16"))))
MEDIA_CONCURRENCY = max(1, min(8, int(os.environ.get("PDF2PODCAST_MEDIA_CONCURRENCY", str(min(4, os.cpu_count() or 1))))))
MEDIA_SLOTS = threading.BoundedSemaphore(MEDIA_CONCURRENCY)
TTS_ATTEMPTS = max(1, min(6, int(os.environ.get("PDF2PODCAST_TTS_ATTEMPTS", "3"))))
TTS_TIMEOUT_SECONDS = max(30, min(1800, int(os.environ.get("PDF2PODCAST_TTS_TIMEOUT_SECONDS", "300"))))

SCRIPT_SYSTEM_PROMPT = STRUCTURE_PROMPT


class AdaptiveAPIGate:
    """Start with high IO concurrency; share provider cooldowns rather than retry storms."""

    def __init__(self, maximum: int):
        self.maximum = maximum
        self.limit = maximum
        self.active = 0
        self.successes = 0
        self.blocked_until = 0.0
        self.condition = threading.Condition()

    def __enter__(self):
        with self.condition:
            while True:
                delay = self.blocked_until - time.monotonic()
                if delay <= 0 and self.active < self.limit:
                    self.active += 1
                    return self
                self.condition.wait(timeout=delay if delay > 0 else None)

    def __exit__(self, *_):
        with self.condition:
            self.active -= 1
            self.condition.notify_all()

    def succeeded(self) -> None:
        with self.condition:
            self.successes += 1
            if self.limit < self.maximum and self.successes >= max(4, self.limit * 2):
                self.limit += 1
                self.successes = 0
                self.condition.notify_all()

    def throttled(self, delay: float) -> None:
        with self.condition:
            self.limit = max(1, self.limit // 2)
            self.successes = 0
            self.blocked_until = max(self.blocked_until, time.monotonic() + delay)
            self.condition.notify_all()


OPENAI_GATE = AdaptiveAPIGate(OPENAI_CONCURRENCY)
SPEECH_GATE = AdaptiveAPIGate(TTS_CLIP_CONCURRENCY)


class OpenAIRequestError(RuntimeError):
    """Keep transport retries separate from retries of a malformed model response."""


def retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if seconds < float("inf") else 0
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def request_openai_json(body: dict[str, Any]) -> dict[str, Any]:
    """Bound attempts, honor Retry-After, and never retry an exhausted credit balance."""
    deadline = time.monotonic() + 600
    last_error = "OpenAI request failed"
    for attempt in range(6):
        delay = min(60, 2 ** attempt) + random.uniform(0.25, 1.25)
        request = urllib.request.Request(
            f"{LLM_BASE_URL}/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {openai_key()}", "Content-Type": "application/json"}, method="POST",
        )
        shared_cooldown = False
        try:
            with OPENAI_GATE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OpenAIRequestError("OpenAI request deferred after its retry time budget; checkpoints are retained.")
                with urllib.request.urlopen(request, timeout=min(180, remaining)) as response:
                    payload = json.loads(response.read())
                if not isinstance(payload, dict):
                    raise ValueError("OpenAI returned an invalid response envelope.")
            OPENAI_GATE.succeeded()
            return payload
        except urllib.error.HTTPError as error:
            delay = max(delay, retry_after_seconds((error.headers or {}).get("Retry-After")) + random.uniform(0.1, 0.5))
            try:
                detail = json.loads(error.read()).get("error", {})
                code = detail.get("code") if isinstance(detail, dict) else None
            except (ValueError, AttributeError):
                code = None
            if code in {"insufficient_quota", "billing_hard_limit_reached"}:
                raise OpenAIRequestError("OpenAI quota exhausted; add credits before retrying.") from error
            if error.code not in {408, 409, 429, 500, 502, 503, 504}:
                raise OpenAIRequestError(f"OpenAI HTTP {error.code}; configuration or account action is required.") from error
            last_error = f"OpenAI HTTP {error.code}"
            if error.code in {429, 503}:
                OPENAI_GATE.throttled(delay)
                shared_cooldown = True
        except (OSError, ValueError) as error:
            last_error = f"OpenAI transport error: {type(error).__name__}"
        if attempt == 5 or time.monotonic() + delay >= deadline:
            raise OpenAIRequestError(f"{last_error}; request retries exhausted, saved work retained.")
        if not shared_cooldown:
            time.sleep(delay)
    raise OpenAIRequestError(last_error)


def run_bounded_parallel(
    items: list[Any],
    operation: Callable[[Any], Any],
    concurrency: int,
    on_result: Callable[[Any, Any], None] | None = None,
) -> None:
    """Bound both active and submitted work; publish results only on the caller thread."""
    if not items:
        return
    workers = min(concurrency, len(items))
    remaining = iter(items)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pdf2podcast-api") as pool:
        pending = {}

        def fill_slots() -> None:
            while len(pending) < workers:
                try:
                    item = next(remaining)
                except StopIteration:
                    break
                pending[pool.submit(operation, item)] = item

        try:
            fill_slots()
            while pending:
                finished, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                for future in finished:
                    item = pending.pop(future)
                    result = future.result()
                    if on_result is not None:
                        on_result(item, result)
                fill_slots()
        finally:
            # Stop submitting on failure. Let already-running calls finish and save
            # checkpoints before the job becomes retryable; no overlapping attempts.
            for future in pending:
                future.cancel()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per write so concurrent writers never share a temporary file.
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(50):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            # Windows refuses to replace a file another thread has open for reading.
            if attempt == 49:
                raise
            time.sleep(0.02)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return re.sub(r"-+", "-", value)[:80] or "section"


def safe_job(job_id: str) -> Path:
    if not JOB_ID.fullmatch(job_id):
        raise ValueError("Invalid job id")
    return JOBS / job_id


def job_path(job_id: str) -> Path:
    return safe_job(job_id) / "job.json"


def load_job(job_id: str) -> dict[str, Any]:
    job = read_json(job_path(job_id))
    if not isinstance(job, dict):
        raise FileNotFoundError(job_id)
    return job


def save_job(job_id: str, **changes: Any) -> dict[str, Any]:
    job = load_job(job_id)
    job.update(changes)
    job["updated_at"] = now()
    atomic_json(job_path(job_id), job)
    return job


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in job.items() if key != "input_path"}
    if job.get("status") in {"complete", "completed"}:
        result["playback_url"] = playback_path(job["job_id"])
    return result


def list_jobs(limit: int = 100) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for path in JOBS.glob("pdf-*/job.json"):
        job = read_json(path)
        if isinstance(job, dict):
            found.append(public_job(job))
    def created_timestamp(item: dict[str, Any]) -> float:
        value = item.get("created_at")
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except (ValueError, OverflowError):
                pass
        return 0.0

    found.sort(key=created_timestamp, reverse=True)
    return found[: max(1, min(limit, 200))]


def extract_pdf(pdf: Path) -> tuple[str, list[dict[str, Any]]]:
    reader = PdfReader(str(pdf))
    metadata = reader.metadata or {}
    title = str(metadata.get("/Title") or pdf.stem).strip()
    pages = []
    for index, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        text = text.replace("\u0000", "").replace("\r\n", "\n")
        text = re.sub(r"(?<=\w)-\n(?=\w)", "", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{4,}", "\n\n", text).strip()
        if text:
            pages.append({"page": index, "text": text})
    if not pages:
        raise RuntimeError("No extractable text was found. Scanned PDFs need OCR first.")
    return title or pdf.stem, pages


def chunk_pages(pages: list[dict[str, Any]]) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for page in pages:
        block = f"\n\n--- PAGE {page['page']} ---\n{page['text']}"
        if current and size + len(block) > SCRIPT_CHUNK_CHARS:
            chunks.append("".join(current).strip())
            current, size = [], 0
        current.append(block)
        size += len(block)
    if current:
        chunks.append("".join(current).strip())
    return chunks


def openai_key() -> str:
    # Match the key to the provider so one provider's key is never sent to the other.
    name = "DEEPSEEK_API_KEY" if "deepseek.com" in LLM_BASE_URL else "OPENAI_API_KEY"
    direct = os.environ.get(name, "").strip()
    if direct:
        return direct
    path = Path(
        os.environ.get("PDF2PODCAST_OPENAI_KEY_FILE", "~/.pdf2podcast/openai-key")
    ).expanduser()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError("The Pdf2Podcast OpenAI key file is missing.") from error


def parse_model_json(content: str) -> dict[str, Any]:
    content = content.strip()
    fence = chr(96) * 3
    if content.startswith(fence):
        content = content[len(fence):].removeprefix("json").strip()
        if content.endswith(fence):
            content = content[:-len(fence)].strip()
    value = json.loads(content)
    if not isinstance(value, dict):
        raise RuntimeError("The model response must be a JSON object.")
    return value


def create_script(
    document_title: str, chunk: dict[str, Any], index: int, total: int, *, finalize: bool = True,
) -> dict[str, Any]:
    request_body = {
        "model": SCRIPT_MODEL,
        "messages": [
            {"role": "system", "content": SCRIPT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Document: {document_title}\n"
                    f"Annotation batch {index} of {total}, NOT a chapter boundary. "
                    "Map the supplied line IDs to actual printed headings and paragraphs. "
                    "Never generate male narration; it is copied directly from these lines. "
                    "The first paragraph may continue the preceding batch.\n\n"
                    f"{json.dumps(chunk, ensure_ascii=False)}"
                ),
            },
        ],
        "response_format": {"type": "json_object"},
    }
    if SCRIPT_MAX_TOKENS > 0:
        request_body["max_tokens"] = SCRIPT_MAX_TOKENS
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            payload = request_openai_json(request_body)
            proposal = parse_model_json(payload["choices"][0]["message"]["content"])
            if not isinstance(proposal.get("groups"), list):
                raise RuntimeError("The structure annotation must contain a groups list.")
            if not finalize:
                return proposal
            plan = validate_groups(proposal, chunk)
            return summarize_recovered_paragraphs(plan, chunk)
        except OpenAIRequestError:
            raise
        except (OSError, KeyError, json.JSONDecodeError, RuntimeError) as error:
            last_error = error
        time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"Script generation failed: {last_error}")


def summarize_one_paragraph(context: dict[str, Any], cache_root: Path | None = None) -> dict[str, str]:
    """One request owns one paragraph; the model cannot misassign or omit paragraph IDs."""
    fingerprint = hashlib.sha256(json.dumps(
        {"context": context, "style": SUMMARY_STYLE_PROMPT, "model": SCRIPT_MODEL, "contract": "one-paragraph-v1"},
        sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    cache_path = cache_root / f"{fingerprint}.json" if cache_root is not None else None
    cached = read_json(cache_path, {}) if cache_path is not None else {}
    if (isinstance(cached, dict) and cached.get("fingerprint") == fingerprint
            and summary_is_concise(cached.get("summary")) and isinstance(cached.get("topic"), str)):
        return {"topic": cached["topic"], "summary": cached["summary"]}
    for attempt in range(4):
        body = {
            "model": SCRIPT_MODEL,
            "messages": [
                {"role": "system", "content": SUMMARY_STYLE_PROMPT +
                 '\nSummarize exactly ONE supplied paragraph. Return one JSON object: '
                 '{"topic":"Specific 3-9 word topic","summary":"One short sentence."}. '
                 "Do not return a list, paragraph IDs, source line ranges, or rewritten narration. " +
                 ("Keep the sentence especially short, ideally 8-12 words." if attempt else "")},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 250,
        }
        payload = request_openai_json(body)
        try:
            result = parse_model_json(payload["choices"][0]["message"]["content"])
            if not isinstance(result, dict) or not summary_is_concise(result.get("summary")):
                continue
            summary = re.sub(r"\s+", " ", result["summary"]).strip()
            topic = str(result.get("topic") or " ".join(context["text"].split()[:9])).strip()[:180]
            annotation = {"topic": topic, "summary": summary}
            if cache_path is not None:
                atomic_json(cache_path, {"fingerprint": fingerprint, **annotation})
            return annotation
        except (KeyError, TypeError, ValueError, RuntimeError):
            continue
    raise RuntimeError("A paragraph's short summary could not be generated after four attempts; source and completed summaries are saved.")


def summarize_recovered_paragraphs(
    plan: dict[str, Any], chunk: dict[str, Any], cache_root: Path | None = None,
) -> dict[str, Any]:
    """Retry independent summaries only; excluded metadata never enters the summary queue."""
    pending = []
    for group in plan["groups"]:
        if group.get("kind") not in {"paragraph", "caption"}:
            group.pop("needs_summary", None)
        elif group.get("needs_summary") or not summary_is_concise(group.get("summary")):
            pending.append(group)
    if not pending:
        return plan
    by_id = {line["id"]: line for line in chunk["lines"]}

    def repair(group: dict[str, Any]) -> None:
        context = {
            "document_title": chunk.get("document_title"),
            "preceding_context": chunk.get("preceding_context", ""),
            "text": join_lines([by_id[index] for index in range(group["start"], group["end"] + 1)], text_key="reading_text"),
        }
        annotation = summarize_one_paragraph(context, cache_root)
        group.update(annotation)
        group.pop("needs_summary", None)

    run_bounded_parallel(pending, repair, SUMMARY_CONCURRENCY)
    return plan


def refine_summary_briefs(root: Path, title: str, chapters: list[dict[str, Any]]) -> None:
    """Introduce merged paragraphs once, preserving all of their source narration."""
    pending = []
    for chapter in chapters:
        for section in chapter["sections"]:
            if not section.get("summary_needs_brief") and summary_is_concise(section.get("summary")):
                continue
            context = {"id": section["paragraph_id"], "document_title": title,
                       "chapter_title": chapter.get("source_heading") or chapter["title"],
                       "text": section["source_text"]}
            fingerprint = hashlib.sha256(json.dumps(
                {"context": context, "style": SUMMARY_STYLE_PROMPT, "model": SCRIPT_MODEL},
                sort_keys=True, ensure_ascii=False,
            ).encode()).hexdigest()
            cached = read_json(root / "summary-briefs" / f"{section['paragraph_id']}.json", {})
            if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint and summary_is_concise(cached.get("summary")):
                section["summary"] = cached["summary"]
                section.pop("summary_needs_brief", None)
            else:
                pending.append((section, context, fingerprint))

    def generate(item: Any) -> None:
        section, context, fingerprint = item
        annotation = summarize_one_paragraph({key: value for key, value in context.items() if key != "id"},
                                             root / "paragraph-summaries")
        section["summary"] = annotation["summary"]
        section.pop("summary_needs_brief", None)
        atomic_json(root / "summary-briefs" / f"{section['paragraph_id']}.json",
                    {"fingerprint": fingerprint, "summary": section["summary"]})

    run_bounded_parallel(pending, generate, SUMMARY_CONCURRENCY)
    for chapter in chapters:
        for section in chapter["sections"]:
            for segment in section["segments"]:
                if segment["kind"] == "summary":
                    segment["lines"] = [section["summary"]]


def clean_script(value: dict[str, Any], fallback_title: str) -> dict[str, Any]:
    sections = []
    for section in value.get("sections", []):
        if not isinstance(section, dict):
            continue
        segments = []
        for segment in section.get("segments", []):
            if not isinstance(segment, dict):
                continue
            speaker = "guide" if segment.get("speaker") == "guide" else "source"
            kind = str(segment.get("kind") or ("source" if speaker == "source" else "explanation"))
            lines = [
                re.sub(r"\s+", " ", str(line)).strip()
                for line in segment.get("lines", [])
                if str(line).strip()
            ]
            if lines:
                segments.append({"speaker": speaker, "kind": kind, "lines": lines})
        if segments:
            sections.append(
                {
                    "title": str(section.get("title") or "Untitled section").strip(),
                    "segments": segments,
                }
            )
    if not sections:
        raise RuntimeError("The script model returned no usable sections.")
    return {
        "title": str(value.get("chapter_title") or fallback_title).strip(),
        "sections": sections,
    }


async def synthesize(text: str, voice: str, output: Path) -> list[tuple[float, str]]:
    boundaries: list[tuple[float, str]] = []
    audio = bytearray()
    communicator = edge_tts.Communicate(text, voice)
    async for item in communicator.stream():
        if item["type"] == "audio":
            audio.extend(item["data"])
        elif item["type"] in ("WordBoundary", "SentenceBoundary"):
            boundaries.append((item["offset"] / 10_000_000, item["text"]))
    output.write_bytes(audio)
    return boundaries


def duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return round(float(result.stdout.strip()), 3)
    except ValueError:
        return 0.0


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def line_offsets(
    lines: list[str], boundaries: list[tuple[float, str]], total_duration: float
) -> list[float]:
    if not boundaries:
        total_chars = max(1, sum(len(line) for line in lines))
        consumed = 0
        offsets = []
        for line in lines:
            offsets.append(total_duration * consumed / total_chars)
            consumed += len(line)
        return offsets
    offsets, pointer = [], 0
    for line in lines:
        target = normalized(line)
        start = boundaries[pointer][0] if pointer < len(boundaries) else total_duration
        accumulated = ""
        while pointer < len(boundaries) and len(accumulated) < len(target):
            accumulated += normalized(boundaries[pointer][1])
            pointer += 1
        offsets.append(round(start, 3))
    return offsets


def concatenate(inputs: list[Path], output: Path) -> None:
    if not inputs:
        raise RuntimeError("There was no audio to assemble.")
    manifest = output.parent / f".{output.stem}-concat.txt"
    lines = []
    for path in inputs:
        escaped = str(path.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'\n")
    manifest.write_text("".join(lines), encoding="utf-8")
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(manifest),
            "-c", "copy", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    manifest.unlink(missing_ok=True)
    if result.returncode:
        raise RuntimeError(f"Audio assembly failed: {result.stderr[-500:]}")


def synthesize_segment_audio(
    segment_dir: Path, segment_number: int, segment: dict[str, Any],
) -> tuple[Path, list[tuple[float, str]], float]:
    """Checkpoint independent voice clips without making completion order meaningful."""
    text = "\n".join(segment["lines"])
    voice = GUIDE_VOICE if segment["speaker"] == "guide" else SOURCE_VOICE
    part = segment_dir / f"{segment_number:03d}-{segment['speaker']}.mp3"
    speech_key = hashlib.sha256((voice + "\n" + text).encode()).hexdigest()
    checkpoint_path = part.with_suffix(".timing.json")
    checkpoint = read_json(checkpoint_path, {})
    cached = (isinstance(checkpoint, dict) and checkpoint.get("speech_key") == speech_key
              and part.is_file() and part.stat().st_size > 0)
    if cached:
        boundaries = [(float(offset), word) for offset, word in checkpoint.get("boundaries", [])]
    else:
        # Never let a failed partial download inherit an older successful checkpoint.
        checkpoint_path.unlink(missing_ok=True)
        try:
            with SPEECH_GATE:
                boundaries = asyncio.run(asyncio.wait_for(
                    synthesize(text, voice, part), timeout=TTS_TIMEOUT_SECONDS,
                ))
            SPEECH_GATE.succeeded()
        except Exception as error:
            if getattr(error, "status", None) in {429, 503} or type(error).__name__ == "NoAudioReceived":
                SPEECH_GATE.throttled(3 + random.uniform(0, 2))
            raise
    with MEDIA_SLOTS:
        part_duration = duration(part)
    if part_duration <= 0:
        raise RuntimeError("The speech provider returned an empty or unreadable audio segment.")
    if not cached:
        atomic_json(checkpoint_path, {"speech_key": speech_key, "boundaries": boundaries})
    return part, boundaries, part_duration


def synthesize_section(
    root: Path,
    chapter_number: int,
    section_number: int,
    section: dict[str, Any],
    speech_pool: ThreadPoolExecutor | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    slug = f"{chapter_number:02d}-{section_number:02d}-{slugify(section['title'])}"
    segment_dir = root / "segments" / slug
    segment_dir.mkdir(parents=True, exist_ok=True)
    section_audio = root / "sections" / f"{slug}.mp3"
    section_audio.parent.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    synced_lines: list[dict[str, Any]] = []
    elapsed = 0.0
    source_start = None
    summary_start = None
    source_end = None
    summary_end = None
    audio_segments = []

    if speech_pool is None:
        rendered = [synthesize_segment_audio(segment_dir, number, segment)
                    for number, segment in enumerate(section["segments"], 1)]
    else:
        # Only active paragraph workers submit clips to this one shared pool.
        # The title, summary and source can render together, but are joined in order.
        futures = [speech_pool.submit(synthesize_segment_audio, segment_dir, number, segment)
                   for number, segment in enumerate(section["segments"], 1)]
        try:
            rendered = [future.result() for future in futures]
        except BaseException:
            for future in futures:
                future.cancel()
            # A retry must not write the same clip while its previous attempt runs.
            wait(futures)
            raise

    for segment, (part, boundaries, part_duration) in zip(section["segments"], rendered):
        lines = segment["lines"]
        if segment["speaker"] == "source" and source_start is None:
            source_start = elapsed
        if segment["speaker"] == "source":
            source_end = elapsed + part_duration
        if segment["kind"] == "summary" and summary_start is None:
            summary_start = elapsed
        if segment["kind"] == "summary":
            summary_end = elapsed + part_duration
        audio_segments.append({"speaker": segment["speaker"], "kind": segment["kind"],
                               "start": round(elapsed, 3), "end": round(elapsed + part_duration, 3)})
        offsets = line_offsets(lines, boundaries, part_duration)
        for line_index, (offset, line) in enumerate(zip(offsets, lines)):
            synced_lines.append(
                {
                    "t": round(elapsed + offset, 3),
                    "end": round(elapsed + (offsets[line_index + 1] if line_index + 1 < len(offsets) else part_duration), 3),
                    "speaker": segment["speaker"],
                    "kind": segment["kind"],
                    "text": line,
                    "paragraph_id": section.get("paragraph_id"),
                    "chapter_n": chapter_number,
                    "section_n": section_number,
                    "page_start": section.get("page_start"),
                    "page_end": section.get("page_end"),
                    "source_text": section.get("source_text") if segment["speaker"] == "source" else None,
                }
            )
        parts.append(part)
        elapsed += part_duration

    with MEDIA_SLOTS:
        concatenate(parts, section_audio)
        actual_duration = duration(section_audio)
    if actual_duration <= 0:
        raise RuntimeError("Paragraph audio assembly produced no playable audio.")
    paragraph_info = {key: section.get(key) for key in (
        "paragraph_id", "content_kind", "source_text", "spoken_text", "summary", "page_start", "page_end",
    )}
    paragraph_info.update(source_start=source_start, source_end=source_end,
                          summary_start=summary_start, summary_end=summary_end,
                          audio_segments=audio_segments, end=actual_duration)
    sync = {
        "n": section_number,
        "title": section["title"],
        "slug": slug,
        "duration": actual_duration,
        "lines": synced_lines,
        **paragraph_info,
    }
    sync_path = root / "sync" / f"{slug}.json"
    atomic_json(sync_path, sync)
    return (
        {
            "n": section_number,
            "title": section["title"],
            "slug": slug,
            "duration": actual_duration,
            **paragraph_info,
        },
        synced_lines,
    )


def synthesize_section_with_retries(
    root: Path, chapter_number: int, section_number: int, section: dict[str, Any],
    speech_pool: ThreadPoolExecutor | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Retry one paragraph with backoff, retaining any completed voice segment."""
    for attempt in range(TTS_ATTEMPTS):
        try:
            return synthesize_section(root, chapter_number, section_number, section, speech_pool)
        except Exception as error:
            # Configuration/storage failures do not improve with more provider calls.
            status = getattr(error, "status", None)
            if isinstance(error, (PermissionError, FileNotFoundError)) or (
                isinstance(status, int) and 400 <= status < 500 and status not in {408, 409, 425, 429}
            ) or attempt + 1 >= TTS_ATTEMPTS:
                raise
            time.sleep(min(30, 2 ** (attempt + 1)) + random.uniform(0, 1))
    raise RuntimeError("Speech generation exhausted its attempts.")


def narrate_paragraphs_parallel(
    root: Path, job_id: str, chapters: list[dict[str, Any]],
) -> dict[tuple[int, int], tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Generate paragraph audio concurrently, leaving global timing to ordered assembly."""
    items = [(chapter_number, section_number, section)
             for chapter_number, chapter in enumerate(chapters, 1)
             for section_number, section in enumerate(chapter["sections"], 1)]
    workers = min(TTS_CONCURRENCY, len(items))
    results: dict[tuple[int, int], tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    started = time.monotonic()
    completed_weight = 0

    def weight(item: tuple[int, int, dict[str, Any]]) -> int:
        return max(1, sum(len(line) for segment in item[2]["segments"] for line in segment["lines"]))

    total_weight = sum(weight(item) for item in items)
    save_job(job_id, narration_concurrency=workers, speech_concurrency=TTS_CLIP_CONCURRENCY,
             stage=f"Narrating {workers} paragraphs with up to {TTS_CLIP_CONCURRENCY} voice clips at once",
             estimated_seconds_remaining=None)

    def completed(item: tuple[int, int, dict[str, Any]], result: Any) -> None:
        nonlocal completed_weight
        chapter_number, section_number, _ = item
        results[(chapter_number, section_number)] = result
        completed_weight += weight(item)
        count = len(results)
        # Use observed wall-clock throughput, not summed worker runtimes. Wait for
        # a first wave of completions before presenting a speculative parallel ETA.
        estimate = None
        if count >= workers:
            estimate = round((time.monotonic() - started) * (total_weight - completed_weight) / max(1, completed_weight))
        save_job(job_id, completed_chunks=count,
                 stage=f"Narrating: {count} of {len(items)} paragraphs complete (up to {TTS_CLIP_CONCURRENCY} voice clips at once)",
                 current_chapter=chapter_number, current_section=section_number,
                 progress_percent=42 + round(52 * count / len(items), 1),
                 estimated_seconds_remaining=estimate)

    with ThreadPoolExecutor(max_workers=TTS_CLIP_CONCURRENCY, thread_name_prefix="pdf2podcast-speech") as speech_pool:
        run_bounded_parallel(
            items,
            lambda item: synthesize_section_with_retries(root, item[0], item[1], item[2], speech_pool),
            TTS_CONCURRENCY,
            completed,
        )
    return results


def annotation_fingerprint(chunk: dict[str, Any]) -> str:
    """Keep checkpoints sensitive to both immutable source and its annotation context."""
    return hashlib.sha256(json.dumps(
        {"chunk": chunk, "prompt": SCRIPT_SYSTEM_PROMPT, "summary_style": SUMMARY_STYLE_PROMPT, "model": SCRIPT_MODEL},
        sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()


def prepare_annotation_contexts(title: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Predict context from printed headings; ordered validation remains authoritative."""
    prepared = []
    references, metadata = False, False
    for index, original in enumerate(chunks):
        prepared.append({**original, "document_title": title,
                         "preceding_context": join_lines(chunks[index - 1]["lines"], text_key="reading_text")[-1200:] if index else "",
                         "reference_section": references, "metadata_section": metadata})
        for line in original["lines"]:
            text = line["text"].strip()
            if REFERENCE_HEADING.fullmatch(text):
                references, metadata = True, False
            elif METADATA_HEADING.fullmatch(text):
                references, metadata = False, True
            elif (line.get("bold") and not line.get("caption_hint") and not line.get("figure_text_hint")
                  and len(text) <= 160 and len(text.split()) <= 18 and not text.endswith((".", ";", ","))):
                references, metadata = False, False
    return prepared


def load_annotation_proposal(
    root: Path, title: str, chunk: dict[str, Any], index: int, total: int,
) -> tuple[str, dict[str, Any]]:
    """Save parallel API output before reconciliation, so a failed neighbour wastes no calls."""
    fingerprint = annotation_fingerprint(chunk)
    cached = read_json(root / "annotations" / f"{index:03d}.json", {})
    if isinstance(cached, dict) and cached.get("source_fingerprint") == fingerprint:
        return fingerprint, cached
    proposal_path = root / "annotation-proposals" / f"{index:03d}.json"
    cached = read_json(proposal_path, {})
    if (isinstance(cached, dict) and cached.get("source_fingerprint") == fingerprint
            and isinstance(cached.get("proposal"), dict)):
        return fingerprint, cached["proposal"]
    proposal = create_script(title, chunk, index, total, finalize=False)
    atomic_json(proposal_path, {"source_fingerprint": fingerprint, "proposal": proposal})
    return fingerprint, proposal


def annotate_document_parallel(
    root: Path, job_id: str, title: str, chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fan out expensive mapping calls, then reconcile actual chapter state in PDF order."""
    prepared = prepare_annotation_contexts(title, chunks)
    # A resumed job already knows its earlier chapter states. Use them as hints
    # to avoid speculative calls for completed batches; fingerprints still decide reuse.
    for index in range(1, len(prepared)):
        previous = read_json(root / "annotations" / f"{index:03d}.json", {})
        if isinstance(previous, dict) and previous.get("source_fingerprint"):
            prepared[index]["reference_section"] = bool(previous.get("reference_section"))
            prepared[index]["metadata_section"] = bool(previous.get("metadata_section"))
    items = list(enumerate(prepared, 1))
    workers = min(SCRIPT_CONCURRENCY, len(items))
    proposals: dict[int, tuple[str, dict[str, Any]]] = {}
    started = time.monotonic()
    save_job(job_id, script_concurrency=workers, summary_concurrency=SUMMARY_CONCURRENCY,
             stage=f"Mapping the paper with up to {workers} requests at once", estimated_seconds_remaining=None)

    def mapped(item: Any, result: Any) -> None:
        proposals[item[0]] = result
        count = len(proposals)
        remaining = round((time.monotonic() - started) * (len(items) - count) / count) if count >= workers else None
        save_job(job_id, completed_chunks=count,
                 stage=f"Mapping paper: {count} of {len(items)} batches complete (up to {workers} at once)",
                 progress_percent=5 + round(22 * count / len(items), 1), estimated_seconds_remaining=remaining)

    run_bounded_parallel(
        items, lambda item: load_annotation_proposal(root, title, item[1], item[0], len(items)), workers, mapped,
    )

    plans = []
    contexts = []
    context_retries = 0
    for index, predicted in items:
        actual = {**predicted,
                  "reference_section": bool(plans and plans[-1].get("reference_section")),
                  "metadata_section": bool(plans and plans[-1].get("metadata_section"))}
        fingerprint, proposal = proposals[index]
        if fingerprint != annotation_fingerprint(actual):
            # A guessed heading is never allowed to change what is retained or
            # omitted. Replay only the affected batch with its true preceding state.
            context_retries += 1
            save_job(job_id, stage=f"Resolving chapter context for batch {index} of {len(items)}",
                     context_retries=context_retries, estimated_seconds_remaining=None)
            fingerprint, proposal = load_annotation_proposal(root, title, actual, index, len(items))
        plan = validate_groups(proposal, actual)
        plan["source_fingerprint"] = fingerprint
        plans.append(plan)
        contexts.append(actual)
        save_job(job_id, stage=f"Reconciling paper order: batch {index} of {len(items)}",
                 progress_percent=27 + round(3 * index / len(items), 1), estimated_seconds_remaining=None)

    completed_summaries = 0

    def summarized(item: Any, plan: Any) -> None:
        nonlocal completed_summaries
        index = item[0]
        plans[index - 1] = plan
        atomic_json(root / "annotations" / f"{index:03d}.json", plan)
        completed_summaries += 1
        save_job(job_id, completed_chunks=completed_summaries,
                 stage=f"Preparing summaries: {completed_summaries} of {len(items)} batches complete",
                 progress_percent=30 + round(10 * completed_summaries / len(items), 1), estimated_seconds_remaining=None)

    run_bounded_parallel(
        list(enumerate(plans, 1)),
        lambda item: summarize_recovered_paragraphs(item[1], contexts[item[0] - 1], root / "paragraph-summaries"),
        SUMMARY_CONCURRENCY, summarized,
    )
    return plans


def process_job(job_id: str) -> None:
    root = safe_job(job_id)
    started = time.monotonic()
    try:
        save_job(
            job_id,
            status="processing",
            stage="Extracting readable text",
            progress_percent=2,
            error=None,
            started_at=now(),
        )
        document = extract_document(root / "input.pdf", load_job(job_id).get("filename", "paper.pdf"))
        title = document["title"]
        chunks = annotation_batches(document, SCRIPT_CHUNK_CHARS)
        atomic_json(root / "extracted.json", document)
        (root / "extracted.txt").write_text(
            "\n".join(f"[page {line['page']}, line {line['id']}] {line['text']}" for line in document["lines"]), encoding="utf-8",
        )
        save_job(
            job_id,
            title=title,
            page_count=document["page_count"],
            reading_mode="paper_companion",
            pipeline_version=4,
            total_chunks=len(chunks),
            completed_chunks=0,
            stage="Identifying original headings and paragraphs",
            progress_percent=5,
        )

        plans = annotate_document_parallel(root, job_id, title, chunks)

        chapters, omitted = chapters_from_plans(document, plans)
        save_job(job_id, stage="Preparing short paragraph summaries", estimated_seconds_remaining=None)
        refine_summary_briefs(root, title, chapters)
        atomic_json(root / "extraction-omissions.json", omitted)
        content = {
            "version": 4,
            "reading_mode": "paper_companion",
            "title": title,
            "voices": {"source": SOURCE_VOICE, "guide": GUIDE_VOICE},
            "chapters": [
                {**chapter, "n": index}
                for index, chapter in enumerate(chapters, 1)
            ],
        }
        atomic_json(root / "content.json", content)
        total_sections = sum(len(chapter["sections"]) for chapter in chapters)
        save_job(
            job_id,
            completed_chunks=0,
            total_chunks=total_sections,
            chapter_count=len(chapters),
            section_count=total_sections,
            paragraph_count=total_sections,
            stage="Preparing original-text narration and paragraph summaries",
            progress_percent=42,
        )

        # Network completion order must never influence paragraph order or timestamps.
        recordings = narrate_paragraphs_parallel(root, job_id, chapters)
        manifest_chapters = []
        all_audio: list[Path] = []
        global_lines: list[dict[str, Any]] = []
        global_time = 0.0
        for chapter_number, chapter in enumerate(chapters, 1):
            manifest_sections = []
            for section_number, section in enumerate(chapter["sections"], 1):
                section_info, lines = recordings[(chapter_number, section_number)]
                section_info["start"] = round(global_time, 3)
                for timestamp in ("source_start", "source_end", "summary_start", "summary_end", "end"):
                    if section_info.get(timestamp) is not None:
                        section_info[timestamp] = round(global_time + section_info[timestamp], 3)
                section_info["audio_segments"] = [
                    {**segment, "start": round(global_time + segment["start"], 3),
                     "end": round(global_time + segment["end"], 3)}
                    for segment in section_info.get("audio_segments", [])
                ]
                for line in lines:
                    global_lines.append({**line, "t": round(global_time + line["t"], 3),
                                         "end": round(global_time + line["end"], 3)})
                manifest_sections.append(section_info)
                audio_path = root / "sections" / f"{section_info['slug']}.mp3"
                all_audio.append(audio_path)
                global_time += section_info["duration"]
            manifest_chapters.append(
                {
                    "n": chapter_number,
                    "title": chapter["title"],
                    "source_heading": chapter.get("source_heading"),
                    "level": chapter.get("level", 1),
                    "page_start": chapter.get("page_start"),
                    "page_end": chapter.get("page_end"),
                    "sections": manifest_sections,
                }
            )

        save_job(
            job_id,
            stage="Assembling the finished episode",
            progress_percent=96,
            estimated_seconds_remaining=30,
        )
        concatenate(all_audio, root / "audio.mp3")
        full_duration = duration(root / "audio.mp3")
        manifest = {
            "version": 4,
            "reading_mode": "paper_companion",
            "title": title,
            "voices": {"source": SOURCE_VOICE, "guide": GUIDE_VOICE},
            "duration": full_duration,
            "chapters": manifest_chapters,
        }
        atomic_json(root / "manifest.json", manifest)
        atomic_json(
            root / "transcript.json",
            {"version": 4, "reading_mode": "paper_companion", "duration": full_duration, "lines": global_lines},
        )
        (root / "transcript.txt").write_text(
            "\n\n".join(
                f"[{'Guide' if line['speaker'] == 'guide' else 'Source'}]\n{line['text']}"
                for line in global_lines
            ),
            encoding="utf-8",
        )
        save_job(
            job_id,
            status="complete",
            stage="Ready to listen",
            progress_percent=100,
            estimated_seconds_remaining=0,
            completed_chunks=total_sections,
            audio_bytes=(root / "audio.mp3").stat().st_size,
            duration_seconds=full_duration,
            completed_at=now(),
            processing_seconds=round(time.monotonic() - started),
            error=None,
        )
    except Exception as error:
        save_job(
            job_id,
            status="failed",
            stage="Conversion failed",
            error=str(error),
            estimated_seconds_remaining=0,
            failed_at=now(),
        )


def retry_job(job_id: str, keep_retrying: bool) -> dict[str, Any]:
    """Requeue the same saved PDF, never create another library entry or concurrent run."""
    with JOB_CONTROL_LOCK:
        job = load_job(job_id)
        if job.get("status") in {"complete", "completed"}:
            return job
        if not (safe_job(job_id) / "input.pdf").is_file():
            raise ValueError("The saved PDF is missing; upload the document again.")
        if job.get("status") in {"queued", "processing", "running"}:
            return save_job(job_id, retry_until_complete=keep_retrying)
        job = save_job(job_id, status="queued", stage="Queued to resume saved conversion",
                       retry_until_complete=keep_retrying, next_retry_at=None,
                       last_error=job.get("error"), error=None, estimated_seconds_remaining=None)
        UPLOADS.put(job_id)
        return job


def schedule_retry(job_id: str) -> None:
    with JOB_CONTROL_LOCK:
        job = load_job(job_id)
        if job.get("status") != "failed" or not job.get("retry_until_complete"):
            return
        error = str(job.get("error") or "")
        if any(marker in error.lower() for marker in ("insufficient_quota", "invalid_api_key", "http 401", "http 402", "http 403", "password protected", "no extractable text was found")):
            save_job(job_id, stage="Needs attention before retrying", retry_until_complete=False)
            return
        attempts = int(job.get("retry_attempts") or 0) + 1
        delay = min(900, 30 * (2 ** min(attempts - 1, 5)))
        due = datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat()
        save_job(job_id, status="queued", stage=f"Automatic retry {attempts} in {delay} seconds",
                 retry_attempts=attempts, next_retry_at=due, last_error=error,
                 estimated_seconds_remaining=delay)

        def enqueue() -> None:
            with JOB_CONTROL_LOCK:
                current = load_job(job_id)
                if current.get("status") == "queued" and current.get("next_retry_at") == due:
                    save_job(job_id, stage="Resuming saved conversion", next_retry_at=None)
                    UPLOADS.put(job_id)

        timer = threading.Timer(delay, enqueue)
        timer.daemon = True
        timer.start()


def worker() -> None:
    while True:
        job_id = UPLOADS.get()
        try:
            with JOB_CONTROL_LOCK:
                if load_job(job_id).get("status") != "queued":
                    continue
                save_job(job_id, status="processing", stage="Resuming conversion", next_retry_at=None)
            process_job(job_id)
            schedule_retry(job_id)
        finally:
            UPLOADS.task_done()


def recover_jobs() -> None:
    JOBS.mkdir(parents=True, exist_ok=True)
    for path in JOBS.glob("pdf-*/job.json"):
        job = read_json(path)
        if isinstance(job, dict) and job.get("retry_until_complete") and job.get("status") in {"queued", "processing", "running", "failed", "aborted"}:
            save_job(job["job_id"], status="queued", stage="Resuming after server restart", next_retry_at=None)
            UPLOADS.put(job["job_id"])
            continue
        if isinstance(job, dict) and job.get("status") in {"queued", "processing", "running"}:
            job.update(
                {
                    "status": "aborted",
                    "stage": "Interrupted by server restart",
                    "error": "The conversion was interrupted and can be uploaded again.",
                    "updated_at": now(),
                }
            )
            atomic_json(path, job)


def parse_pdf_upload(body: bytes, content_type: str) -> tuple[str, bytes]:
    match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type)
    if not match:
        raise ValueError("Expected multipart/form-data with a boundary.")
    boundary = (match.group(1) or match.group(2)).encode()
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        raw_headers, data = part.split(b"\r\n\r\n", 1)
        headers = raw_headers.decode("utf-8", errors="replace")
        if 'name="pdf"' not in headers:
            continue
        filename_match = re.search(r'filename="([^"]*)"', headers)
        filename = Path(filename_match.group(1) if filename_match else "upload.pdf").name
        data = data.rstrip(b"\r\n-")
        if not data.startswith(b"%PDF"):
            raise ValueError("The uploaded file is not a PDF.")
        return filename or "upload.pdf", data
    raise ValueError('The multipart request must contain a "pdf" file field.')


class Handler(BaseHTTPRequestHandler):
    server_version = "Pdf2Podcast/2"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)

    def cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Range, If-Range")
        self.send_header("Access-Control-Expose-Headers", "Accept-Ranges, Content-Length, Content-Range")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def json_response(self, status: int, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def error_response(self, status: int, message: str, code: str) -> None:
        self.json_response(status, {"detail": message, "code": code})

    def authorized(self) -> bool:
        expected = os.environ.get("PDF2PODCAST_MOBILE_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        valid = bool(expected) and hmac.compare_digest(supplied, f"Bearer {expected}")
        if not valid:
            self.error_response(401, "A valid Pdf2Podcast mobile token is required.", "unauthorized")
        return valid

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        retry_match = re.fullmatch(r"/api/audio/jobs/(pdf-[a-f0-9]{16})/retry", self.path.rstrip("/"))
        if retry_match:
            if not self.authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 4096:
                    raise ValueError("Retry request is too large.")
                options = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(options, dict) or type(options.get("retry_until_complete", False)) is not bool:
                    raise ValueError("retry_until_complete must be true or false.")
                job = retry_job(retry_match.group(1), options.get("retry_until_complete", False))
                self.json_response(202, public_job(job))
            except FileNotFoundError:
                self.error_response(404, "Conversion not found.", "job_not_found")
            except (OSError, ValueError) as error:
                self.error_response(400, str(error), "invalid_retry")
            return
        if self.path.rstrip("/") != "/api/audio/upload":
            self.error_response(404, "Route not found.", "not_found")
            return
        if not self.authorized():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self.error_response(413, "PDF is empty or exceeds the upload limit.", "upload_too_large")
            return
        try:
            filename, data = parse_pdf_upload(
                self.rfile.read(length), self.headers.get("Content-Type", "")
            )
            job_id = f"pdf-{uuid.uuid4().hex[:16]}"
            root = safe_job(job_id)
            root.mkdir(parents=True)
            (root / "input.pdf").write_bytes(data)
            job = {
                "job_id": job_id,
                "filename": filename,
                "pipeline_version": 3,
                "reading_mode": "paper_companion",
                "title": Path(filename).stem,
                "status": "queued",
                "stage": "Waiting to begin",
                "progress_percent": 0,
                "estimated_seconds_remaining": None,
                "completed_chunks": 0,
                "total_chunks": None,
                "chapter_count": None,
                "section_count": None,
                "page_count": None,
                "audio_bytes": None,
                "error": None,
                "created_at": now(),
                "updated_at": now(),
                "source_sha256": hashlib.sha256(data).hexdigest(),
            }
            atomic_json(root / "job.json", job)
            UPLOADS.put(job_id)
            self.json_response(202, public_job(job))
        except (OSError, ValueError) as error:
            self.error_response(400, str(error), "invalid_upload")

    def do_GET(self) -> None:
        if self.path == "/health":
            self.json_response(200, {"ok": True, "service": "pdf2podcast", "version": 2})
            return
        path, _, query = self.path.partition("?")
        if not authorized_playback(path, query) and not self.authorized():
            return
        if path.rstrip("/") == "/api/audio/jobs":
            limit_match = re.search(r"(?:^|&)limit=(\d+)", query)
            limit = int(limit_match.group(1)) if limit_match else 100
            self.json_response(200, {"jobs": list_jobs(limit)})
            return
        match = re.fullmatch(
            r"/api/audio/jobs/(pdf-[a-f0-9]{16})(?:/(audio|transcript|manifest|sync))?",
            path.rstrip("/"),
        )
        if not match:
            self.error_response(404, "Route not found.", "not_found")
            return
        job_id, artifact = match.groups()
        try:
            job = load_job(job_id)
        except (FileNotFoundError, ValueError):
            self.error_response(404, "Conversion not found.", "job_not_found")
            return
        if artifact is None:
            self.json_response(200, public_job(job))
            return
        choices = {
            "audio": ("audio.mp3", "audio/mpeg"),
            "transcript": ("transcript.txt", "text/plain; charset=utf-8"),
            "manifest": ("manifest.json", "application/json; charset=utf-8"),
            "sync": ("transcript.json", "application/json; charset=utf-8"),
        }
        filename, content_type = choices[artifact]
        file_path = safe_job(job_id) / filename
        if not file_path.is_file():
            self.error_response(409, "This artifact is not ready yet.", "artifact_not_ready")
            return
        if artifact == "audio":
            send_audio(self, file_path)
            return
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.end_headers()
        with file_path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not os.environ.get("PDF2PODCAST_MOBILE_TOKEN"):
        raise SystemExit("PDF2PODCAST_MOBILE_TOKEN is required")
    recover_jobs()
    threading.Thread(target=worker, name="pdf2podcast-worker", daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Pdf2Podcast v2 listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
