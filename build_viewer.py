#!/usr/bin/env python3
"""
build_viewer.py
---------------
Build a self-contained HTML viewer for mission images.

It reads mission_images.json + activeChannels.json and emits:

  * viewer_data.js  - compact per-attempt/per-channel data
                      (sorted millisecond timestamps + filenames) for the
                      channels named in activeChannels.json.
  * index.html      - a 4-row image viewer (from activeChannels.json) with an
                      attempt drop-down and a 1-second scrubber that shows, for
                      every active channel, the image whose filename timestamp
                      is closest to the selected second.

The HTML is designed to be opened directly via file:// : it loads
viewer_data.js through a <script> tag (no fetch/CORS) and references the
real image files under --image-base (default inspections). The first row
also shows the attempt's trajectory map from svg/.

Usage:
    python3 build_viewer.py
    python3 build_viewer.py --image-base ../work --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

HERE: str = os.path.dirname(os.path.abspath(__file__))

_TS_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})(?:-(\d+))?Z"
)

# Extensions an <img> tag can actually render.
IMG_EXTS: set[str] = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def frame_key(filename: str) -> str:
    """Return the timestamp stem of a frame filename (up to and incl. 'Z').

    This lets a ``.raw`` data frame and its decoded ``.png`` (which carries a
    colormap/range suffix) be matched even though their full names differ.

    Args:
        filename: A frame filename like ``2026-...Z.raw`` or
            ``2026-...Z_be8_5.0-75.0C_rainbow_with_scale.png``.

    Returns:
        The leading ``...Z`` timestamp portion.
    """
    idx = filename.find("Z")
    return filename[: idx + 1] if idx >= 0 else filename


def resolve_channel_frames(
    abs_base: str, folder: str, images: list[dict[str, Any]]
) -> list[tuple[float, str]]:
    """Map a channel's recorded frames to viewable image paths.

    Args:
        abs_base: Absolute image root (e.g. .../backup/work).
        folder: The channel_folder relative path.
        images: Image entries from mission_images.json.

    Returns:
        A list of ``(epoch_seconds, viewable_relative_path)`` tuples, sorted
        by timestamp. For .raw channels the path points at the decoded png.
    """
    parsed = []
    for image in images:
        ts = parse_filename_ts(image.get("timestamp_from_filename", ""))
        if ts is not None:
            parsed.append((ts, image.get("filename", "")))
    if not parsed:
        return []

    # Fast path: the recorded files are already displayable images.
    if all(os.path.splitext(fn)[1].lower() in IMG_EXTS for _, fn in parsed):
        return sorted(parsed, key=lambda p: p[0])

    # Slow path: data frames (e.g. .raw) -> find decoded images on disk.
    folder_abs = os.path.join(abs_base, folder)
    key_map: dict[str, str] = {}
    if os.path.isdir(folder_abs):
        for root, dirs, files in os.walk(folder_abs):
            depth = root[len(folder_abs):].count(os.sep)
            if depth > 1:  # only channel dir + immediate subdirs (png/, raw/)
                dirs[:] = []
                continue
            for fname in files:
                if os.path.splitext(fname)[1].lower() in IMG_EXTS:
                    rel = os.path.relpath(os.path.join(root, fname), folder_abs)
                    key_map.setdefault(frame_key(fname), rel)

    resolved = []
    for ts, fname in parsed:
        rel = key_map.get(frame_key(fname))
        if rel:
            resolved.append((ts, rel))
    return sorted(resolved, key=lambda p: p[0])


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def parse_filename_ts(value: str) -> float | None:
    """Parse a 'YYYY-MM-DDTHH-MM-SS-fractionZ' filename into epoch seconds.

    Args:
        value: A timestamp_from_filename string.

    Returns:
        Epoch seconds (float, with fractional component), or None.
    """
    match = _TS_RE.match(value)
    if not match:
        return None
    year, mon, day, hh, mm, ss, frac = match.groups()
    base = datetime(
        int(year), int(mon), int(day), int(hh), int(mm), int(ss),
        tzinfo=timezone.utc,
    ).timestamp()
    if frac:
        base += int(frac) / (10 ** len(frac))
    return base


def short_label(folder: str) -> str:
    """Return a compact label for an attempt folder.

    Args:
        folder: The full attempt_folder path.

    Returns:
        A short "group / action" style label.
    """
    parts = folder.split("/")
    group = parts[0] if parts else folder
    action = parts[1] if len(parts) > 1 else ""
    # Trim a leading ISO timestamp from the group for readability.
    group = re.sub(r"^\d{4}-\d{2}-\d{2}T\d{4,6}Z_", "", group)
    return f"{group} / {action}".strip(" /")


# --------------------------------------------------------------------------- #
# data building
# --------------------------------------------------------------------------- #
def load_svg_map(svg_index_path: str) -> dict[str, str]:
    """Load svg/index.json and return an attempt_folder -> SVG filename map.

    Each map's ``source`` is ``<attempt_folder>/raw-robot--graphnav-localization``,
    so the attempt folder is the source with its last path segment removed.

    Args:
        svg_index_path: Path to svg/index.json.

    Returns:
        A dict mapping attempt folder paths to SVG filenames.
    """
    if not os.path.isfile(svg_index_path):
        return {}
    with open(svg_index_path, encoding="utf-8") as handle:
        idx = json.load(handle)
    out: dict[str, str] = {}
    for entry in idx.get("maps", []):
        folder = "/".join(entry["source"].split("/")[:-1])
        out[folder] = entry["file"]
    return out


def build_data(
    images: list,
    active_channels: dict[str, list[str]],
    image_base: str,
    svg_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Aggregate active-channel images into a compact viewer payload.

    Args:
        images: The parsed mission_images list.
        active_channels: The row->channels mapping from activeChannels.json.
        image_base: Relative base path where real images live.

    Returns:
        A dict with image_base and an ``attempts`` list.
    """
    wanted = {c for chans in active_channels.values() for c in chans}
    abs_base = (
        image_base if os.path.isabs(image_base) else os.path.join(HERE, image_base)
    )
    attempts_out: list[dict[str, Any]] = []

    for attempt in images:
        channels_out: dict[str, dict[str, Any]] = {}
        for channel in attempt.get("channels", []):
            name = channel["channel_name"]
            if name not in wanted or not channel.get("images"):
                continue
            folder = channel.get("channel_folder", "")
            pairs = resolve_channel_frames(abs_base, folder, channel["images"])
            if not pairs:
                continue
            channels_out[name] = {
                "folder": folder,
                "t": [int(round(p[0] * 1000)) for p in pairs],
                "f": [p[1] for p in pairs],
            }
        if not channels_out:
            continue

        all_ts = [t for ch in channels_out.values() for t in ch["t"]]
        attempts_out.append({
            "folder": attempt.get("attempt_folder"),
            "label": short_label(attempt.get("attempt_folder", "")),
            "started": attempt.get("started"),
            "ended": attempt.get("ended"),
            "start_ms": min(all_ts),
            "end_ms": max(all_ts),
            "svg": (svg_map or {}).get(attempt.get("attempt_folder", "")),
            "channels": channels_out,
        })

    attempts_out.sort(key=lambda a: a["start_ms"])
    return {"image_base": image_base, "attempts": attempts_out}


