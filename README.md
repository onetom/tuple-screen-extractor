# tuple-screen-extractor

Extract every shared-screen frame from a [Tuple](https://tuple.app) call into
JPEGs — plus a timestamped index and, optionally, a watchable video.

Tuple's **Capture** feature (macOS 3.3.0+) records shared screens locally, but
it does not store them as video files and the app offers no playback. Frames
live in an encrypted, proprietary store inside Tuple's local index database,
and the only supported way to get a viewable image is one frame at a time
through the `tuple` CLI. This tool turns that single-frame API into a complete,
exact per-sharer frame sequence.

Everything stays local: this tool only reads files and talks to the Tuple
daemon already on your machine. It sends nothing anywhere.

## Requirements

- **macOS** — Capture (and its local archive) is a macOS-only Tuple feature.
- **Tuple app** running and signed in, with calls captured via Capture.
- **`tuple` CLI** installed (app sidebar → Local History → *Install Tuple CLI*).
- **Python 3.9+**, standard library only (developed against 3.14).
- **ffmpeg** (optional) — only for `--video`. `brew install ffmpeg`.

## Quick start

```sh
# see what would be extracted, before touching anything
./tuple_extract_screens.py <call-id> --dry-run

# extract a 15-second window around something someone said
./tuple_extract_screens.py <call-id> --since 01:04:15 --until 01:04:30 --video

# one frame per 5 seconds for the whole call (fast overview)
./tuple_extract_screens.py <call-id> --every 5

# absolutely every stored frame of every sharer (can be huge — see below)
./tuple_extract_screens.py <call-id>
```

`<call-id>` is the ID (or any unique prefix) from `tuple capture list`:

```sh
$ tuple capture list
 Date             Title Call                                  Segments Participants
 2026-09-17 13:55       a1b2c3d4-0000-4f07-a545-b3177728788b 2809     Alice A., Bob B.
```

## Output layout

```
<out>/
  frames/
    user-163400-alice-a/
      segment-01/
        000001_20260917T055950.911Z.jpg   # <seq>_<UTC timestamp>.jpg, full display resolution
        ...
    user-145137-bob-b/
      segment-02/
        ...
  manifest.jsonl        # one JSON record per frame (the authoritative index)
  frames.csv            # same data, flattened for spreadsheets
  summary.json          # counts, failures, per-segment stats, elapsed time
  user-163400-alice-a-segment-01.mp4   # only with --video
  user-145137-bob-b-segment-02.mp4
```

A "segment" is one continuous stretch of one participant sharing their screen.
A call where sharing changes hands produces one segment per sharer; each
segment's frames and video are kept separately, because a frame only exists
relative to a specific sharer.

One manifest row:

```json
{"annotations_overlay": "none", "app": "Zen", "bytes": 327736,
 "call_id": "a1b2c3d4-...", "cli_segment_id": "...", "drift_ms": 0,
 "file": "frames/user-163400-alice-a/segment-01/000001_20260917T055950.911Z.jpg",
 "frame_time": "2026-09-17T05:59:50.911Z", "height": 1440,
 "recording_uuid": "...", "requested_ms": 1789624790911,
 "requested_time": "2026-09-17T05:59:50.911Z", "segment": 1,
 "segment_uuid": "...", "url": null, "user": "Alice A.", "user_id": 163400,
 "width": 2560, "window_title": "…pull request title…"}
```

`app`, `window_title`, and `url` are the app/window that was visible on that
sharer's screen at that moment (reconstructed from Tuple's shared-content
events) — useful for finding "that frame where the deploy dashboard was up".

## How it works

### What Tuple actually stores

Capturing a call writes a local archive at
`~/Library/Application Support/app.tuple.app/index.db` (SQLite, WAL mode;
Tuple ships its own schema diagram next to it as `index-schema.mmd`).
Relevant tables:

| Table | Contents |
|---|---|
| `calls` | call ID, start/end, title |
| `recording_sessions` | one row per Capture on/off stretch within a call |
| `screen_share_segments` | one row per participant's sharing stretch: `user_id`, exact `started_at`/`ended_at` |
| `screen_share_frames` | every stored frame: `share_segment_id`, `ts_ms` (epoch ms), `key_frame_ts_ms`, and an **encrypted, proprietary `data` blob** |
| `shared_content` + `events` | which app/window title/URL was on a sharer's screen when |
| `annotation_events` | draw-on-screen strokes, per share segment |

Observed against Tuple 3.3.5: frames are stored at **~8 fps** (one every
~120–125 ms) at **full display resolution**, with periodic key frames and
delta frames in between. A 3.5-hour single-sharer call had 99,966 frames.

### Why the database (metadata only)

The frame blobs are encrypted and undecodable outside Tuple, so extracting
frames means asking the CLI to render each one:

```sh
tuple --format json screen --at 2026-09-17T05:59:50.911Z --call a1b2c3d4 --user 163400 -o frame.jpg
# → frame.jpg (JPEG, annotations composited in)
# → receipt on stdout: {"frame_time": "2026-09-17T05:59:50.911Z", "sharer_user_id": 163400,
#                       "width": 2560, "height": 1440, "annotations_overlay": "none", ...}
```

The only thing the database is needed for is **which timestamps have a frame
and which user shared it** (`--user` is required because frames exist
per-sharer; the person speaking at time T is often not the person whose screen
has the interesting frame). The tool opens the database `mode=ro` with
`PRAGMA query_only` and selects only `ts_ms`, timing, and user columns — the
`data` blob is never read or decoded. The extraction itself goes entirely
through the supported CLI. (`--source cli` gives a no-database fallback that
rebuilds sharing intervals from `tuple capture show` events and walks them at
a probe stride, de-duplicating on the receipt's `frame_time`; it can miss
frames stored faster than the stride.)

