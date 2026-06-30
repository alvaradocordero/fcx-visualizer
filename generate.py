#!/usr/bin/env python3
"""
generate.py
-----------
Reads mission_images.json and mission_positions.json, describes their contents,
and produces three kinds of artifacts:

  1. channels.json   - every channel observed, with category + usage stats
  2. attempts.json   - every attempt folder with start/stop, channels, image counts
  3. svg/<source>.svg - a 2D (x,y) trajectory map for each position source

Usage:
    python3 generate.py
"""

import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGES_PATH = os.path.join(HERE, "mission_images.json")
POSITIONS_PATH = os.path.join(HERE, "mission_positions.json")
SVG_DIR = os.path.join(HERE, "svg")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def now_iso():
    return datetime.now(timezone.utc).isoformat()


def channel_category(name: str) -> str:
    """Map a channel name to a high-level category."""
    if name.startswith("raw-image--") or name.startswith("image_"):
        return "image"
    if name.startswith("raw-robot--"):
        return "robot"
    if name.startswith("raw-spotcam--"):
        return "spotcam"
    if name.startswith("raw-sv600--"):
        return "sv600"
    return "other"


def channel_short(name: str) -> str:
    """Human-readable tail of a channel name (strip the data-source prefix)."""
    for sep in ("--", "_"):
        if sep in name:
            return name.rsplit(sep, 1)[-1]
    return name


def humanize_channel(name: str) -> str:
    """Pretty label for SVG/titles, e.g. raw-image--frontleft_fisheye_image -> frontleft_fisheye_image."""
    if name.startswith("raw-image--"):
        return name[len("raw-image--"):]
    if name.startswith("image_"):
        return name[len("image_"):]
    return name


def fmt_ts(ts):
    if ts:
        return ts
    return None


# --------------------------------------------------------------------------- #
# load
# --------------------------------------------------------------------------- #
def load():
    with open(IMAGES_PATH, "r") as f:
        images = json.load(f)
    with open(POSITIONS_PATH, "r") as f:
        positions = json.load(f)
    return images, positions


# --------------------------------------------------------------------------- #
# describe
# --------------------------------------------------------------------------- #
def describe(images, positions):
    print("=" * 72)
    print(" mission_images.json")
    print("=" * 72)
    n_attempts = len(images)
    n_channels_total = sum(len(a.get("channels", [])) for a in images)
    uniq_channels = set()
    total_images = 0
    for a in images:
        for c in a.get("channels", []):
            uniq_channels.add(c["channel_name"])
            total_images += len(c.get("images", []))
    print(f"  type           : list")
    print(f"  attempts       : {n_attempts}")
    print(f"  channel slots  : {n_channels_total} (attempt x channel)")
    print(f"  unique channels: {len(uniq_channels)}")
    print(f"  total images   : {total_images}")
    print(f"  per attempt    : mission_root, attempt_folder, action_name,")
    print(f"                    started, ended, group_name, channels[]")

    print()
    print("=" * 72)
    print(" mission_positions.json")
    print("=" * 72)
    print(f"  type           : dict")
    print(f"  generated_at   : {positions.get('generated_at')}")
    print(f"  root           : {positions.get('root')}")
    print(f"  pattern        : {positions.get('pattern')}")
    print(f"  source_count   : {positions.get('source_count')}")
    print(f"  record_count   : {positions.get('record_count')}")
    print(f"  sources[]      : path, rel_path, position_json, record_count,")
    print(f"                    first_timestamp, last_timestamp")
    print(f"  records[]      : timestamp, timestamp_ns, position{{x,y,z}},")
    print(f"                    xy_rotation_deg, xz_rotation_deg, source")
    print("=" * 72)
    print()


