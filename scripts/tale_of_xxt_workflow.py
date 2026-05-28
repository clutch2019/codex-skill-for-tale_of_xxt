#!/usr/bin/env python3
"""Prepare and transcribe Douban podcast episodes for outline generation."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DOUBAN_API = "https://www.douban.com/api/v2/folco/podcast_episode/{episode_id}"
OPENROUTER_TRANSCRIPTIONS_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
OPENROUTER_ASR_MODEL = "qwen/qwen3-asr-flash-2026-02-10"
DEFAULT_WINDOW_SECONDS = 120


class WorkflowError(RuntimeError):
    pass


def fetch_json(url: str, *, timeout: int = 30) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 tale_of_xxt-skill/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1000]
        raise WorkflowError(f"HTTP {exc.code} while fetching {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise WorkflowError(f"Network error while fetching {url}: {exc}") from exc


def download_file(url: str, output: Path, *, timeout: int = 60) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 tale_of_xxt-skill/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response, output.open("wb") as f:
        shutil.copyfileobj(response, f)


def post_json(url: str, headers: dict[str, str], payload: dict[str, Any], *, timeout: int) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw[:1000]}
        return exc.code, parsed
    except Exception as exc:
        return 0, {"error": repr(exc)}


def parse_episode_id(douban_url: str) -> str:
    parsed = urllib.parse.urlparse(douban_url)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("podcast_episode", "episode_id", "id"):
        if key in query and query[key]:
            match = re.search(r"\d+", query[key][0])
            if match:
                return match.group(0)

    patterns = [
        r"/podcast_episode/(\d+)",
        r"/podcast/episode/(\d+)",
        r"podcast_episode/(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, douban_url)
        if match:
            return match.group(1)

    raise WorkflowError(f"Could not parse Douban podcast episode id from: {douban_url}")


def slugify(value: str, fallback: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value, flags=re.UNICODE).strip("_")
    value = re.sub(r"_+", "_", value)
    return value[:80] or fallback


def format_time(seconds: int | float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def probe_duration_seconds(audio_path: Path) -> int | None:
    if not shutil.which("ffprobe"):
        return None
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return None
    try:
        return int(float(result.stdout.strip()))
    except ValueError:
        return None


def split_audio(audio_path: Path, chunks_dir: Path, window_seconds: int, basename: str) -> list[Path]:
    if not shutil.which("ffmpeg"):
        raise WorkflowError("ffmpeg is required for audio splitting but was not found in PATH.")

    chunks_dir.mkdir(parents=True, exist_ok=True)
    for old_chunk in chunks_dir.glob(f"{basename}_chunk_*.mp3"):
        old_chunk.unlink()

    output_pattern = chunks_dir / f"{basename}_chunk_%03d.mp3"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-f",
        "segment",
        "-segment_time",
        str(window_seconds),
        "-reset_timestamps",
        "1",
        "-map",
        "0:a",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "64k",
        str(output_pattern),
    ]
    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise WorkflowError(f"ffmpeg failed:\n{result.stderr.strip()}")
    return sorted(chunks_dir.glob(f"{basename}_chunk_*.mp3"))


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_episode(args: argparse.Namespace) -> int:
    episode_id = parse_episode_id(args.url)
    metadata = fetch_json(DOUBAN_API.format(episode_id=episode_id), timeout=args.timeout)
    audio_url = metadata.get("audio_href")
    if not audio_url:
        raise WorkflowError("Douban metadata did not include audio_href.")

    title = str(metadata.get("title") or f"douban_podcast_episode_{episode_id}")
    podcast_title = str((metadata.get("podcast") or {}).get("title") or "")
    duration_seconds = metadata.get("duration_seconds")
    if not isinstance(duration_seconds, int):
        duration_seconds = None

    basename = args.basename or f"{episode_id}_{slugify(title, 'episode')}"
    workdir = Path(args.workdir).expanduser().resolve() if args.workdir else Path.cwd() / basename
    audio_dir = workdir / "audio"
    chunks_dir = workdir / "chunks"
    audio_ext = Path(urllib.parse.urlparse(audio_url).path).suffix or ".mp3"
    audio_path = audio_dir / f"{basename}{audio_ext}"

    workdir.mkdir(parents=True, exist_ok=True)
    if args.force or not audio_path.exists():
        print(f"Downloading audio: {audio_url}")
        download_file(audio_url, audio_path, timeout=args.timeout)
    else:
        print(f"Using existing audio: {audio_path}")

    probed_duration = probe_duration_seconds(audio_path)
    if probed_duration:
        duration_seconds = probed_duration
    if not duration_seconds:
        raise WorkflowError("Could not determine audio duration from Douban metadata or ffprobe.")

    chunk_paths = split_audio(audio_path, chunks_dir, args.window_seconds, basename)
    chunks = []
    for index, chunk_path in enumerate(chunk_paths):
        start_seconds = index * args.window_seconds
        end_seconds = min(start_seconds + args.window_seconds, duration_seconds)
        chunks.append(
            {
                "index": index,
                "path": str(chunk_path.relative_to(workdir)),
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
                "start": format_time(start_seconds),
                "end": format_time(end_seconds),
            }
        )

    manifest = {
        "schema": "tale_of_xxt-manifest-v1",
        "source": {
            "input_url": args.url,
            "episode_id": episode_id,
            "douban_api_url": DOUBAN_API.format(episode_id=episode_id),
            "audio_url": audio_url,
        },
        "episode": {
            "title": title,
            "podcast_title": podcast_title,
            "duration_seconds": duration_seconds,
            "duration": format_time(duration_seconds),
            "published_at": metadata.get("create_at"),
            "douban_url": metadata.get("url") or f"https://www.douban.com/podcast_episode/{episode_id}",
            "description_html": metadata.get("description_html") or "",
        },
        "settings": {
            "window_seconds": args.window_seconds,
            "asr_model": OPENROUTER_ASR_MODEL,
        },
        "paths": {
            "workdir": str(workdir),
            "audio": str(audio_path.relative_to(workdir)),
            "chunks_dir": str(chunks_dir.relative_to(workdir)),
            "asr_jsonl": "asr_results/asr.jsonl",
            "asr_markdown": "asr.md",
            "summary_prompt": "summary_prompt.md",
            "final_outline": "coarse_summary_outline.md",
        },
        "chunks": chunks,
    }

    manifest_path = workdir / "manifest.json"
    write_manifest(manifest_path, manifest)
    print(f"Wrote manifest: {manifest_path}")
    print(f"Prepared {len(chunks)} chunk(s) in {chunks_dir}")
    return 0


def read_done_indices(jsonl_path: Path) -> set[int]:
    done: set[int] = set()
    if not jsonl_path.exists():
        return done
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("ok") and isinstance(row.get("index"), int):
                done.add(row["index"])
    return done


def transcribe_chunk(
    workdir: Path,
    chunk: dict[str, Any],
    api_key: str,
    timeout: int,
    retries: int,
) -> dict[str, Any]:
    audio_path = workdir / chunk["path"]
    if not audio_path.exists():
        return {**chunk, "ok": False, "error": f"missing audio chunk: {audio_path}"}

    audio_format = audio_path.suffix.lower().lstrip(".") or "mp3"
    base64_audio = base64.b64encode(audio_path.read_bytes()).decode("utf-8")
    payload = {
        "model": OPENROUTER_ASR_MODEL,
        "input_audio": {
            "data": base64_audio,
            "format": audio_format,
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error: Any = None
    for attempt in range(retries + 1):
        status, result = post_json(OPENROUTER_TRANSCRIPTIONS_URL, headers, payload, timeout=timeout)
        if 200 <= status < 300 and isinstance(result, dict) and "text" in result:
            return {
                **chunk,
                "ok": True,
                "model": OPENROUTER_ASR_MODEL,
                "text": str(result.get("text") or "").strip(),
            }
        last_error = {"status_code": status, "response": result}
        if attempt < retries:
            time.sleep(min(20, 2**attempt + random.random()))

    return {**chunk, "ok": False, "model": OPENROUTER_ASR_MODEL, "error": last_error}


def transcribe_episode(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = load_manifest(manifest_path)
    workdir = Path(manifest["paths"]["workdir"]).expanduser().resolve()
    output = Path(args.output).expanduser().resolve() if args.output else workdir / manifest["paths"]["asr_jsonl"]

    api_key = args.api_key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print(
            "Missing OpenRouter API key. Set OPENROUTER_API_KEY or pass --api-key before running ASR.",
            file=sys.stderr,
        )
        return 2

    output.parent.mkdir(parents=True, exist_ok=True)
    chunks = manifest.get("chunks") or []
    if not chunks:
        raise WorkflowError("Manifest has no chunks.")

    wanted_indices = None
    if args.start is not None or args.end is not None:
        start = args.start if args.start is not None else 0
        end = args.end if args.end is not None else len(chunks) - 1
        if start < 0 or end < start or end >= len(chunks):
            raise WorkflowError(f"Invalid chunk range {start}-{end}; valid range is 0-{len(chunks) - 1}.")
        wanted_indices = set(range(start, end + 1))

    done = read_done_indices(output)
    remaining = [
        chunk
        for chunk in chunks
        if chunk.get("index") not in done and (wanted_indices is None or chunk.get("index") in wanted_indices)
    ]
    if not remaining:
        print("All requested chunks are already transcribed.")
        return 0

    print(f"Transcribing {len(remaining)} chunk(s); output={output}")
    failures = 0
    with output.open("a", encoding="utf-8") as f:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(transcribe_chunk, workdir, chunk, api_key, args.timeout, args.retries): chunk
                for chunk in remaining
            }
            for future in concurrent.futures.as_completed(futures):
                row = future.result()
                if not row.get("ok"):
                    failures += 1
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                status = "ok" if row.get("ok") else "failed"
                print(f"{status}: {int(row.get('index', -1)):03d} {row.get('start')} - {row.get('end')}")

    return 1 if failures else 0


def read_asr_rows(jsonl_path: Path) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    rows: dict[int, dict[str, Any]] = {}
    errors: dict[int, dict[str, Any]] = {}
    if not jsonl_path.exists():
        return rows, errors
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            index = row.get("index")
            if not isinstance(index, int):
                continue
            if row.get("ok"):
                rows[index] = row
            else:
                errors[index] = row
    return rows, errors


def compute_ranges(total: int, agents: int) -> list[tuple[int, int]]:
    if total <= 0:
        return []
    agents = max(1, min(agents, total))
    base, extra = divmod(total, agents)
    ranges = []
    start = 0
    for i in range(agents):
        size = base + (1 if i < extra else 0)
        if size <= 0:
            continue
        end = start + size - 1
        ranges.append((start, end))
        start = end + 1
    return ranges


def print_ranges(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = load_manifest(manifest_path)
    workdir = Path(manifest["paths"]["workdir"]).expanduser().resolve()
    chunks = manifest.get("chunks") or []
    ranges = compute_ranges(len(chunks), args.agents)
    rows = [
        {
            "start": start,
            "end": end,
            "size": end - start + 1,
            "output": str(workdir / "asr_results" / f"part_{start:03d}_{end:03d}.jsonl"),
        }
        for start, end in ranges
    ]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(f"{row['start']:03d}-{row['end']:03d}\t{row['size']}\t{row['output']}")
    return 0


def validate_asr(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = load_manifest(manifest_path)
    workdir = Path(manifest["paths"]["workdir"]).expanduser().resolve()
    inputs = (
        [Path(p).expanduser().resolve() for p in args.input]
        if args.input
        else sorted((workdir / "asr_results").glob("*.jsonl"))
    )

    ok_seen: dict[int, list[str]] = {}
    failed_seen: dict[int, list[str]] = {}
    for input_path in inputs:
        if not input_path.exists():
            raise WorkflowError(f"Missing ASR JSONL input: {input_path}")
        with input_path.open("r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise WorkflowError(f"Invalid JSON in {input_path}:{line_number}: {exc}") from exc
                index = row.get("index")
                if not isinstance(index, int):
                    continue
                target = ok_seen if row.get("ok") else failed_seen
                target.setdefault(index, []).append(f"{input_path}:{line_number}")

    total = len(manifest.get("chunks") or [])
    missing = [idx for idx in range(total) if idx not in ok_seen]
    duplicates = {idx: places for idx, places in ok_seen.items() if len(places) > 1}
    failures = {idx: places for idx, places in failed_seen.items() if idx not in ok_seen}

    print(f"ASR inputs: {len(inputs)}")
    print(f"Expected chunks: {total}")
    print(f"OK chunks: {len(ok_seen)}")
    if missing:
        print(f"Missing OK chunks: {', '.join(f'{idx:03d}' for idx in missing)}")
    if duplicates:
        print(f"Duplicate OK chunks: {', '.join(f'{idx:03d}' for idx in sorted(duplicates))}")
    if failures:
        print(f"Failed-only chunks: {', '.join(f'{idx:03d}' for idx in sorted(failures))}")

    return 1 if missing or duplicates or failures else 0


def build_asr_markdown(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = load_manifest(manifest_path)
    workdir = Path(manifest["paths"]["workdir"]).expanduser().resolve()
    jsonl_paths = (
        [Path(p).expanduser().resolve() for p in args.input]
        if args.input
        else [workdir / manifest["paths"]["asr_jsonl"]]
    )
    output = Path(args.output).expanduser().resolve() if args.output else workdir / manifest["paths"]["asr_markdown"]
    prompt_output = (
        Path(args.prompt_output).expanduser().resolve()
        if args.prompt_output
        else workdir / manifest["paths"]["summary_prompt"]
    )

    rows: dict[int, dict[str, Any]] = {}
    errors: dict[int, dict[str, Any]] = {}
    for jsonl_path in jsonl_paths:
        if not jsonl_path.exists():
            raise WorkflowError(f"Missing ASR JSONL input: {jsonl_path}")
        path_rows, path_errors = read_asr_rows(jsonl_path)
        rows.update(path_rows)
        errors.update(path_errors)
    chunks = manifest.get("chunks") or []
    missing = [chunk["index"] for chunk in chunks if chunk["index"] not in rows]

    episode = manifest["episode"]
    lines = [
        f"# {episode.get('title', 'Douban Podcast Episode')} ASR",
        "",
        f"- 播客：{episode.get('podcast_title') or '[未知]'}",
        f"- 豆瓣单集：{episode.get('douban_url')}",
        f"- 音频时长：{episode.get('duration')}",
        f"- 时间粒度：{manifest['settings'].get('window_seconds', DEFAULT_WINDOW_SECONDS)} 秒",
        f"- ASR：{manifest['settings'].get('asr_model', OPENROUTER_ASR_MODEL)}",
        "",
        "## 时间轴转写",
        "",
    ]

    for chunk in chunks:
        index = chunk["index"]
        row = rows.get(index)
        text = str((row or {}).get("text") or "").strip() or "[未转写成功]"
        lines.extend([f"### {chunk['start']} - {chunk['end']}", "", text, ""])

    if missing or errors:
        lines.extend(["## 校验", ""])
        if missing:
            lines.append(f"- 缺失片段：{', '.join(f'{idx:03d}' for idx in missing)}")
        failed = sorted(errors)
        if failed:
            lines.append(f"- 失败片段：{', '.join(f'{idx:03d}' for idx in failed)}")
        lines.append("")

    output.parent.mkdir(parents=True, exist_ok=True)
    prompt_output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    prompt_output.write_text(make_summary_prompt(manifest, output), encoding="utf-8")
    print(f"Wrote ASR markdown: {output}")
    print(f"Wrote summary prompt: {prompt_output}")
    return 1 if missing else 0


def make_summary_prompt(manifest: dict[str, Any], asr_path: Path) -> str:
    episode = manifest["episode"]
    description_html = str(episode.get("description_html") or "").strip()
    return f"""# tale_of_xxt 播客粗时间粒度总结任务

