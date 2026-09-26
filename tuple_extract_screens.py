#!/usr/bin/env python3
"""Extract every shared-screen frame Tuple captured for a call.

Tuple's Capture stores shared screens in a private local store, not as video
files. Frames are encrypted, proprietary blobs; the only supported way to get a
viewable image is the CLI:

    tuple --format json screen --at <time> --call <id> --user <sharer> -o f.jpg

which renders the frame in force at `<time>` as a JPEG and prints a receipt
containing the frame's real `frame_time`.

This tool turns that one-frame API into a complete per-sharer image sequence:

  * Inventory (--source db, default): read the frame index SQLite database at
    ~/Library/Application Support/app.tuple.app/index.db *metadata only*
    (screen_share_segments gives each sharer's exact interval;
    screen_share_frames.ts_ms gives the exact timestamp of every stored frame).
    No frame payload is ever selected or read. Every stored frame is then
    requested by its exact timestamp, so extraction is complete with exactly one
    probe per frame — nothing guessed, nothing duplicated.
  * Fallback (--source cli): if the index database is unavailable, rebuild each
    sharer's intervals from `tuple capture show` events and walk them at a probe
    stride, de-duplicating on the receipt's frame_time. This can miss frames
    stored faster than the stride.
  * Output: frames/user-<id>-<name>/segment-<n>/<seq>_<time>.jpg, plus
    manifest.jsonl (one record per frame: timing, dimensions, annotation state,
    and the app/window/URL that was on screen), frames.csv, summary.json, and
    with --video a variable-framerate mp4 per segment timed from real
    frame timestamps.

Observed against Tuple 3.3.5 (macOS): frames are stored at ~8 fps (~120-125 ms
apart, key frames plus delta frames) at full display resolution, and a rendered
JPEG is ~0.3-0.7 MB. Probe throughput depends on whose screen it was: a segment
of your own screen renders at the daemon cap (~14 probes/s with --jobs 4), while
a remote participant's segment decoded ~10x slower (~1.2 probes/s). So the 101k
frames of a 3.5-hour call are ~55 GB: ~2 h for the own-screen segment, far longer
for a remote one. Bound the work with --every, --stride, --since/--until,
--sharer, --limit-frames.

Requires: the `tuple` CLI (app running, signed in); ffmpeg only for --video.

Examples
  ./tuple_extract_screens.py 990b01c1 --dry-run
  ./tuple_extract_screens.py 990b01c1                       # every frame, every sharer
  ./tuple_extract_screens.py 990b01c1 --every 5 --video     # 1 frame / 5 s + mp4
  ./tuple_extract_screens.py 990b01c1 --sharer 145137 --since 01:30 --until 02:00
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import quote

Z = dt.timezone.utc
DEFAULT_DB = os.path.expanduser("~/Library/Application Support/app.tuple.app/index.db")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Fail(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #

def ms_of(iso: str) -> int:
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int(d.timestamp() * 1000)


def rfc3339(ms: int) -> str:
    d = dt.datetime.fromtimestamp(ms / 1000, Z)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def compact(ms: int) -> str:
    d = dt.datetime.fromtimestamp(ms / 1000, Z)
    return d.strftime("%Y%m%dT%H%M%S") + f".{ms % 1000:03d}Z"


def parse_time(value: str):
    """RFC3339/ISO8601 (absolute) or HH:MM[:SS] (offset from call start)."""
    v = value.strip()
    try:
        d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        parts = v.split(":")
        if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
            raise argparse.ArgumentTypeError(f"unparseable timestamp: {value!r}")
        h, m = int(parts[0]), int(parts[1])
        s = int(parts[2]) if len(parts) > 2 else 0
        return ("rel", h * 3600 + m * 60 + s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.now().astimezone().tzinfo)
    return ("abs", int(d.astimezone(Z).timestamp() * 1000))


def resolve_bound(bound, call_start_ms: int) -> int | None:
    if bound is None:
        return None
    kind, val = bound
    return val if kind == "abs" else call_start_ms + val * 1000


def slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return s[:32] or "unknown"


# --------------------------------------------------------------------------- #
# tuple CLI
# --------------------------------------------------------------------------- #

class Tuple:
    def __init__(self, binary: str, host: str | None, env_name: str | None):
        self.base = [binary]
        if host:
            self.base += ["--host", host]
        if env_name:
            self.base += ["--env", env_name]

    def _json(self, args: list[str]) -> object:
        proc = subprocess.run(self.base + ["--format", "json"] + args,
                              capture_output=True, text=True)
        out = proc.stdout.strip()
        if proc.returncode != 0:
            try:
                err = json.loads(out or proc.stderr)
                msg = err.get("error", proc.stderr.strip())
            except json.JSONDecodeError:
                msg = (proc.stderr or proc.stdout).strip()
            raise Fail(f"tuple {' '.join(args)}: {msg}")
        return json.loads(out) if out else None

    def capture_list(self) -> list[dict]:
        return self._json(["capture", "list", "--limit", "-1"]) or []

    def capture_show(self, call_id: str) -> list[dict]:
        # `capture show --format json` emits JSON Lines: one record per line.
        proc = subprocess.run(self.base + ["--format", "json", "capture", "show", call_id],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise Fail(f"tuple capture show {call_id}: {(proc.stderr or proc.stdout).strip()}")
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def probe(self, call_id: str, user_id: int, at_ms: int, tmp: str, exact: bool):
        """Render one frame. Returns (receipt, error_kind); never raises, since
        probes land outside stored ranges routinely at interval edges."""
        cmd = self.base + ["--format", "json", "screen", "--at", rfc3339(at_ms),
                           "--call", call_id, "--user", str(user_id), "-o", tmp]
        if exact:
            cmd.append("--exact")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            try:
                return json.loads(proc.stdout), ""
            except json.JSONDecodeError:
                return None, "bad-receipt"
        try:
            kind = json.loads(proc.stdout or proc.stderr).get("kind", "")
        except json.JSONDecodeError:
            kind = ""
        return None, kind or "probe-failed"


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #

@dataclass
class Segment:
    index: int
    user_id: int
    user: str
    recording_uuid: str
    segment_uuid: str
    start_ms: int
    end_ms: int
    stored_frames: int
    targets: list[int]
    exact_inventory: bool
    content: tuple[list[int], list[dict]] | None = None


@dataclass
class Stats:
    probes: int = 0
    frames: int = 0
    skipped: int = 0
    bytes: int = 0
    drift: int = 0
    failures: dict = field(default_factory=dict)

    def merged(self, o: "Stats") -> None:
        self.probes += o.probes
        self.frames += o.frames
        self.skipped += o.skipped
        self.bytes += o.bytes
        self.drift += o.drift
        for k, v in o.failures.items():
            self.failures[k] = self.failures.get(k, 0) + v


def open_db(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise Fail(f"index database not found: {path}")
    try:
        db = sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise Fail(f"cannot open {path} read-only: {exc}") from None
    db.execute("PRAGMA query_only = ON")
    try:
        db.execute("select count(*) from screen_share_frames").fetchone()
    except sqlite3.Error as exc:
        raise Fail(f"{path} is not a Tuple index database ({exc})") from None
    return db


def plan_db(db: sqlite3.Connection, ref: str, since, until,
            sharers: set[int] | None, every_ms: int, stride: int,
            limit: int) -> tuple[dict, list[Segment]]:
    row = db.execute("select id, started_at, ended_at, title, room_name from calls "
                     "where id = ? or replace(id,'-','') like ? || '%'",
                     (ref, ref.replace("-", ""))).fetchall()
    if not row:
        raise Fail(f"no captured call matches {ref!r} in the index database")
    if len(row) > 1:
        raise Fail(f"call reference {ref!r} matches several calls: "
                   f"{', '.join(r[0][:8] for r in row)}")
    call_id, started_at, ended_at, title, room = row[0]
    call = {"call_id": call_id, "started_at": started_at, "ended_at": ended_at,
            "title": title or "", "room_name": room or ""}
    start_ms = ms_of(started_at)
    resolved_lo, resolved_hi = resolve_bound(since, start_ms), resolve_bound(until, start_ms)
    lo, hi = resolved_lo if resolved_lo is not None else 0, \
        resolved_hi if resolved_hi is not None else 2 ** 62

    rows = db.execute("""
        select s.id, s.uuid, s.user_id, coalesce(u.full_name, u.short_name, 'user ' || s.user_id),
               rs.uuid, rs.started_at, rs.ended_at, s.started_at, s.ended_at,
               (select count(*) from screen_share_frames f
                 where f.share_segment_id = s.id and f.ts_ms between ? and ?)
          from screen_share_segments s
          join recording_sessions rs on rs.id = s.recording_session_id
          left join users u on u.id = s.user_id
         where rs.call_id = ?
         order by s.started_at, s.user_id""", (lo, hi, call_id)).fetchall()
    if not rows:
        return call, [], {}

    # App / window / URL in force at each frame, per sharer.
    content: dict[int, tuple[list[int], list[dict]]] = {}
    for user_id, when, app_name, title_, url in db.execute("""
            select e.user_id, e.time, sc.app_name, sc.title, sc.url
              from events e join shared_content sc on sc.id = e.shared_content_id
              join recording_sessions rs on rs.id = e.recording_session_id
             where rs.call_id = ? and e.shared_content_id is not null
             order by e.user_id, e.time""", (call_id,)).fetchall():
        stamps, entries = content.setdefault(user_id, ([], []))
        stamps.append(ms_of(when))
        entries.append({"app": app_name, "window_title": title_, "url": url})

    segments: list[Segment] = []
    for (sid, seg_uuid, user_id, user, rec_uuid, _rs_s, _rs_e, seg_s, seg_e,
         in_range) in rows:
        if sharers and user_id not in sharers:
            continue
        stamps = [r[0] for r in db.execute(
            "select ts_ms from screen_share_frames where share_segment_id = ? "
            "and ts_ms between ? and ? order by ts_ms", (sid, lo, hi)).fetchall()]
        if every_ms > 0:
            picked, next_keep = [], stamps[0] if stamps else 0
            for ts in stamps:
                if ts >= next_keep:
                    picked.append(ts)
                    next_keep = ts + every_ms
            stamps = picked
        if stride > 1:
            stamps = stamps[::stride]
        if limit:
            stamps = stamps[:limit]
        segments.append(Segment(len(segments) + 1, user_id, user, rec_uuid, seg_uuid,
                                ms_of(seg_s), ms_of(seg_e), in_range, list(stamps), True,
                                content.get(user_id)))
    return call, segments


def plan_cli(cli: Tuple, ref: str, since, until,
             sharers: set[int] | None, every_ms: int, stride: int, limit: int,
             pad: float) -> tuple[dict, list[Segment]]:
    """Fallback without the index database: rebuild sharer intervals from capture
    events, then walk them at a stride and de-duplicate on receipt frame_time."""
    calls = cli.capture_list()
    exact = [c for c in calls if c.get("call_id") == ref]
    hits = exact or [c for c in calls
                     if (c.get("call_id") or "").replace("-", "").startswith(ref.replace("-", ""))]
    if not hits:
        raise Fail(f"no captured call matches {ref!r}; try `tuple capture list`")
    if len(hits) > 1:
        raise Fail(f"call reference {ref!r} is ambiguous: {', '.join(h['call_id'][:8] for h in hits)}")
    call = hits[0]
    call_id = call["call_id"]
    names = {p["id"]: p.get("full_name", f"user {p['id']}") for p in call.get("participants", [])}
    start_ms = ms_of(call["started_at"])
    end_ms = ms_of(call["ended_at"]) if call.get("ended_at") else int(time.time() * 1000)
    resolved_lo, resolved_hi = resolve_bound(since, start_ms), resolve_bound(until, start_ms)
    lo = max(resolved_lo, start_ms) if resolved_lo is not None else start_ms
    hi = min(resolved_hi, end_ms) if resolved_hi is not None else end_ms

    open_share: dict[int, int] = {}
    spans: dict[int, list[list[int]]] = {}
    for r in cli.capture_show(call_id):
        typ, data = r.get("type"), r.get("data") or {}
        when = ms_of(r["time"])
        if typ in ("user_screen_sharing_started", "user_screen_sharing_stopped"):
            uid = (data.get("user") or {}).get("id") or data.get("user_id")
            if not uid:
                continue
            names.setdefault(uid, (data.get("user") or {}).get("full_name") or f"user {uid}")
            if typ.endswith("started"):
                open_share.setdefault(uid, when)
            elif uid in open_share:
                spans.setdefault(uid, []).append([open_share.pop(uid), when])
        elif typ == "shared_content_changed":
            uid = data.get("user_id")
            if uid:
                spans.setdefault(uid, []).append([when, when])
    for uid, when in open_share.items():
        spans.setdefault(uid, []).append([when, end_ms])

    segments: list[Segment] = []
    for uid, ranges in sorted(spans.items()):
        if sharers and uid not in sharers:
            continue
        ranges.sort()
        merged: list[list[int]] = []
        for a, b in ranges:
            if merged and a - merged[-1][1] <= max(pad * 1000, 2000):
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for a, b in merged:
            a = max(a - int(pad * 1000), lo)
            b = min(b + int(pad * 1000), hi)
            if b <= a:
                continue
            stride_ms = max(every_ms, 125) * max(stride, 1)
            targets = list(range(a, b + 1, stride_ms))
            if limit:
                targets = targets[:limit]
            segments.append(Segment(len(segments) + 1, uid, names.get(uid, f"user {uid}"),
                                    "", "", a, b, len(targets), targets, False, None))
    segments.sort(key=lambda s: s.start_ms)
    for i, s in enumerate(segments, 1):
        s.index = i
    return call, segments


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #

def load_done(path: str) -> set[tuple[str, int]]:
    done = set()
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    done.add((rec.get("segment_uuid") or rec["user_id"], rec["requested_ms"]))
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
    return done


def content_at(seg: Segment, ms: int) -> dict:
    if not seg.content:
        return {}
    stamps, entries = seg.content
    i = bisect.bisect_right(stamps, ms) - 1
    return entries[i] if i >= 0 else {}


def extract(cli: Tuple, call_id: str, seg: Segment, out_dir: str, tmp_dir: str,
            manifest, jobs: int, exact: bool, quiet: bool) -> tuple[Stats, list[tuple[str, int]]]:
    stats = Stats()
    kept: list[tuple[str, int]] = []
    user_dir = os.path.join(out_dir, "frames", f"user-{seg.user_id}-{slug(seg.user)}",
                            f"segment-{seg.index:02d}")
    os.makedirs(user_dir, exist_ok=True)
    if not seg.targets:
        return stats, kept

    chunk = max(jobs * 8, 32)
    seq = len(kept)
    t0 = time.time()
    seen_frames: set[str] = set()
    for lo in range(0, len(seg.targets), chunk):
        batch = seg.targets[lo:lo + chunk]
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(
                lambda i_ms: (i_ms[0], *cli.probe(call_id, seg.user_id, i_ms[1],
                                                   os.path.join(tmp_dir, f"p{i_ms[0]}.jpg"), exact)),
                list(enumerate(batch))))
        for idx, receipt, kind in results:
            tmp = os.path.join(tmp_dir, f"p{idx}.jpg")
            ms = batch[idx]
            stats.probes += 1
            if receipt is None:
                stats.failures[kind] = stats.failures.get(kind, 0) + 1
                if os.path.exists(tmp):
                    os.remove(tmp)
                continue
            ft = receipt.get("frame_time")
            ft_ms = ms_of(ft) if ft else ms
            if not seg.exact_inventory:
                # Stride walk: several probes can resolve to the same stored frame.
                if ft in seen_frames:
                    stats.skipped += 1
                    os.remove(tmp)
                    continue
                seen_frames.add(ft)
            seq += 1
            final = os.path.join(user_dir, f"{seq:06d}_{compact(ft_ms)}.jpg")
            shutil.move(tmp, final)
            kept.append((final, ft_ms))
            stats.frames += 1
            stats.bytes += int(receipt.get("bytes") or 0)
            if ft_ms != ms:
                stats.drift += 1
            rec = {"call_id": call_id, "segment": seg.index, "segment_uuid": seg.segment_uuid,
                   "recording_uuid": seg.recording_uuid, "user_id": seg.user_id, "user": seg.user,
                   "requested_ms": ms, "requested_time": rfc3339(ms),
                   "frame_time": ft, "drift_ms": ft_ms - ms,
                   "file": os.path.relpath(final, out_dir),
                   "bytes": int(receipt.get("bytes") or 0),
                   "width": receipt.get("width"), "height": receipt.get("height"),
                   "cli_segment_id": receipt.get("segment_id"),
                   "annotations_overlay": receipt.get("annotations_overlay")}
            rec.update(content_at(seg, ms))
            manifest.write(json.dumps(rec, sort_keys=True) + "\n")
        manifest.flush()
        if not quiet:
            rate = stats.probes / max(time.time() - t0, 1e-6)
            eta = (len(seg.targets) - stats.probes) / max(rate, 1e-6)
            print(f"  segment {seg.index:02d} user {seg.user_id}: {stats.frames}/{len(seg.targets)} frames, "
                  f"{stats.bytes / 1e6:.0f} MB, {rate:.1f} probes/s, eta {eta / 60:.1f} min",
                  file=sys.stderr, end="\r", flush=True)
    if not quiet:
        print("", file=sys.stderr, flush=True)
    return stats, kept


def stitch(frames: list[tuple[str, int]], out_path: str, fps: float) -> str | None:
    if len(frames) < 2 or shutil.which("ffmpeg") is None:
        return None
    lst = out_path + ".txt"
    with open(lst, "w") as fh:
        for i, (path, ms) in enumerate(frames):
            dur = 1.0 / fps
            if i + 1 < len(frames):
                dur = min(max((frames[i + 1][1] - ms) / 1000.0, 0.04), 10.0)
            fh.write(f"file '{os.path.abspath(path)}'\nduration {dur:.3f}\n")
        fh.write(f"file '{os.path.abspath(frames[-1][0])}'\n")
    # ffmpeg >= 8 renamed -vsync to -fps_mode; fall back for older builds.
    for mode in (["-fps_mode", "vfr"], ["-vsync", "vfr"]):
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                               "-safe", "0", "-i", lst, *mode, "-pix_fmt", "yuv420p", out_path],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            os.remove(lst)
            return out_path
    os.remove(lst)
    log(f"  ffmpeg failed: {proc.stderr.strip()[:300]}")
    return None


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("call", help="captured call ID or unique prefix")
    ap.add_argument("-o", "--out", help="output directory (default ./<call8>-screens)")
    ap.add_argument("--source", choices=["auto", "db", "cli"], default="auto",
                    help="frame inventory source: db = exact timestamps from the index "
                         "database (default, metadata only), cli = stride walk via the CLI")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"index database (default {DEFAULT_DB})")
    ap.add_argument("--every", type=float, default=0.0,
                    help="keep at most one frame per N seconds (0 = every stored frame)")
    ap.add_argument("--stride", type=int, default=1, help="keep every Nth stored frame")
    ap.add_argument("--jobs", type=int, default=4, help="parallel probes (daemon caps near 4)")
    ap.add_argument("--sharer", type=int, action="append", dest="sharers",
                    help="only this sharer user ID (repeatable)")
    ap.add_argument("--since", type=parse_time, help="range start: RFC3339 or HH:MM[:SS] from call start")
    ap.add_argument("--until", type=parse_time, help="range end: RFC3339 or HH:MM[:SS] from call start")
    ap.add_argument("--limit-frames", type=int, default=0, help="max frames per segment (sampling)")
    ap.add_argument("--exact", action="store_true", help="pass --exact: do not settle annotations forward")
    ap.add_argument("--pad", type=float, default=2.0, help="seconds to widen intervals by (cli source)")
    ap.add_argument("--no-resume", action="store_true", help="re-extract frames already in the manifest")
    ap.add_argument("--video", action="store_true", help="stitch each segment into an mp4 (needs ffmpeg)")
    ap.add_argument("--video-fps", type=float, default=8.0, help="fallback fps when spacing is unknown")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--tuple-bin", default=os.environ.get("TUPLE_BIN", "tuple"))
    ap.add_argument("--host", default=os.environ.get("TUPLE_HOST"), help="Tuple app socket")
    ap.add_argument("--env", default=os.environ.get("TUPLE_ENV"), help="Tuple environment")
    args = ap.parse_args()

    cli = Tuple(args.tuple_bin, args.host, args.env)
    out_dir = args.out or None
    done: set[tuple[str, int]] = set()
    manifest_path = os.path.join(out_dir, "manifest.jsonl") if out_dir else None

    db = None
    if args.source in ("auto", "db"):
        try:
            db = open_db(args.db)
        except Fail as exc:
            if args.source == "db":
                raise
            log(f"index database unavailable ({exc}); falling back to CLI stride walk")
    if db is not None:
        call, segments = plan_db(db, args.call, args.since, args.until,
                                 set(args.sharers or []) or None, int(args.every * 1000),
                                 args.stride, args.limit_frames)
        source = "db"
    else:
        call, segments = plan_cli(cli, args.call, args.since, args.until,
                                  set(args.sharers or []) or None, int(args.every * 1000),
                                  args.stride, args.limit_frames, args.pad)
        source = "cli"
    if db is not None:
        db.close()

    call_id = call["call_id"]
    if not segments:
        log("nothing to extract: no shared-screen frames match the filters")
        return 1
    out_dir = out_dir or f"{call_id[:8]}-screens"
    manifest_path = os.path.join(out_dir, "manifest.jsonl")
    if not args.no_resume:
        done = load_done(manifest_path)
        if source == "db" and done:
            for s in segments:
                s.targets = [ts for ts in s.targets if (s.segment_uuid, ts) not in done]
            segments = [s for s in segments if s.targets]
    if not segments:
        log("nothing to extract: every matching frame is already in the manifest")
        return 0

    est = sum(len(s.targets) for s in segments)
    if args.dry_run:
        log(f"call    {call_id}  {call.get('title') or ''} {call.get('room_name') or ''}".rstrip())
        log(f"window  {call.get('started_at')} -> {call.get('ended_at')}")
        log(f"source  {source} (exact frame inventory)" if source == "db" else
            f"source  {source} (stride walk, may miss frames)")
        log(f"plan    {len(segments)} segment(s), {est} frame(s) to extract"
            f"{f', {len(done)} already extracted' if done else ''}"
            f"; ~{est / max(args.jobs, 1) * 0.075 / 60:.1f} min at {args.jobs} probes in parallel")
        for s in segments:
            log(f"  seg {s.index:02d}  user {s.user_id:<7} {s.user:<22} {rfc3339(s.start_ms)} -> "
                f"{rfc3339(s.end_ms)}  stored {s.stored_frames:<6} extract {len(s.targets):<6}"
                f"{f'  ~{len(s.targets) * 0.5 / 1000:.1f} GB' if source == 'db' else ''}")
        return 0

    tmp_dir = os.path.join(out_dir, ".tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    log(f"call {call_id}: {len(segments)} segment(s), {est} frame(s) via {source} inventory; "
        f"{'resuming ' + str(len(done)) + ' existing; ' if done else ''}out {out_dir}/")

    total = Stats()
    videos: list[str] = []
    extracted_by_seg: dict[int, int] = {}
    t_start = time.time()
    with open(manifest_path, "a") as manifest:
        for s in segments:
            if not args.quiet:
                log(f"segment {s.index:02d}: user {s.user_id} {s.user} "
                    f"{rfc3339(s.start_ms)} -> {rfc3339(s.end_ms)} ({len(s.targets)} frames)")
            stats, kept = extract(cli, call_id, s, out_dir, tmp_dir, manifest,
                                  args.jobs, args.exact, args.quiet)
            total.merged(stats)
            extracted_by_seg[s.index] = len(kept)
            if args.video:
                made = stitch(kept, os.path.join(
                    out_dir, f"user-{s.user_id}-{slug(s.user)}-segment-{s.index:02d}.mp4"),
                    args.video_fps)
                if made:
                    videos.append(made)
                    log(f"video {made} ({len(kept)} frames)")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    csv_path = os.path.join(out_dir, "frames.csv")
    cols = ["segment", "user_id", "user", "frame_time", "requested_time", "drift_ms",
            "file", "bytes", "width", "height", "annotations_overlay", "app", "window_title", "url"]
    with open(manifest_path) as src, open(csv_path, "w", newline="") as out:
        wr = csv.writer(out)
        wr.writerow(cols)
        for line in src:
            rec = json.loads(line)
            wr.writerow([rec.get(c) for c in cols])

    summary = {"call_id": call_id, "title": call.get("title") or "", "source": source,
               "out_dir": out_dir, "every_seconds": args.every, "stride": args.stride,
               "segments": [{"segment": s.index, "user_id": s.user_id, "user": s.user,
                             "segment_uuid": s.segment_uuid, "start": rfc3339(s.start_ms),
                             "end": rfc3339(s.end_ms), "stored_frames": s.stored_frames,
                             "extracted": extracted_by_seg.get(s.index, 0)} for s in segments],
               "frames_written": total.frames, "probes": total.probes,
               "duplicate_frames_skipped": total.skipped,
               "frames_with_frame_time_drift": total.drift,
               "probe_failures": total.failures, "bytes": total.bytes, "videos": videos,
               "elapsed_seconds": round(time.time() - t_start, 1)}
    with open(os.path.join(out_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)

    log(f"wrote {total.frames} frames ({total.bytes / 1e6:.0f} MB) from {total.probes} probes "
        f"in {summary['elapsed_seconds']}s; manifest {manifest_path}, index {csv_path}")
    if total.failures:
        log(f"note: {total.failures} probes returned no frame (timestamps outside stored ranges)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Fail as exc:
        log(f"error: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