# --------------------------------------------------------------------------- #
# channels.json
# --------------------------------------------------------------------------- #
def build_channels(images):
    # name -> {attempts present, total images, has_images, extensions set, example path}
    agg = defaultdict(lambda: {
        "attempts_present": 0,
        "total_images": 0,
        "extensions": set(),
        "example_path": None,
    })
    for a in images:
        for c in a.get("channels", []):
            name = c["channel_name"]
            n = len(c.get("images", []))
            entry = agg[name]
            entry["attempts_present"] += 1
            entry["total_images"] += n
            for img in c.get("images", []):
                if img.get("extension"):
                    entry["extensions"].add(img["extension"])
            if entry["example_path"] is None and c.get("images"):
                entry["example_path"] = c["images"][0].get("path")
            if entry["example_path"] is None and c.get("channel_folder"):
                entry["example_path"] = c["channel_folder"]

    channels = []
    for name in sorted(agg):
        e = agg[name]
        channels.append({
            "name": name,
            "short": channel_short(name),
            "label": humanize_channel(name),
            "category": channel_category(name),
            "attempts_present": e["attempts_present"],
            "total_images": e["total_images"],
            "has_images": e["total_images"] > 0,
            "extensions": sorted(e["extensions"]),
            "example_path": e["example_path"],
        })

    out = {
        "generated_at": now_iso(),
        "source_file": "mission_images.json",
        "total_channels": len(channels),
        "categories": sorted({c["category"] for c in channels}),
        "channels": channels,
    }
    with open(os.path.join(HERE, "channels.json"), "w") as f:
        json.dump(out, f, indent=2)
    print(f"wrote channels.json  ({len(channels)} channels)")
    return out


# --------------------------------------------------------------------------- #
# attempts.json
# --------------------------------------------------------------------------- #
def build_attempts(images):
    attempts = []
    for a in images:
        ch_list = []
        total_images = 0
        for c in a.get("channels", []):
            n = len(c.get("images", []))
            total_images += n
            ch_list.append({
                "name": c["channel_name"],
                "label": humanize_channel(c["channel_name"]),
                "category": channel_category(c["channel_name"]),
                "image_count": n,
            })
        # keep channel order from the source file
        attempts.append({
            "attempt_folder": a.get("attempt_folder"),
            "group_name": a.get("group_name"),
            "action_name": a.get("action_name"),
            "started": fmt_ts(a.get("started")),
            "ended": fmt_ts(a.get("ended")),
            "channel_count": len(ch_list),
            "total_images": total_images,
            "channels": ch_list,
        })

    # sort chronologically by start (None last)
    def sort_key(x):
        return (x["started"] is None, x["started"] or "")

    attempts.sort(key=sort_key)

    out = {
        "generated_at": now_iso(),
        "source_file": "mission_images.json",
        "total_attempts": len(attempts),
        "attempts": attempts,
    }
    with open(os.path.join(HERE, "attempts.json"), "w") as f:
        json.dump(out, f, indent=2)
    print(f"wrote attempts.json  ({len(attempts)} attempts)")
    return out


# --------------------------------------------------------------------------- #
# SVG maps per source
# --------------------------------------------------------------------------- #
def _hex_color(t: float) -> str:
    """Interpolate a color along a blue -> cyan -> green -> yellow -> red ramp."""
    stops = [
        (0.00, (31, 119, 180)),   # blue
        (0.33, (0, 200, 180)),    # teal/green
        (0.66, (230, 180, 0)),    # yellow
        (1.00, (214, 39, 40)),    # red
    ]
    t = max(0.0, min(1.0, t))
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0
            r = round(c0[0] + (c1[0] - c0[0]) * f)
            g = round(c0[1] + (c1[1] - c0[1]) * f)
            b = round(c0[2] + (c1[2] - c0[2]) * f)
            return f"#{r:02x}{g:02x}{b:02x}"
    r, g, b = stops[-1][1]
    return f"#{r:02x}{g:02x}{b:02x}"