输入文件：`{asr_path}`

请基于该 ASR 文件生成 `coarse_summary_outline.md`，目标是：

1. 先给出“专名校正说明”。优先从豆瓣单集简介、单集酒单/节目单、播客标题、主播明确公布的信息中提取专名。ASR 中疑似错字不要直接采用。
2. 生成“粗时间粒度内容总结”：用表格列出时间窗和该时间窗内的内容总结。时间窗可以合并多个 2 分钟 ASR chunk，但必须保留清晰的时间范围。
3. 在总结基础上生成“内容大纲（含最早出现时间）”：每个一级主题都标注最早出现的时间节点。
4. 对并列含义的内容使用结构化文本输出，例如项目列表、分组小标题、表格。避免大段复述 ASR。
5. 只在必要位置引用少量原话；不要大规模逐句复述。
6. 关键名词必须校正：店铺、酒吧、餐厅、厂牌、城市地点优先查大众点评/美团/地图/品牌官网；文艺作品、人名、播客/书影音条目优先查豆瓣；商品/酒款可用品牌官网、电商页、Untappd、RateBeer 等交叉确认。所有外部校正来源在末尾列出链接。
7. 如果 ASR 与外部来源冲突，先说明“推断依据”，不要把不确定内容写成确定事实。

豆瓣元数据：

