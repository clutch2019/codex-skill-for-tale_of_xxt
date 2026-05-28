# Manifest Schema

`manifest.json` is the workflow contract between the coordinator and ASR workers.

Top-level fields:

- `schema`: currently `tale_of_xxt-manifest-v1`
- `source`: original Douban URL, parsed episode id, Douban API URL, and audio URL
- `episode`: title, podcast title, duration, publish time, Douban URL, and description HTML
- `settings`: chunk window seconds and ASR model
- `paths`: workdir-relative output paths
- `chunks`: ordered list of chunk records

Each chunk record contains:

- `index`: zero-based chunk number
- `path`: workdir-relative audio chunk path
- `start_seconds` / `end_seconds`: numeric time range
- `start` / `end`: display timestamps

ASR JSONL rows preserve the chunk fields and add:

- `ok`: boolean success flag
- `model`: ASR model used
- `text`: transcribed content when successful
- `error`: response or exception details when failed

Important invariants:

- `--start` and `--end` are inclusive.
- Valid chunk indices are `0..len(chunks)-1`.
- Multi-agent workers must write disjoint output files. The manifest does not lock files.
- Final coverage requires exactly one successful row for every chunk index.