def _esc(s):
    """Escape a string for safe inclusion in SVG text/attribute content."""
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def make_svg(source_rel, recs, first_ts=None, last_ts=None):
    """Build an SVG string for one source's (x,y) trajectory."""
    pts = [(r["position"]["x"], r["position"]["y"]) for r in recs
           if isinstance(r.get("position"), dict)
           and r["position"].get("x") is not None
           and r["position"].get("y") is not None]
    if len(pts) < 2:
        return None, 0

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    n = len(pts)

    W, H = 1000, 1000
    pad = 60
    span_x = max(1e-6, maxx - minx)
    span_y = max(1e-6, maxy - miny)
    span = max(span_x, span_y)  # keep aspect square
    scale = (W - 2 * pad) / span

    def tx(x):
        return pad + (x - minx) * scale + (span - span_x) / 2 * scale

    def ty(y):
        # flip y so +y is up on the map
        return H - pad - (y - miny) * scale - (span - span_y) / 2 * scale

    # grid
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="sans-serif">',
        f'<title>{_esc(source_rel)}</title>',
        f'<desc>source: {_esc(source_rel)} | points: {n} | '
        f'first: {_esc(first_ts)} | last: {_esc(last_ts)}</desc>',
        '<rect width="100%" height="100%" fill="#0f1420"/>',
    ]
    # subtle grid
    for gx in range(0, W + 1, 100):
        parts.append(f'<line x1="{gx}" y1="0" x2="{gx}" y2="{H}" stroke="#1d2638" stroke-width="1"/>')
    for gy in range(0, H + 1, 100):
        parts.append(f'<line x1="0" y1="{gy}" x2="{W}" y2="{gy}" stroke="#1d2638" stroke-width="1"/>')

    # trajectory as colored segments (color encodes time progression)
    for i in range(1, n):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        col = _hex_color((i - 1) / max(1, n - 1))
        parts.append(
            f'<line x1="{tx(x0):.1f}" y1="{ty(y0):.1f}" '
            f'x2="{tx(x1):.1f}" y2="{ty(y1):.1f}" '
            f'stroke="{col}" stroke-width="2.2" stroke-linecap="round"/>'
        )

    # start (green) & end (white) markers
    sx, sy = tx(pts[0][0]), ty(pts[0][1])
    ex, ey = tx(pts[-1][0]), ty(pts[-1][1])
    parts.append(f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="7" fill="#2ca02c" stroke="#fff" stroke-width="1.5"/>')
    parts.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="7" fill="#ffffff" stroke="#000" stroke-width="1.5"/>')

    # labels
    label = source_rel.split("/")[0]  # mission group
    parts.append(
        f'<text x="20" y="32" fill="#e6edf3" font-size="20" font-weight="bold">{label}</text>'
    )
    parts.append(
        f'<text x="20" y="56" fill="#9aa7b5" font-size="13">{source_rel}</text>'
    )
    parts.append(
        f'<text x="20" y="{H - 28}" fill="#9aa7b5" font-size="12">'
        f'{n} points  |  X[{minx:.2f}, {maxx:.2f}]  Y[{miny:.2f}, {maxy:.2f}]'
        f'  |  start=green  end=white  time=blue&#8594;red</text>'
    )
    parts.append(
        f'<text x="{W - 20}" y="{H - 28}" fill="#9aa7b5" font-size="12" text-anchor="end">'
        f'x,y meters</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts), n


def build_svgs(positions):
    os.makedirs(SVG_DIR, exist_ok=True)
    # group records by source
    by_source = defaultdict(list)
    for r in positions["records"]:
        by_source[r["source"]].append(r)

    # also keep source-level metadata
    meta = {s["rel_path"]: s for s in positions.get("sources", [])}

    written = 0
    skipped = 0
    for src in sorted(by_source):
        recs = by_source[src]
        # sort by time just in case
        recs.sort(key=lambda r: (r.get("timestamp_ns") is None, r.get("timestamp_ns") or 0))
        m = meta.get(src, {})
        svg, n = make_svg(src, recs, m.get("first_timestamp"), m.get("last_timestamp"))
        if svg is None:
            skipped += 1
            continue
        # safe filename from source rel path
        safe = src.replace("__", "_").strip("/").replace("/", "__")
        if len(safe) > 120:
            safe = safe[:120]
        out_path = os.path.join(SVG_DIR, f"{safe}.svg")
        with open(out_path, "w") as f:
            f.write(svg)
        written += 1

    # also build an index listing all maps
    index = {
        "generated_at": now_iso(),
        "source_file": "mission_positions.json",
        "svg_directory": "svg",
        "total_sources": positions.get("source_count"),
        "maps_written": written,
        "maps_skipped": skipped,
        "maps": sorted(
            [
                {
                    "source": src,
                    "file": (src.replace("__", "_").strip("/").replace("/", "__"))[:120] + ".svg",
                    "points": len(by_source[src]),
                    "first_timestamp": meta.get(src, {}).get("first_timestamp"),
                    "last_timestamp": meta.get(src, {}).get("last_timestamp"),
                }
                for src in by_source
            ],
            key=lambda d: d["source"],
        ),
    }
    with open(os.path.join(SVG_DIR, "index.json"), "w") as f:
        json.dump(index, f, indent=2)

    print(f"wrote svg/           ({written} maps, {skipped} skipped) + svg/index.json")
    return index


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    images, positions = load()
    describe(images, positions)
    build_channels(images)
    build_attempts(images)
    build_svgs(positions)
    print("\nDone.")


if __name__ == "__main__":
    main()
