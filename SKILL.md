---
name: tale_of_xxt
description: Download a Douban podcast episode audio, split it into 2-minute chunks, transcribe chunks with OpenRouter Qwen3-ASR, and produce a Chinese coarse-grained timeline summary plus structured outline with corrected proper nouns. Use when the user provides a Douban podcast episode URL and asks for ASR, a timeline summary, a 内容大纲, or a reusable workflow for podcast summarization. Requires an OpenRouter API key before ASR.
---

# tale_of_xxt

Use this skill to turn one Douban podcast episode URL into:

- `manifest.json`: source metadata, audio path, chunk list, time windows, output paths
- `asr_results/*.jsonl`: per-chunk ASR rows
- `asr.md`: `{时间窗 + ASR 内容}` markdown
- `summary_prompt.md`: prompt scaffold for final summarization
- `coarse_summary_outline.md`: final 粗时间粒度总结 + 结构化大纲

## First Check

Before running any workflow, check whether the user provided an OpenRouter API key or whether `OPENROUTER_API_KEY` is set.

- If no key is available, stop and tell the user: `需要 OpenRouter API key 才能调用 Qwen3-ASR。请设置 OPENROUTER_API_KEY 或提供 key 后再继续。`
- Do not write API keys into scripts, manifests, markdown outputs, or shell history.
- Prefer `OPENROUTER_API_KEY` over passing `--api-key` in commands, especially when using subagents.

## Tool Script

The bundled script implements the mechanical workflow:

```bash
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py --help
```

Subcommands:

- `prepare`: parse Douban episode id, fetch Douban metadata, download `audio_href`, split audio into 2-minute mp3 chunks, and write `manifest.json`.
- `transcribe`: read `manifest.json`, transcribe a chunk range with OpenRouter `qwen/qwen3-asr-flash-2026-02-10`, and append JSONL rows.
- `build-asr`: merge one or more ASR JSONL files into timestamped `asr.md`, and write `summary_prompt.md`.
- `ranges`: print balanced inclusive chunk ranges for multi-agent ASR.
- `validate-asr`: verify every chunk has one successful ASR row before building `asr.md`.
- `run`: sequentially run prepare, transcribe, and build-asr without multi-agent partitioning.

Dependencies: `python3`, `ffmpeg`, `ffprobe`, network access to Douban audio and OpenRouter.

For manifest details, see [references/manifest-schema.md](references/manifest-schema.md).

## Recommended Workflow

1. Create a working directory and manifest:

```bash
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py prepare \
  --url "<DOUBAN_EPISODE_URL>" \
  --workdir "./tale_of_xxt_work"
```

Use a fresh workdir per episode or unique ASR output filenames per run. `prepare --force` recreates audio chunks but does not delete old ASR JSONL or markdown outputs.

2. Inspect `manifest.json` for `episode`, `chunks`, and `paths`.
   This manifest is the handoff contract for all later stages.

3. Transcribe chunks.
   For short episodes, local execution is fine:

```bash
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py transcribe \
  --manifest "./tale_of_xxt_work/manifest.json" \
  --workers 2
```

4. For long episodes, use multi-agent ASR partitioning.
   Partition contiguous chunk ranges, give each spawned worker a disjoint `--start`, `--end`, and `--output`, then merge the part files.

5. Build ASR markdown:

```bash
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py build-asr \
  --manifest "./tale_of_xxt_work/manifest.json" \
  --input ./tale_of_xxt_work/asr_results/part_*.jsonl
```

6. Use `asr.md`, `summary_prompt.md`, and external lookups to write `coarse_summary_outline.md`.

## Multi-Agent ASR

Use `multi_agent_v1.spawn_agent` when the episode has many chunks or the user explicitly asks for multi-agent/parallel extraction.

Main-agent responsibilities:

- Run `prepare` locally first so all workers share the same `manifest.json`.
- Do not use `run` for multi-agent workflows; it performs full ASR in one process.
- Read the number of chunks from `manifest.json`.
- Partition into contiguous inclusive ranges, usually 20-30 chunks per worker. Example for 94 chunks: `000-023`, `024-047`, `048-071`, `072-093`.
- You may compute ranges with:

