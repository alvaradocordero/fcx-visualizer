"""
test_generate.py
----------------
pytest suite for the non-trivial pure functions in generate.py.

Run with:  pytest -q
"""

import json
import os
import sys

import pytest

# Make the package importable when tests live in tests/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import generate  # noqa: E402


# --------------------------------------------------------------------------- #
# channel_category
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name, expected",
    [
        ("raw-image--frontleft_fisheye_image", "image"),
        ("image_back_fisheye_image", "image"),
        ("raw-robot--graphnav-localization", "robot"),
        ("raw-spotcam--ir-radiometric", "spotcam"),
        ("raw-spotcam--audio", "spotcam"),
        ("raw-sv600--video", "sv600"),
        ("raw-sv600--beamforming", "sv600"),
        ("something-unexpected", "other"),
    ],
)
def test_channel_category(name, expected):
    """Channel names map to the correct high-level category."""
    assert generate.channel_category(name) == expected


# --------------------------------------------------------------------------- #
# channel_short
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name, expected",
    [
        ("raw-image--MecQ_Soundsurface", "MecQ_Soundsurface"),
        ("image_back_fisheye_image", "image"),
        ("plain", "plain"),
    ],
)
def test_channel_short(name, expected):
    """The tail after the last separator is returned."""
    assert generate.channel_short(name) == expected


# --------------------------------------------------------------------------- #
# humanize_channel
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name, expected",
    [
        ("raw-image--frontleft_fisheye_image", "frontleft_fisheye_image"),
        ("image_back_fisheye_image", "back_fisheye_image"),
        ("raw-robot--state", "raw-robot--state"),
        ("unknown", "unknown"),
    ],
)
def test_humanize_channel(name, expected):
    """Data-source prefixes are stripped for human labels."""
    assert generate.humanize_channel(name) == expected


# --------------------------------------------------------------------------- #
# _hex_color
# --------------------------------------------------------------------------- #
def test_hex_color_endpoints():
    """Boundary values return the exact ramp endpoints."""
    assert generate._hex_color(0.0) == "#1f77b4"
    assert generate._hex_color(1.0) == "#d62728"


def test_hex_color_clamps():
    """Values outside [0, 1] are clamped, never erroring."""
    assert generate._hex_color(-5) == "#1f77b4"
    assert generate._hex_color(5) == "#d62728"


def test_hex_color_format():
    """Every color is a valid #rrggbb string."""
    for t in (0.1, 0.25, 0.5, 0.75, 0.9):
        color = generate._hex_color(t)
        assert color.startswith("#") and len(color) == 7


# --------------------------------------------------------------------------- #
# _esc
# --------------------------------------------------------------------------- #
def test_esc_replaces_special_chars():
    """XML-significant characters are escaped."""
    assert generate._esc("a & b < c > d") == "a &amp; b &lt; c &gt; d"


def test_esc_none_to_empty():
    """None collapses to the empty string."""
    assert generate._esc(None) == ""


def test_esc_non_string():
    """Non-string values are coerced to strings before escaping."""
    assert generate._esc(42) == "42"


# --------------------------------------------------------------------------- #
# safe_filename
# --------------------------------------------------------------------------- #
def test_safe_filename_flattens_and_clamps():
    """Path separators collapse and length is capped."""
    source = "group/SKG Start - 1/raw-robot--graphnav-localization"
    out = generate.safe_filename(source)
    assert "/" not in out
    assert out.endswith("raw-robot--graphnav-localization")


def test_safe_filename_clamps_long_input():
    """Over-long inputs are truncated to the configured maximum."""
    long_source = "a" * 500
    assert len(generate.safe_filename(long_source)) == generate.MAX_LINE_FILENAME


# --------------------------------------------------------------------------- #
# sort_key
# --------------------------------------------------------------------------- #
def test_sort_key_orders_and_pushes_nulls_last():
    """Started times sort ascending; nulls go to the end."""
    attempts = [
        {"started": None},
        {"started": "2026-05-07T20:52:03Z"},
        {"started": "2026-04-28T22:23:18Z"},
    ]
    ordered = sorted(attempts, key=generate.sort_key)
    assert ordered[0]["started"] == "2026-04-28T22:23:18Z"
    assert ordered[-1]["started"] is None


def test_record_sort_key_handles_missing():
    """Missing timestamp_ns sorts after present values."""
    records = [
        {"timestamp_ns": None},
        {"timestamp_ns": 5},
        {"timestamp_ns": 1},
    ]
    ordered = sorted(records, key=generate.record_sort_key)
    assert [r["timestamp_ns"] for r in ordered] == [1, 5, None]


# --------------------------------------------------------------------------- #
# make_svg
# --------------------------------------------------------------------------- #
def _record(x, y, ns):
    return {
        "position": {"x": x, "y": y, "z": 0.0},
        "timestamp_ns": ns,
        "source": "s",
    }


def test_make_svg_too_few_points():
    """Fewer than two valid points yields (None, 0)."""
    svg, count = generate.make_svg("s", [_record(0, 0, 0)])
    assert svg is None
    assert count == 0


def test_make_svg_valid_output():
    """Two distinct points produce non-empty SVG with markers."""
    records = [_record(0.0, 0.0, 0), _record(1.0, 1.0, 1)]
    svg, count = generate.make_svg("group/SKG Start - 1/raw-robot--loc", records)
    assert count == 2
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    assert svg.count("<circle") == 2
    # The illegal '--' must never appear inside an XML comment.
    assert "<!--" not in svg


def test_make_svg_ignores_bad_positions():
    """Records missing x/y are skipped without crashing."""
    records = [
        _record(0.0, 0.0, 0),
        {"position": {}, "timestamp_ns": 1, "source": "s"},
        _record(2.0, 2.0, 2),
    ]
    svg, count = generate.make_svg("s", records)
    assert count == 2
    assert svg is not None


# --------------------------------------------------------------------------- #
# resolve_params / parse_args
# --------------------------------------------------------------------------- #
def test_parse_args_defaults(tmp_path):
    """Defaults are populated from the help formatter."""
    args = generate.parse_args([])
    assert args.verbose is False
    assert args.cprofile is False
    assert args.params == "parameters.json"


def test_resolve_params_uses_defaults(tmp_path, monkeypatch):
    """Missing parameter file falls back to built-in defaults."""
    monkeypatch.chdir(tmp_path)
    args = generate.parse_args(["--params", "does-not-exist.json"])
    params = generate.resolve_params(args)
    assert params["images_file"] == "mission_images.json"
    assert params["svg_directory"] == "svg"


def test_resolve_params_cli_overrides(tmp_path, monkeypatch):
    """CLI overrides win over file values."""
    monkeypatch.chdir(tmp_path)
    pf = tmp_path / "p.json"
    pf.write_text(json.dumps({"images_file": "from-file.json"}))
    args = generate.parse_args(["--params", str(pf), "--images", "cli.json"])
    params = generate.resolve_params(args)
    assert params["images_file"] == "cli.json"
