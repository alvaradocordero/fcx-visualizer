#!/usr/bin/env python3
"""
generate.py
-----------
Read mission_images.json and mission_positions.json, describe their contents,
and emit three kinds of artifacts:

  1. channels.json   - every channel observed, with category + usage stats.
  2. attempts.json   - every attempt folder with start/stop, channels,
                       and image counts.
  3. svg/<source>.svg - a 2D (x, y) trajectory map for each position source.

Standard parameters live in parameters.json (see --params).  A structured
execution log is written to script.json on every run.

Usage:
    python3 generate.py                       # run with default parameters.json
    python3 generate.py --verbose             # show progress bars + extra output
    python3 generate.py --cprofile            # profile the run with cProfile
    python3 generate.py --params other.json   # use a different parameter file
    python3 generate.py --help                # full help menu
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import logging
import os
import pstats
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

# Module-level constants (UPPER_SNAKE per project convention).
HERE: str = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PARAMS_FILE: str = "parameters.json"
DEFAULT_LOG_FILE: str = "script.json"
SVG_MARGIN: int = 60
SVG_SIZE: int = 1000
MAX_LINE_FILENAME: int = 120


# --------------------------------------------------------------------------- #
# progress + logging helpers
# --------------------------------------------------------------------------- #
def _progress(
    iterable: Iterable[Any],
    total: int,
    desc: str,
    verbose: bool,
    stream: Any = sys.stderr,
) -> Iterator[Any]:
    """Yield items while rendering a small stderr progress bar when verbose.

    Args:
        iterable: The sequence to iterate over.
        total: Total number of items (drives the percentage).
        desc: Label shown before the counter.
        verbose: When False, this is a silent passthrough.
        stream: Writable stream for the bar (default stderr).

    Yields:
        Each item from ``iterable`` in order.
    """
    for index, item in enumerate(iterable, 1):
        yield item
        if verbose:
            percent = index * 100 // max(1, total)
            stream.write(f"\r{desc}: {index}/{total} ({percent}%)")
            stream.flush()
    if verbose:
        stream.write("\n")


def setup_logging(verbose: bool) -> logging.Logger:
    """Configure and return a module logger.

    Args:
        verbose: When True the level is DEBUG, otherwise WARNING.

    Returns:
        A configured ``logging.Logger`` instance.
    """
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    return logging.getLogger("generate")


# --------------------------------------------------------------------------- #
# small pure helpers (unit-tested)
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def channel_category(name: str) -> str:
    """Map a raw channel name to a high-level category.

    Args:
        name: The channel name, e.g. ``raw-image--frontleft_fisheye_image``.

    Returns:
        One of ``image``, ``robot``, ``spotcam``, ``sv600``, ``other``.
    """
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
    """Return the human-readable tail of a channel name.

    Args:
        name: The channel name.

    Returns:
        The substring after the last ``--`` or ``_`` separator.
    """
    for sep in ("--", "_"):
        if sep in name:
            return name.rsplit(sep, 1)[-1]
    return name


def humanize_channel(name: str) -> str:
    """Return a pretty label by stripping the data-source prefix.

    Args:
        name: The channel name.

    Returns:
        ``raw-image--foo`` -> ``foo``, ``image_foo`` -> ``foo``;
        otherwise the name unchanged.
    """
    if name.startswith("raw-image--"):
        return name[len("raw-image--"):]
    if name.startswith("image_"):
        return name[len("image_"):]
    return name


def fmt_ts(ts: str | None) -> str | None:
    """Normalize a timestamp field for JSON output.

    Args:
        ts: A timestamp string or ``None``.

    Returns:
        The timestamp unchanged, or ``None``.
    """
    return ts


def safe_filename(source: str) -> str:
    """Convert a source rel-path into a filesystem-safe SVG filename stem.

    Args:
        source: A ``group/attempt/channel`` relative path.

    Returns:
        A flattened, length-clamped string usable as a filename.
    """
    safe = source.replace("__", "_").strip("/").replace("/", "__")
    return safe[:MAX_LINE_FILENAME]


def _hex_color(t: float) -> str:
    """Interpolate a color along a blue -> teal -> yellow -> red ramp.

    Args:
        t: Progress fraction in [0, 1]; values are clamped to that range.

    Returns:
        A ``#rrggbb`` color string.
    """
    stops: list[tuple[float, tuple[int, int, int]]] = [
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
            frac = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            red = round(c0[0] + (c1[0] - c0[0]) * frac)
            green = round(c0[1] + (c1[1] - c0[1]) * frac)
            blue = round(c0[2] + (c1[2] - c0[2]) * frac)
            return f"#{red:02x}{green:02x}{blue:02x}"
    red, green, blue = stops[-1][1]
    return f"#{red:02x}{green:02x}{blue:02x}"


def _esc(value: Any) -> str:
    """Escape a value for safe inclusion in SVG text/attribute content.

    Args:
        value: Any value; ``None`` becomes the empty string.

    Returns:
        An XML-escaped string.
    """
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def sort_key(attempt: dict[str, Any]) -> tuple[bool, str]:
    """Order attempts chronologically by start time, pushing nulls last.

    Args:
        attempt: An attempt dict containing an optional ``started`` field.

    Returns:
        A tuple sortable key: ``(started is None, started or "")``.
    """
    started = attempt.get("started")
    return (started is None, started or "")


def record_sort_key(record: dict[str, Any]) -> tuple[bool, int]:
    """Order position records by their nanosecond timestamp.

    Args:
        record: A position record with an optional ``timestamp_ns``.

    Returns:
        A tuple key placing missing timestamps last.
    """
    ns = record.get("timestamp_ns")
    return (ns is None, ns or 0)


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def load(images_file: str, positions_file: str) -> tuple[list, dict]:
    """Load and validate the two source JSON files.

    Args:
        images_file: Path to mission_images.json.
        positions_file: Path to mission_positions.json.

    Returns:
        A ``(images, positions)`` tuple.

    Raises:
        FileNotFoundError: If either file does not exist.
        ValueError: If either file is not valid JSON.
    """
    for path in (images_file, positions_file):
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Required input not found: {path}")
    try:
        with open(images_file, "r", encoding="utf-8") as handle:
            images = json.load(handle)
        with open(positions_file, "r", encoding="utf-8") as handle:
            positions = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in source file: {exc}") from exc
    return images, positions


# --------------------------------------------------------------------------- #
# describe
# --------------------------------------------------------------------------- #
def describe(images: list, positions: dict) -> None:
    """Print a human-readable summary of both source files to stdout.

    Args:
        images: The parsed mission_images list.
        positions: The parsed mission_positions dict.
    """
    unique_channels: set[str] = set()
    total_images = 0
    for attempt in images:
        for channel in attempt.get("channels", []):
            unique_channels.add(channel["channel_name"])
            total_images += len(channel.get("images", []))

    print("=" * 72)
    print(" mission_images.json")
    print("=" * 72)
    print(f"  type           : list")
    print(f"  attempts       : {len(images)}")
    print(f"  unique channels: {len(unique_channels)}")
    print(f"  total images   : {total_images}")
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
    print("=" * 72)
    print()


# --------------------------------------------------------------------------- #
# channels.json
# --------------------------------------------------------------------------- #
def build_channels(images: list, output_file: str) -> dict:
    """Aggregate channels and write channels.json.

    Args:
        images: The parsed mission_images list.
        output_file: Destination path for channels.json.

    Returns:
        The channels summary dict that was written.
    """
    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "attempts_present": 0,
            "total_images": 0,
            "extensions": set(),
            "example_path": None,
        }
    )
    for attempt in images:
        for channel in attempt.get("channels", []):
            name = channel["channel_name"]
            count = len(channel.get("images", []))
            entry = agg[name]
            entry["attempts_present"] += 1
            entry["total_images"] += count
            for image in channel.get("images", []):
                if image.get("extension"):
                    entry["extensions"].add(image["extension"])
            if entry["example_path"] is None:
                if channel.get("images"):
                    entry["example_path"] = channel["images"][0].get("path")
                elif channel.get("channel_folder"):
                    entry["example_path"] = channel["channel_folder"]

    channels = [
        {
            "name": name,
            "short": channel_short(name),
            "label": humanize_channel(name),
            "category": channel_category(name),
            "attempts_present": agg[name]["attempts_present"],
            "total_images": agg[name]["total_images"],
            "has_images": agg[name]["total_images"] > 0,
            "extensions": sorted(agg[name]["extensions"]),
            "example_path": agg[name]["example_path"],
        }
        for name in sorted(agg)
    ]
    out = {
        "generated_at": now_iso(),
        "source_file": "mission_images.json",
        "total_channels": len(channels),
        "categories": sorted({c["category"] for c in channels}),
        "channels": channels,
    }
    with open(output_file, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=2)
    print(f"wrote {output_file}  ({len(channels)} channels)")
    return out


# --------------------------------------------------------------------------- #
# attempts.json
# --------------------------------------------------------------------------- #
def build_attempts(images: list, output_file: str) -> dict:
    """Aggregate attempts and write attempts.json.

    Args:
        images: The parsed mission_images list.
        output_file: Destination path for attempts.json.

    Returns:
        The attempts summary dict that was written.
    """
    attempts = []
    for attempt in images:
        channel_list = []
        total_images = 0
        for channel in attempt.get("channels", []):
            count = len(channel.get("images", []))
            total_images += count
            channel_list.append(
                {
                    "name": channel["channel_name"],
                    "label": humanize_channel(channel["channel_name"]),
                    "category": channel_category(channel["channel_name"]),
                    "image_count": count,
                }
            )
        attempts.append(
            {
                "attempt_folder": attempt.get("attempt_folder"),
                "group_name": attempt.get("group_name"),
                "action_name": attempt.get("action_name"),
                "started": fmt_ts(attempt.get("started")),
                "ended": fmt_ts(attempt.get("ended")),
                "channel_count": len(channel_list),
                "total_images": total_images,
                "channels": channel_list,
            }
        )
    attempts.sort(key=sort_key)
    out = {
        "generated_at": now_iso(),
        "source_file": "mission_images.json",
        "total_attempts": len(attempts),
        "attempts": attempts,
    }
    with open(output_file, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=2)
    print(f"wrote {output_file}  ({len(attempts)} attempts)")
    return out


# --------------------------------------------------------------------------- #
# SVG maps per source
# --------------------------------------------------------------------------- #
def make_svg(
    source_rel: str,
    records: list,
    first_ts: str | None = None,
    last_ts: str | None = None,
) -> tuple[str | None, int]:
    """Build an SVG string for one source's (x, y) trajectory.

    Args:
        source_rel: The source relative path (used for titles/labels).
        records: Position records for this source.
        first_ts: First timestamp of the source (metadata).
        last_ts: Last timestamp of the source (metadata).

    Returns:
        A ``(svg_string, point_count)`` tuple, or ``(None, 0)`` if too few
        valid points exist.
    """
    points = [
        (r["position"]["x"], r["position"]["y"])
        for r in records
        if isinstance(r.get("position"), dict)
        and r["position"].get("x") is not None
        and r["position"].get("y") is not None
    ]
    if len(points) < 2:
        return None, 0

    width = height = SVG_SIZE
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    count = len(points)
    span_x = max(1e-6, max_x - min_x)
    span_y = max(1e-6, max_y - min_y)
    span = max(span_x, span_y)  # keep aspect square
    scale = (width - 2 * SVG_MARGIN) / span

    def to_x(value: float) -> float:
        """Map a world X coordinate to SVG pixel space."""
        return SVG_MARGIN + (value - min_x) * scale + (span - span_x) / 2 * scale

    def to_y(value: float) -> float:
        """Map a world Y coordinate to SVG pixel space (flipped so +y is up)."""
        return (
            height - SVG_MARGIN - (value - min_y) * scale
            - (span - span_y) / 2 * scale
        )

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'font-family="sans-serif">',
        f"<title>{_esc(source_rel)}</title>",
        f"<desc>source: {_esc(source_rel)} | points: {count} | "
        f"first: {_esc(first_ts)} | last: {_esc(last_ts)}</desc>",
        '<rect width="100%" height="100%" fill="#0f1420"/>',
    ]
    for gx in range(0, width + 1, 100):
        parts.append(
            f'<line x1="{gx}" y1="0" x2="{gx}" y2="{height}" '
            f'stroke="#1d2638" stroke-width="1"/>'
        )
    for gy in range(0, height + 1, 100):
        parts.append(
            f'<line x1="0" y1="{gy}" x2="{width}" y2="{gy}" '
            f'stroke="#1d2638" stroke-width="1"/>'
        )

    for i in range(1, count):
        x0, y0 = points[i - 1]
        x1, y1 = points[i]
        color = _hex_color((i - 1) / max(1, count - 1))
        parts.append(
            f'<line x1="{to_x(x0):.1f}" y1="{to_y(y0):.1f}" '
            f'x2="{to_x(x1):.1f}" y2="{to_y(y1):.1f}" '
            f'stroke="{color}" stroke-width="2.2" stroke-linecap="round"/>'
        )

    start_x, start_y = to_x(points[0][0]), to_y(points[0][1])
    end_x, end_y = to_x(points[-1][0]), to_y(points[-1][1])
    parts.append(
        f'<circle cx="{start_x:.1f}" cy="{start_y:.1f}" r="7" '
        f'fill="#2ca02c" stroke="#fff" stroke-width="1.5"/>'
    )
    parts.append(
        f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="7" '
        f'fill="#ffffff" stroke="#000" stroke-width="1.5"/>'
    )

    label = source_rel.split("/")[0]
    bounds = (
        f"{count} points  |  X[{min_x:.2f}, {max_x:.2f}] "
        f"Y[{min_y:.2f}, {max_y:.2f}]"
    )
    legend = "start=green  end=white  time=blue&#8594;red"
    parts.append(
        f'<text x="20" y="32" fill="#e6edf3" font-size="20" '
        f'font-weight="bold">{_esc(label)}</text>'
    )
    parts.append(
        f'<text x="20" y="56" fill="#9aa7b5" '
        f'font-size="13">{_esc(source_rel)}</text>'
    )
    parts.append(
        f'<text x="20" y="{height - 28}" fill="#9aa7b5" font-size="12">'
        f"{bounds}  |  {legend}</text>"
    )
    parts.append(
        f'<text x="{width - 20}" y="{height - 28}" fill="#9aa7b5" '
        f'font-size="12" text-anchor="end">x,y meters</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts), count


def build_svgs(positions: dict, svg_dir: str, verbose: bool) -> dict:
    """Write one SVG map per position source, plus svg/index.json.

    Args:
        positions: The parsed mission_positions dict.
        svg_dir: Directory to write SVG files into.
        verbose: When True, show a per-source progress bar.

    Returns:
        An index dict describing every map written.
    """
    os.makedirs(svg_dir, exist_ok=True)
    by_source: dict[str, list] = defaultdict(list)
    for record in positions["records"]:
        by_source[record["source"]].append(record)
    meta = {s["rel_path"]: s for s in positions.get("sources", [])}

    written = 0
    skipped = 0
    sources = sorted(by_source)
    for source in _progress(sources, len(sources), "svg", verbose):
        records = by_source[source]
        records.sort(key=record_sort_key)
        source_meta = meta.get(source, {})
        svg, _count = make_svg(
            source,
            records,
            source_meta.get("first_timestamp"),
            source_meta.get("last_timestamp"),
        )
        if svg is None:
            skipped += 1
            continue
        out_path = os.path.join(svg_dir, f"{safe_filename(source)}.svg")
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(svg)
        written += 1

    index = {
        "generated_at": now_iso(),
        "source_file": "mission_positions.json",
        "svg_directory": os.path.basename(svg_dir),
        "total_sources": positions.get("source_count"),
        "maps_written": written,
        "maps_skipped": skipped,
        "maps": sorted(
            [
                {
                    "source": source,
                    "file": f"{safe_filename(source)}.svg",
                    "points": len(by_source[source]),
                    "first_timestamp": meta.get(source, {}).get(
                        "first_timestamp"
                    ),
                    "last_timestamp": meta.get(source, {}).get(
                        "last_timestamp"
                    ),
                }
                for source in by_source
            ],
            key=lambda d: d["source"],
        ),
    }
    index_path = os.path.join(svg_dir, "index.json")
    with open(index_path, "w", encoding="utf-8") as handle:
        json.dump(index, handle, indent=2)
    print(
        f"wrote {svg_dir}/  ({written} maps, {skipped} skipped) "
        f"+ {os.path.basename(svg_dir)}/index.json"
    )
    return index


# --------------------------------------------------------------------------- #
# execution log
# --------------------------------------------------------------------------- #
def write_log(
    log_file: str,
    started_at: str,
    duration_sec: float,
    parameters: dict[str, Any],
    results: dict[str, Any],
) -> None:
    """Write a structured execution log to script.json.

    Args:
        log_file: Destination path for the log.
        started_at: ISO timestamp marking run start.
        duration_sec: Wall-clock seconds the run took.
        parameters: Effective parameters used for the run.
        results: Summary counts of each produced artifact.
    """
    payload = {
        "started_at": started_at,
        "ended_at": now_iso(),
        "duration_sec": round(duration_sec, 4),
        "parameters": parameters,
        "results": results,
        "status": "ok",
    }
    with open(log_file, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the argument parser and parse command-line arguments.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        The parsed ``argparse.Namespace``.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Describe mission_images.json and mission_positions.json, then "
            "emit channels.json, attempts.json, and per-source SVG maps."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--params",
        default=DEFAULT_PARAMS_FILE,
        help="JSON file with standard parameters.",
    )
    parser.add_argument(
        "--images",
        default=None,
        help="Override the mission images JSON path.",
    )
    parser.add_argument(
        "--positions",
        default=None,
        help="Override the mission positions JSON path.",
    )
    parser.add_argument(
        "--svg-dir",
        default=None,
        help="Override the SVG output directory.",
    )
    parser.add_argument(
        "--channels-out",
        default=None,
        help="Override the channels.json output path.",
    )
    parser.add_argument(
        "--attempts-out",
        default=None,
        help="Override the attempts.json output path.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Override the script.json log path.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show progress bars and debug logging.",
    )
    parser.add_argument(
        "--cprofile",
        action="store_true",
        help="Profile the run with cProfile and print stats.",
    )
    return parser.parse_args(argv)


def resolve_params(args: argparse.Namespace) -> dict[str, Any]:
    """Load parameters.json and apply CLI overrides.

    Args:
        args: Parsed CLI arguments.

    Returns:
        The effective parameter dict.
    """
    params_path = (
        args.params
        if os.path.isabs(args.params)
        else os.path.join(HERE, args.params)
    )
    params: dict[str, Any] = {}
    if os.path.isfile(params_path):
        with open(params_path, "r", encoding="utf-8") as handle:
            params = json.load(handle)
    # CLI overrides take precedence.
    overrides = {
        "images_file": args.images,
        "positions_file": args.positions,
        "svg_directory": args.svg_dir,
        "channels_output": args.channels_out,
        "attempts_output": args.attempts_out,
        "log_file": args.log_file,
    }
    for key, value in overrides.items():
        if value is not None:
            params[key] = value
    # Sensible defaults if nothing was supplied.
    params.setdefault("images_file", "mission_images.json")
    params.setdefault("positions_file", "mission_positions.json")
    params.setdefault("svg_directory", "svg")
    params.setdefault("channels_output", "channels.json")
    params.setdefault("attempts_output", "attempts.json")
    params.setdefault("log_file", DEFAULT_LOG_FILE)
    return params


def run(params: dict[str, Any], verbose: bool) -> dict[str, Any]:
    """Execute the full pipeline and return a results summary.

    Args:
        params: Effective parameter dict.
        verbose: Whether progress bars are shown.

    Returns:
        A dict of artifact summaries for the execution log.
    """
    def _abs(value: str) -> str:
        """Resolve a possibly-relative path against the script directory."""
        return value if os.path.isabs(value) else os.path.join(HERE, value)

    images_file = _abs(params["images_file"])
    positions_file = _abs(params["positions_file"])
    images, positions = load(images_file, positions_file)

    describe(images, positions)
    channels = build_channels(images, params["channels_output"])
    attempts = build_attempts(images, params["attempts_output"])
    svgs = build_svgs(positions, params["svg_directory"], verbose)

    return {
        "channels": {"total_channels": channels["total_channels"]},
        "attempts": {"total_attempts": attempts["total_attempts"]},
        "svgs": {
            "maps_written": svgs["maps_written"],
            "maps_skipped": svgs["maps_skipped"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse args, run, and write the execution log.

    Args:
        argv: Optional argument list for testing.

    Returns:
        Process exit code (0 on success).
    """
    args = parse_args(argv)
    params = resolve_params(args)

    if args.cprofile:
        profiler = cProfile.Profile()
        started = now_iso()
        start = datetime.now(timezone.utc)
        profiler.enable()
        results = run(params, args.verbose)
        profiler.disable()
        duration = (datetime.now(timezone.utc) - start).total_seconds()
        buffer = io.StringIO()
        stats = pstats.Stats(profiler, stream=buffer).sort_stats("cumulative")
        stats.print_stats(20)
        print(buffer.getvalue())
    else:
        started = now_iso()
        start = datetime.now(timezone.utc)
        results = run(params, args.verbose)
        duration = (datetime.now(timezone.utc) - start).total_seconds()

    write_log(params["log_file"], started, duration, params, results)
    print(f"wrote {params['log_file']}  (run log)")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