def write_data_js(data: dict[str, Any], path: str) -> None:
    """Write the viewer payload as a window.VIEWER_DATA assignment.

    Args:
        data: The payload from build_data.
        path: Destination .js file path.
    """
    # Compact separators keep the file small.
    body = json.dumps(data, separators=(",", ":"))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("window.VIEWER_DATA = ")
        handle.write(body)
        handle.write(";\n")


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FCX Mission Image Viewer</title>
<style>
  :root {{
    --bg:#0f1420; --panel:#171f2e; --panel2:#1d2638; --txt:#e6edf3;
    --mut:#9aa7b5; --acc:#2f81f7; --line:#2a3650;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--txt);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  }}
  header {{
    position:sticky; top:0; z-index:5; background:var(--panel);
    border-bottom:1px solid var(--line); padding:10px 16px;
    display:flex; flex-wrap:wrap; gap:12px; align-items:center;
  }}
  header h1 {{ font-size:16px; margin:0; margin-right:auto; font-weight:600; }}
  select, button, input[type=range] {{
    background:var(--panel2); color:var(--txt); border:1px solid var(--line);
    border-radius:6px; padding:6px 10px; font-size:13px;
  }}
  button {{ cursor:pointer; }}
  button:hover {{ border-color:var(--acc); }}
  button:disabled {{ opacity:.4; cursor:default; }}
  #time {{ font-variant-numeric:tabular-nums; min-width:96px; text-align:center;
           color:var(--acc); font-weight:600; }}
  .scrub {{ display:flex; align-items:center; gap:8px; flex:1 1 320px; }}
  input[type=range] {{ flex:1; }}
  main {{ padding:14px 16px 40px; }}
  .row {{ margin-bottom:14px; }}
  .row-title {{ color:var(--mut); font-size:12px; letter-spacing:.04em;
               text-transform:uppercase; margin:0 0 6px; }}
  .cells {{ display:flex; gap:10px; }}
  .cell {{ flex:1 1 0; background:var(--panel); border:1px solid var(--line);
          border-radius:8px; overflow:hidden; min-width:0; }}
  .cell .cap {{ display:flex; justify-content:space-between; gap:8px;
               padding:5px 8px; font-size:11px; color:var(--mut);
               background:var(--panel2); }}
  .cell .cap b {{ color:var(--txt); font-weight:600; }}
  .cell img {{ width:100%; display:block; background:#000;
              min-height:90px; object-fit:contain; }}
  .cell-map {{ flex:0 0 230px; }}
  .cell-map img {{ aspect-ratio:1/1; background:#0f1420; }}
  .empty {{ padding:18px 8px; text-align:center; color:var(--mut); font-size:12px; }}
  .placeholder {{ padding:30px; text-align:center; color:var(--mut); }}
</style>
</head>
<body>
<header>
  <h1>FCX Mission Viewer</h1>
  <label style="display:flex;gap:6px;align-items:center;">
    <span style="color:var(--mut);font-size:12px;">Attempt</span>
    <select id="attempt"></select>
  </label>
  <div class="scrub">
    <button id="prev" title="&#8722;1 s">&#9664;</button>
    <input type="range" id="slider" min="0" max="1" step="1" value="0" disabled>
    <button id="next" title="+1 s">&#9654;</button>
    <span id="time">--:--:--</span>
    <button id="play">Play</button>
  </div>
</header>
<main id="main"><div class="placeholder">Select an attempt to begin.</div></main>

<script src="viewer_data.js"></script>
<script>
__CHANNELS__JSON__;
(function () {{
  "use strict";
  const DATA = window.VIEWER_DATA || {{ attempts: [] }};
  const ROWS = window.ACTIVE_CHANNELS;
  const IMG_BASE = DATA.image_base || "../work";
  const attemptSel = document.getElementById("attempt");
  const slider = document.getElementById("slider");
  const timeEl = document.getElementById("time");
  const mainEl = document.getElementById("main");
  const prevBtn = document.getElementById("prev");
  const nextBtn = document.getElementById("next");
  const playBtn = document.getElementById("play");
  let cur = null;     // current attempt object
  let playTimer = null;

  function fmtClock(ms) {{
    const d = new Date(ms);
    const p = n => String(n).padStart(2, "0");
    return p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" +
           p(d.getUTCSeconds()) + "Z";
  }}

  // Build the static 4-row scaffold once.
  function buildScaffold() {{
    mainEl.innerHTML = "";
    for (const [rowKey, chans] of Object.entries(ROWS)) {{
      const row = document.createElement("div");
      row.className = "row";
      row.dataset.row = rowKey;
      const title = document.createElement("div");
      title.className = "row-title";
      title.textContent = rowKey + "  (" + chans.length + ")";
      const cells = document.createElement("div");
      cells.className = "cells";
      if (rowKey === "row_1") {{
        const mapCell = document.createElement("div");
        mapCell.className = "cell cell-map";
        mapCell.dataset.channel = "__map__";
        mapCell.innerHTML =
          '<div class="cap"><b>trajectory</b><span class="t">map</span></div>' +
          '<div class="empty">no map</div>';
        cells.appendChild(mapCell);
      }}
      for (const ch of chans) {{
        const cell = document.createElement("div");
        cell.className = "cell";
        cell.dataset.channel = ch;
        cell.innerHTML =
          '<div class="cap"><b>' + ch + '</b><span class="t">—</span></div>' +
          '<div class="empty">no data</div>';
        cells.appendChild(cell);
      }}
      row.appendChild(title);
      row.appendChild(cells);
      mainEl.appendChild(row);
    }}
  }}

  // Binary search: index of timestamp nearest to targetMs.
  function nearestIndex(t, targetMs) {{
    let lo = 0, hi = t.length - 1;
    if (targetMs <= t[0]) return 0;
    if (targetMs >= t[hi]) return hi;
    while (lo < hi) {{
      const mid = (lo + hi) >> 1;
      if (t[mid] < targetMs) lo = mid + 1; else hi = mid;
    }}
    if (lo > 0 && Math.abs(t[lo - 1] - targetMs) <= Math.abs(t[lo] - targetMs))
      return lo - 1;
    return lo;
  }}

  function imgSrc(folder, file) {{
    return encodeURI(IMG_BASE + "/" + folder + "/" + file);
  }}

  function render(targetMs) {{
    if (!cur) return;
    timeEl.textContent = fmtClock(targetMs);
    for (const cell of mainEl.querySelectorAll(".cell")) {{
      const tspan = cell.querySelector(".t");
      if (cell.dataset.channel === "__map__") {{
        let img = cell.querySelector("img");
        if (cur.svg) {{
          if (!img) {{
            const e = cell.querySelector(".empty"); if (e) e.remove();
            img = document.createElement("img");
            cell.appendChild(img);
          }}
          const src = encodeURI("svg/" + cur.svg);
          if (img.dataset.src !== src) {{ img.src = src; img.dataset.src = src; }}
          tspan.textContent = "map";
        }} else {{
          if (img) img.remove();
          if (!cell.querySelector(".empty"))
            cell.insertAdjacentHTML("beforeend", '<div class="empty">no map</div>');
          tspan.textContent = "—";
        }}
        continue;
      }}
      const ch = cur.channels[cell.dataset.channel];
      if (!ch) {{
        cell.querySelector("img, .empty")?.remove();
        if (!cell.querySelector(".empty"))
          cell.insertAdjacentHTML("beforeend", '<div class="empty">no data</div>');
        tspan.textContent = "—";
        continue;
      }}
      const idx = nearestIndex(ch.t, targetMs);
      const ms = ch.t[idx], file = ch.f[idx];
      tspan.textContent = fmtClock(ms);
      let img = cell.querySelector("img");
      if (!img) {{
        const e = cell.querySelector(".empty"); if (e) e.remove();
        img = document.createElement("img");
        cell.appendChild(img);
      }}
      const src = imgSrc(ch.folder, file);
      if (img.dataset.src !== src) {{ img.src = src; img.dataset.src = src; }}
    }}
  }}

  function loadAttempt(i) {{
    cur = DATA.attempts[i] || null;
    if (!cur) return;
    const start = Math.floor(cur.start_ms / 1000);
    const end = Math.floor(cur.end_ms / 1000);
    slider.min = start; slider.max = end; slider.step = 1; slider.value = start;
    slider.disabled = false;
    prevBtn.disabled = nextBtn.disabled = playBtn.disabled = false;
    render(start * 1000);
  }}

  // --- events ---
  attemptSel.addEventListener("change", () => loadAttempt(+attemptSel.value));
  slider.addEventListener("input", () => render(+slider.value * 1000));
  prevBtn.addEventListener("click", () => {{
    slider.value = Math.max(+slider.min, +slider.value - 1); render(+slider.value*1000);
  }});
  nextBtn.addEventListener("click", () => {{
    slider.value = Math.min(+slider.max, +slider.value + 1); render(+slider.value*1000);
  }});
  function setPlay(on) {{
    if (on && !playTimer) {{
      playTimer = setInterval(() => {{
        let v = +slider.value + 1;
        if (v >= +slider.max) {{ v = +slider.max; setPlay(false); }}
        slider.value = v; render(v * 1000);
      }}, 220);
      playBtn.textContent = "Pause";
    }} else {{
      clearInterval(playTimer); playTimer = null; playBtn.textContent = "Play";
    }}
  }}
  playBtn.addEventListener("click", () => setPlay(!playTimer));
  document.addEventListener("keydown", e => {{
    if (e.target === attemptSel) return;
    if (e.key === "ArrowLeft") prevBtn.click();
    else if (e.key === "ArrowRight") nextBtn.click();
    else if (e.key === " ") {{ e.preventDefault(); playBtn.click(); }}
  }});

  // --- init ---
  buildScaffold();
  DATA.attempts.forEach((a, i) => {{
    const opt = document.createElement("option");
    opt.value = i;
    opt.textContent = a.label + "  [" + fmtClock(a.start_ms) + "]";
    attemptSel.appendChild(opt);
  }});
  if (DATA.attempts.length) loadAttempt(0);
}})();
</script>
</body>
</html>
"""


def write_html(active_channels: dict[str, list[str]], path: str) -> None:
    """Write index.html, injecting the activeChannels layout.

    Args:
        active_channels: The row->channels mapping.
        path: Destination .html file path.
    """
    channels_js = "window.ACTIVE_CHANNELS = " + json.dumps(active_channels) + ";"
    # The template uses doubled braces (str.format-style escaping); since we
    # inject via .replace(), collapse them to single braces first, then drop in
    # the channels JSON (which carries its own single braces).
    html = HTML_TEMPLATE.replace("{{", "{").replace("}}", "}")
    html = html.replace("__CHANNELS__JSON__;", channels_js)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Optional argument list.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(
        description="Build the FCX mission image viewer (HTML + data).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--images", default="mission_images.json",
                        help="mission_images.json path.")
    parser.add_argument("--active", default="activeChannels.json",
                        help="activeChannels.json path.")
    parser.add_argument("--image-base", default="inspections",
                        help="relative base path where image files live.")
    parser.add_argument("--data-out", default="viewer_data.js",
                        help="output JS data file.")
    parser.add_argument("--html-out", default="index.html",
                        help="output HTML file.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="verbose logging.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: build the data + HTML viewer.

    Args:
        argv: Optional argument list.

    Returns:
        Process exit code.
    """
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s", stream=sys.stderr,
    )
    log = logging.getLogger("build_viewer")

    images_path = os.path.join(HERE, args.images) if not os.path.isabs(args.images) else args.images
    active_path = os.path.join(HERE, args.active) if not os.path.isabs(args.active) else args.active

    for p in (images_path, active_path):
        if not os.path.isfile(p):
            raise FileNotFoundError(f"Required input not found: {p}")

    with open(images_path, encoding="utf-8") as h:
        images = json.load(h)
    with open(active_path, encoding="utf-8") as h:
        active = json.load(h)

    svg_map = load_svg_map(os.path.join(HERE, "svg", "index.json"))
    data = build_data(images, active, args.image_base, svg_map)
    write_data_js(data, os.path.join(HERE, args.data_out))
    write_html(active, os.path.join(HERE, args.html_out))

    n = len(data["attempts"])
    size = os.path.getsize(os.path.join(HERE, args.data_out))
    log.info("wrote %s (%d attempts, %.1f MB)", args.data_out, n, size / 1e6)
    log.info("wrote %s", args.html_out)
    print(f"Done: {args.html_out} + {args.data_out} ({n} attempts).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