Because the inventory is exact, extraction is too: **one probe per stored
frame, zero guessing, zero duplicates**. Verified against the database on a
full segment: 1332 stored frames → 1332 extracted frames, 1332 distinct
`frame_time`s, 0 missing, 0 extra, 0 probe failures.

### Video stitching

`--video` concatenates each segment's frames with ffmpeg's concat demuxer,
giving every frame the **real duration** until the next stored frame (floored
at 40 ms, capped at 10 s) instead of a constant fps — the video plays at
true speed even where Tuple's storage cadence wobbles. Encoded with
`-fps_mode vfr` (falls back to `-vsync vfr` on older ffmpeg), H.264/yuv420p.

### Resume

Every frame written is appended to `manifest.jsonl`. Re-running a command
(after an interrupt, or with a wider time range) skips frames already in the
manifest, so extraction is cheap to retry and safe to Ctrl-C.

## Options

| Option | Default | Meaning |
|---|---|---|
| `call` | — | call ID or unique prefix (from `tuple capture list`) |
| `-o, --out` | `./<call8>-screens` | output directory |
| `--source {auto,db,cli}` | `auto` | `db`: exact frame inventory from the index database (metadata only). `cli`: stride walk via capture events, no database. `auto` falls back if the DB can't be opened. |
| `--db` | `~/Library/Application Support/app.tuple.app/index.db` | index database path |
| `--every N` | 0 (every stored frame) | keep at most one frame per N seconds |
| `--stride N` | 1 | keep every Nth stored frame |
| `--since` / `--until` | whole call | RFC3339 timestamp, or `HH:MM[:SS]` **relative to call start** |
| `--sharer UID` | all sharers | only this user ID (repeatable; IDs from the dry run) |
| `--limit-frames N` | 0 (no limit) | at most N frames per segment (quick sampling) |
| `--jobs N` | 4 | parallel probes; the Tuple daemon caps out around here |
| `--exact` | off | request the exact instant (don't settle an in-progress annotation forward) |
| `--video` | off | stitch each segment into an mp4 (needs ffmpeg) |
| `--video-fps` | 8 | fallback fps when frame spacing is unknown |
| `--no-resume` | off | re-extract frames already in the manifest |
| `--dry-run` | off | print the plan (per-segment counts, sizes, time estimate) and exit |
| `--quiet` | off | suppress per-chunk progress |
| `--tuple-bin` / `--host` / `--env` | `tuple` | CLI location / app socket / Tuple environment |

`--dry-run` example (redacted):

```
call    a1b2c3d4-0000-4f07-a545-b3177728788b
window  2026-09-17T05:55:35.792Z -> 2026-09-17T09:26:02.182Z
source  db (exact frame inventory)
plan    2 segment(s), 101298 frame(s) to extract; ~31.7 min at 4 probes in parallel
  seg 01  user 163400  Alice A.   05:56:15 -> 05:59:53  stored 1332   extract 1332    ~0.7 GB
  seg 02  user 145137  Bob B.     05:59:54 -> 09:26:01  stored 99966  extract 99966   ~50.0 GB
```

## Performance (measured on Tuple 3.3.5, M-series Mac)

| Segment type | Probes/s (--jobs 4) | Frame size | Notes |
|---|---|---|---|
| your own screen | ~14 (daemon cap; more jobs don't help) | ~0.6 MB | 2560×1440 |
| a remote participant's screen | ~1.2 | ~0.35 MB | 3840×2160; delta-chain decode is ~10× slower |

So: budget **~1 probe per stored frame**; a full 100k-frame call is ~55 GB
and anywhere from ~2 hours (own screen) to ~20 hours (remote sharer). In
practice `--every 1`–`--every 5` (one frame per second or per five seconds)
turns an unmanageable job into minutes and is usually all you need; go to full
density only for forensic windows via `--since/--until`.

## Privacy and safety

- Frames show **everything** that was on the shared screen — code, credentials
  left visible, private messages. Treat an extraction like the call itself.
  Do not commit extractions to a repository; add the output directory to
  `.gitignore`.
- Tuple already notifies every call participant when Capture starts; this tool
  changes nothing about that. Get consent before extracting and sharing frames.
- Database access is strictly read-only and never touches frame blobs; the tool
  cannot modify Tuple's archive. `--source cli` exists if you'd rather not
  touch the database at all.

## Troubleshooting

- **`nothing to extract: no shared-screen frames match the filters`** — the
  call wasn't captured, nobody shared a screen in the selected range, or
  `--since/--until` land outside the call. Check with `--dry-run` and
  `tuple capture list`.
- **A few `probe_failures: {"no-recording": N}` in `summary.json`** — expected
  at interval edges with `--source cli`: those probes landed outside stored
  ranges. With `--source db` it should be empty.
- **`ffmpeg failed: Unrecognized option 'vsync'`** — ffmpeg ≥ 8 renamed
  `-vsync`; the tool already tries `-fps_mode vfr` first and falls back, so if
  you see this, your ffmpeg is older than the fallback assumes; upgrade
  (`brew upgrade ffmpeg`) or drop `--video`.
- **Extraction is ~10× slower for someone else's screen** — that's the Tuple
  daemon decoding remote delta chains, not this tool; `--every` is your friend.

## Exit codes

`0` success (including "nothing left to do on resume"), `1` error or no
matching frames, `130` interrupted by Ctrl-C (resume from where it stopped).

## Credits

This project was generated by the Kimi K3 and Qwen3.8 Flash Next LLMs —
including the investigation of Tuple's storage format, the extractor itself,
and this README — under human direction and with all behavior verified against
a live Tuple 3.3.5 installation.