- 标题：{episode.get('title')}
- 播客：{episode.get('podcast_title')}
- 豆瓣 URL：{episode.get('douban_url')}
- 发布时间：{episode.get('published_at')}
- 时长：{episode.get('duration')}

豆瓣简介 HTML：

```html
{description_html}
```
"""


def run_all(args: argparse.Namespace) -> int:
    prepare_args = argparse.Namespace(
        url=args.url,
        workdir=args.workdir,
        basename=args.basename,
        window_seconds=args.window_seconds,
        force=args.force,
        timeout=args.timeout,
    )
    prepare_status = prepare_episode(prepare_args)
    if prepare_status:
        return prepare_status

    episode_id = parse_episode_id(args.url)
    metadata = fetch_json(DOUBAN_API.format(episode_id=episode_id), timeout=args.timeout)
    title = str(metadata.get("title") or f"douban_podcast_episode_{episode_id}")
    basename = args.basename or f"{episode_id}_{slugify(title, 'episode')}"
    workdir = Path(args.workdir).expanduser().resolve() if args.workdir else Path.cwd() / basename
    manifest_path = workdir / "manifest.json"

    transcribe_args = argparse.Namespace(
        manifest=str(manifest_path),
        output=None,
        api_key=args.api_key,
        start=None,
        end=None,
        workers=args.workers,
        timeout=args.timeout,
        retries=args.retries,
    )
    transcribe_status = transcribe_episode(transcribe_args)
    if transcribe_status:
        return transcribe_status

    build_args = argparse.Namespace(
        manifest=str(manifest_path),
        input=None,
        output=None,
        prompt_output=None,
    )
    return build_asr_markdown(build_args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Download audio, split it into chunks, and write manifest.json.")
    prepare.add_argument("--url", required=True, help="Douban podcast episode URL.")
    prepare.add_argument("--workdir", help="Output working directory. Defaults to ./<episode_id>_<title_slug>.")
    prepare.add_argument("--basename", help="Stable basename for audio and chunks.")
    prepare.add_argument("--window-seconds", type=int, default=DEFAULT_WINDOW_SECONDS)
    prepare.add_argument("--force", action="store_true", help="Re-download audio and recreate chunks.")
    prepare.add_argument("--timeout", type=int, default=60)
    prepare.set_defaults(func=prepare_episode)

    transcribe = subparsers.add_parser("transcribe", help="Transcribe chunks with OpenRouter Qwen3-ASR.")
    transcribe.add_argument("--manifest", required=True)
    transcribe.add_argument("--output", help="ASR JSONL output. Defaults to manifest path setting.")
    transcribe.add_argument("--api-key", help="OpenRouter API key. Prefer OPENROUTER_API_KEY env var.")
    transcribe.add_argument("--start", type=int, help="Optional first chunk index.")
    transcribe.add_argument("--end", type=int, help="Optional last chunk index, inclusive.")
    transcribe.add_argument("--workers", type=int, default=2)
    transcribe.add_argument("--timeout", type=int, default=180)
    transcribe.add_argument("--retries", type=int, default=3)
    transcribe.set_defaults(func=transcribe_episode)

    build = subparsers.add_parser("build-asr", help="Build timestamped ASR markdown and a summary prompt.")
    build.add_argument("--manifest", required=True)
    build.add_argument("--input", nargs="+", help="ASR JSONL input(s). Defaults to manifest path setting.")
    build.add_argument("--output", help="ASR markdown output. Defaults to manifest path setting.")
    build.add_argument("--prompt-output", help="Summary prompt output. Defaults to manifest path setting.")
    build.set_defaults(func=build_asr_markdown)

    ranges = subparsers.add_parser("ranges", help="Print balanced inclusive ASR chunk ranges for multi-agent work.")
    ranges.add_argument("--manifest", required=True)
    ranges.add_argument("--agents", type=int, default=4)
    ranges.add_argument("--json", action="store_true")
    ranges.set_defaults(func=print_ranges)

    validate = subparsers.add_parser("validate-asr", help="Validate ASR JSONL coverage before building asr.md.")
    validate.add_argument("--manifest", required=True)
    validate.add_argument("--input", nargs="+", help="ASR JSONL input(s). Defaults to all asr_results/*.jsonl.")
    validate.set_defaults(func=validate_asr)

    run = subparsers.add_parser("run", help="Run prepare, transcribe, and build-asr.")
    run.add_argument("--url", required=True, help="Douban podcast episode URL.")
    run.add_argument("--workdir", help="Output working directory. Defaults to ./<episode_id>_<title_slug>.")
    run.add_argument("--basename", help="Stable basename for audio and chunks.")
    run.add_argument("--window-seconds", type=int, default=DEFAULT_WINDOW_SECONDS)
    run.add_argument("--force", action="store_true")
    run.add_argument("--api-key", help="OpenRouter API key. Prefer OPENROUTER_API_KEY env var.")
    run.add_argument("--workers", type=int, default=2)
    run.add_argument("--timeout", type=int, default=180)
    run.add_argument("--retries", type=int, default=3)
    run.set_defaults(func=run_all)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