```bash
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py ranges \
  --manifest "<ABS_WORKDIR>/manifest.json" \
  --agents 4
```

- If computing manually for `N` chunks and `K` agents, clamp `K <= N`. For agent `i`: `start = i * floor(N/K) + min(i, N % K)`, `size = floor(N/K) + (1 if i < N % K else 0)`, `end = start + size - 1`. Skip empty ranges.
- Spawn one worker per range with `multi_agent_v1.spawn_agent`. Each worker must write to a unique file such as `asr_results/part_000_023.jsonl`.
- Do not let multiple workers append to the same JSONL file.
- While workers run, extract candidate proper nouns from the Douban description, title, shownotes, and obvious repeated ASR terms if available.
- Wait for workers, then run `validate-asr --input part_*.jsonl`.
- If validation passes, run `build-asr --input part_*.jsonl`.
- If `asr.md` has missing or failed chunks, retry only the failed ranges locally or with one extra worker.

Spawn prompt template:

```text
Use the tale_of_xxt workflow. In workdir: <ABS_WORKDIR>.
Transcribe only chunks <START>-<END> from <ABS_WORKDIR>/manifest.json.
Run:
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py transcribe \
  --manifest "<ABS_WORKDIR>/manifest.json" \
  --start <START> \
  --end <END> \
  --output "<ABS_WORKDIR>/asr_results/part_<START>_<END>.jsonl" \
  --workers 2
Do not edit any other files. Do not overwrite other ASR part files. Report failures and the output file path.
```

Use `agent_type: "worker"` for execution subtasks. Do not include API keys in the spawn prompt; rely on `OPENROUTER_API_KEY` being available in the environment.

Coordinator merge/build commands after workers finish:

```bash
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py validate-asr \
  --manifest "<ABS_WORKDIR>/manifest.json" \
  --input "<ABS_WORKDIR>"/asr_results/part_*.jsonl

python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py build-asr \
  --manifest "<ABS_WORKDIR>/manifest.json" \
  --input "<ABS_WORKDIR>"/asr_results/part_*.jsonl
```

Expected files after ASR:

- `manifest.json`
- `audio/...`
- `chunks/*_chunk_NNN.mp3`
- `asr_results/part_START_END.jsonl` for each spawned worker
- `asr.md`
- `summary_prompt.md`
- `coarse_summary_outline.md`

## Final Summary Rules

Write the final output in Chinese unless the user asks otherwise.

Required sections:

1. `# [episode title] 粗时间粒度总结与内容大纲`
2. `## 专名校正说明`
3. `## 粗时间粒度内容总结`
4. `## 内容大纲（含最早出现时间）`
5. `## 外部校正参考`

Content rules:

- The timeline summary should use coarse windows, not every 2-minute chunk. Merge adjacent chunks when they form one topic.
- The outline must mark the earliest observed timestamp for each major topic.
- Use structured text for parallel ideas: tables, flat bullet lists, grouped bullets, or short subsections.
- Summarize and synthesize. Quote original wording only when it is necessary for a joke, phrase, or interpretive point.
- Do not paste large ASR passages into the final answer.
- Preserve uncertainty: if ASR and external sources conflict, state the inference basis.

Proper noun rules:

- First use Douban title, shownotes, episode description, and official single-episode metadata.
- For restaurants, bars, shops, venues, breweries, and local place names, verify with Meituan, Dianping, maps, brand sites, or official accounts when possible.
- For books, films, TV, music, podcasts, authors, actors, directors, and other cultural works or people, verify with Douban first when possible.
- For beer names and breweries, use brand sites, ecommerce product pages, Untappd/RateBeer, or official social pages as cross-checks.
- Put source links in `## 外部校正参考`.

## Output Standard

The final `coarse_summary_outline.md` should read like an edited episode guide, not a transcript. It should help a reader quickly know:

- what happened across the episode
- which topics appeared when
- what the main judgments and transitions were
- which ASR terms were corrected and why
- where external name checks came from
