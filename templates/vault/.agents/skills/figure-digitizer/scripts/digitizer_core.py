"""figure-digitizer core: evidence-bound extraction of chart values from rasters.

Design contract (mirrors `SKILL.md`):

* every measurement is taken on the **original raster** (SHA-256 + width + height),
* every value comes from a **verified axis calibration**, never from a preview,
  thumbnail, enlarged crop or a previously rendered overlay,
* anything the pixels cannot support stays `low_confidence` / `not_extracted`,
* numeric output is authorised only when the figure spec is marked `verified`
  and all quality gates pass.

Pure Python + NumPy + Pillow. No network access, no OCR service, no model call.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
from PIL import Image, ImageDraw

SPEC_SCHEMA = "agentero.figure-digitizer/spec/v1"
PREFLIGHT_SCHEMA = "agentero.figure-digitizer/preflight/v1"
REPORT_SCHEMA = "agentero.figure-digitizer/report/v1"
TOOL_VERSION = "0.1.0"

# Status vocabulary shared with SKILL.md and the report contract.
STATUS_EXTRACTED = "extracted"
STATUS_PARTIAL = "partial_visible"
STATUS_LOW = "low_confidence"
STATUS_NOT_EXTRACTED = "not_extracted"
STATUS_REFUSED = "refused"

# Route maturity. This skill ships candidate routes on purpose: a route is
# promoted to `stable` only after held-out figures and a matched comparison
# against an independent tool (see references/chart-routes.md).
MATURITY_CANDIDATE = "candidate"
MATURITY_STABLE = "stable"
MATURITY_ASSISTED = "assisted"

ROUTES: list[dict[str, Any]] = [
    {
        "id": "line",
        "chart_types": ["line", "timeseries", "time_series", "curve", "dose_response", "roc", "survival"],
        "maturity": MATURITY_CANDIDATE,
        "grammar": "color-separated continuous polyline or marker-line series on a calibrated Cartesian frame",
        "refuses": [
            "series sharing one stroke color",
            "legend or annotation strokes inside the plot bounds",
            "areas of a stroke hidden by markers, bars or other series",
            "non-Cartesian coordinates (polar, ternary, radar)",
        ],
    },
    {
        "id": "histogram",
        "chart_types": ["histogram", "density", "distribution"],
        "maturity": MATURITY_CANDIDATE,
        "grammar": "contiguous color-separated columns sitting on a common baseline",
        "refuses": [
            "columns that touch without a separable gap",
            "overlapping semi-transparent bars",
            "a baseline that is not visible enough to locate",
        ],
    },
    {
        "id": "bar",
        "chart_types": ["bar", "bars", "column", "grouped_bar", "stacked_bar"],
        "maturity": MATURITY_CANDIDATE,
        "grammar": "grouped, stacked or simple bars anchored on a visible baseline",
        "refuses": [
            "a legend swatch that cannot be separated from plotted geometry",
            "fully occluded segments",
            "3D or perspective bar geometry",
        ],
    },
    {
        "id": "scatter",
        "chart_types": ["scatter", "scatterplot", "points", "dot"],
        "maturity": MATURITY_CANDIDATE,
        "grammar": "compact filled markers on a calibrated Cartesian frame",
        "refuses": [
            "marker swarms that touch into one blob",
            "hollow or gradient-filled markers",
            "markers hidden under bars, curves or annotations",
            "bubble sizes carrying a third variable",
        ],
    },
]

_ROUTE_BY_ID = {route["id"]: route for route in ROUTES}


def route_table() -> list[dict[str, Any]]:
    """Machine-readable route registry (the authoritative capability list)."""
    return [dict(route) for route in ROUTES]


def resolve_route(chart_type: str | None) -> dict[str, Any] | None:
    """Map a chart type onto a registered route; unknown types return None."""
    if not chart_type:
        return None
    normalized = chart_type.strip().lower().replace("-", "_").replace(" ", "_")
    for route in ROUTES:
        if normalized in route["chart_types"]:
            return dict(route)
    return None


# --------------------------------------------------------------------------
# source identity
# --------------------------------------------------------------------------


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_color(value: str | Sequence[int]) -> tuple[int, int, int]:
    """Accept `#rrggbb`, `rrggbb`, `rgb(r,g,b)` or a 3-sequence."""
    if isinstance(value, (list, tuple)):
        if len(value) != 3:
            raise ValueError(f"color needs 3 channels, got {value!r}")
        return tuple(int(v) for v in value)  # type: ignore[return-value]
    text = str(value).strip().lower()
    if text.startswith("rgb(") and text.endswith(")"):
        parts = [int(p) for p in text[4:-1].replace(" ", "").split(",")]
        if len(parts) != 3:
            raise ValueError(f"color needs 3 channels: {value!r}")
        return tuple(parts)  # type: ignore[return-value]
    text = text.lstrip("#")
    if len(text) != 6:
        raise ValueError(f"color must be #rrggbb, got {value!r}")
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def format_color(rgb: Sequence[int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(int(c) for c in rgb))


@dataclass
class RasterSource:
    """The immutable measurement canvas."""

    path: str
    sha256: str
    width: int
    height: int
    array: np.ndarray = field(repr=False)

    @classmethod
    def load(cls, path: str | Path) -> "RasterSource":
        path = Path(path)
        with Image.open(path) as handle:
            image = handle.convert("RGB")
        array = np.asarray(image, dtype=np.uint8)
        height, width = array.shape[:2]
        return cls(
            path=str(path),
            sha256=sha256_file(path),
            width=int(width),
            height=int(height),
            array=array,
        )

    def identity(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
        }

    def check_identity(self, declared: dict[str, Any] | None) -> list[str]:
        """Refuse a spec whose source contract does not match the loaded file."""
        problems: list[str] = []
        if not declared:
            return ["spec.source is missing; record the original raster identity first"]
        sha = str(declared.get("sha256") or "")
        if sha and sha != self.sha256:
            problems.append(
                f"source sha256 mismatch: spec {sha[:12]}… vs file {self.sha256[:12]}…"
            )
        for key, actual in (("width", self.width), ("height", self.height)):
            declared_value = declared.get(key)
            if declared_value in (None, ""):
                continue
            if int(declared_value) != actual:
                problems.append(
                    f"source {key} mismatch: spec {declared_value} vs file {actual}"
                )
        return problems


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------


@dataclass
class Axis:
    """Two-or-more-anchor axis calibration.

    `anchors` are `(pixel, value)` pairs read off verified tick marks. The fit
    is affine in transformed value space, so `log10` axes calibrate
    `log10(value)` against pixels instead of pretending to be linear.
    """

    name: str
    scale: str
    anchors: list[tuple[float, float]]

    def __post_init__(self) -> None:
        if self.scale not in ("linear", "log10"):
            raise ValueError(f"{self.name}: scale must be 'linear' or 'log10'")
        if len(self.anchors) < 2:
            raise ValueError(f"{self.name}: at least two anchors are required")
        pixels = [float(a[0]) for a in self.anchors]
        if len(set(pixels)) < 2:
            raise ValueError(f"{self.name}: anchors must sit at different pixels")
        if self.scale == "log10" and any(float(a[1]) <= 0 for a in self.anchors):
            raise ValueError(f"{self.name}: log10 anchors must be positive")

    def _transform(self, value: float) -> float:
        return math.log10(value) if self.scale == "log10" else float(value)

    def _inverse(self, transformed: float) -> float:
        return float(10.0**transformed) if self.scale == "log10" else float(transformed)

    def _fit(self) -> tuple[float, float]:
        pixels = np.array([float(a[0]) for a in self.anchors], dtype=float)
        values = np.array([self._transform(float(a[1])) for a in self.anchors], dtype=float)
        design = np.vstack([pixels, np.ones_like(pixels)]).T
        slope, intercept = np.linalg.lstsq(design, values, rcond=None)[0]
        if abs(float(slope)) < 1e-12:
            raise ValueError(f"{self.name}: degenerate calibration (zero slope)")
        return float(slope), float(intercept)

    def to_value(self, pixel: float) -> float:
        slope, intercept = self._fit()
        return self._inverse(slope * float(pixel) + intercept)

    def to_pixel(self, value: float) -> float:
        slope, intercept = self._fit()
        return (self._transform(float(value)) - intercept) / slope

    def residual(self) -> dict[str, float]:
        """Agreement of the fit with every anchor; large residuals mean bad anchors."""
        slope, intercept = self._fit()
        transformed = [abs(slope * float(p) + intercept - self._transform(float(v)))
                       for p, v in self.anchors]
        data = [abs(self.to_value(p) - float(v)) for p, v in self.anchors]
        return {
            "max_transformed_residual": max(transformed),
            "max_data_residual": max(data),
        }

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scale": self.scale,
            "anchors": [[float(p), float(v)] for p, v in self.anchors],
            **self.residual(),
        }


@dataclass
class Calibration:
    x: Axis
    y: Axis

    def to_data(self, px: float, py: float) -> tuple[float, float]:
        return self.x.to_value(px), self.y.to_value(py)

    def to_pixel(self, x: float, y: float) -> tuple[float, float]:
        return self.x.to_pixel(x), self.y.to_pixel(y)

    def describe(self) -> dict[str, Any]:
        return {"x": self.x.describe(), "y": self.y.describe()}


def axis_from_spec(payload: dict[str, Any], name: str) -> Axis:
    if not isinstance(payload, dict):
        raise ValueError(f"calibration.{name} is missing")
    scale = str(payload.get("scale") or "linear")
    anchors = payload.get("anchors")
    if not isinstance(anchors, list):
        raise ValueError(f"calibration.{name}.anchors must be a list")
    pairs: list[tuple[float, float]] = []
    for anchor in anchors:
        if not isinstance(anchor, (list, tuple)) or len(anchor) != 2:
            raise ValueError(
                f"calibration.{name}.anchors entries must be [pixel, value], got {anchor!r}"
            )
        pairs.append((float(anchor[0]), float(anchor[1])))
    return Axis(name=name, scale=scale, anchors=pairs)


def calibration_from_spec(spec: dict[str, Any]) -> Calibration:
    calibration = spec.get("calibration") or {}
    return Calibration(
        x=axis_from_spec(calibration.get("x"), "x"),
        y=axis_from_spec(calibration.get("y"), "y"),
    )


# --------------------------------------------------------------------------
# raster primitives
# --------------------------------------------------------------------------


def clip_bounds(bounds: Sequence[float], shape: Sequence[int]) -> tuple[int, int, int, int]:
    height, width = int(shape[0]), int(shape[1])
    left, top, right, bottom = (int(round(float(v))) for v in bounds)
    left = max(0, min(left, width - 1))
    top = max(0, min(top, height - 1))
    right = max(left + 1, min(right, width))
    bottom = max(top + 1, min(bottom, height))
    return left, top, right, bottom


def color_mask(array: np.ndarray, rgb: Sequence[int], tolerance: float) -> np.ndarray:
    """Boolean mask of pixels within `tolerance` Euclidean RGB distance."""
    target = np.array([int(c) for c in rgb], dtype=np.int16)
    diff = array.astype(np.int16) - target
    distance = np.sqrt(np.sum(diff.astype(np.float32) ** 2, axis=2))
    return distance <= float(tolerance)


def mask_bbox(mask: np.ndarray) -> list[int] | None:
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        return None
    return [int(cols.min()), int(rows.min()), int(cols.max()) + 1, int(rows.max()) + 1]


def _runs(indices: np.ndarray) -> list[np.ndarray]:
    """Split sorted indices into contiguous runs."""
    if indices.size == 0:
        return []
    breaks = np.where(np.diff(indices) > 1)[0]
    return np.split(indices, breaks + 1)


def column_segments(
    mask: np.ndarray,
    bounds: Sequence[float],
    min_height: int = 2,
    max_gap: int = 1,
    min_width: int = 1,
) -> list[dict[str, Any]]:
    """Group mask columns into vertical segments (bars / histogram bins)."""
    left, top, right, bottom = clip_bounds(bounds, mask.shape)
    sub = mask[top:bottom, left:right]
    heights = sub.sum(axis=0)
    columns = np.where(heights >= max(1, int(min_height)))[0]
    if columns.size == 0:
        return []
    groups = _runs(columns)
    segments: list[dict[str, Any]] = []
    for group in groups:
        if group.size == 0:
            continue
        x0, x1 = int(group[0]), int(group[-1])
        if x1 - x0 + 1 < max(1, int(min_width)):
            continue
        block = sub[:, x0 : x1 + 1]
        rows = np.where(block.any(axis=1))[0]
        if rows.size == 0:
            continue
        segments.append(
            {
                "x0": int(left + x0),
                "x1": int(left + x1) + 1,
                "center_x": float(left + (x0 + x1) / 2.0),
                "top": float(top + rows.min()),
                "bottom": float(top + rows.max()) + 1,
                "width": int(x1 - x0 + 1),
                "mask_pixels": int(block.sum()),
            }
        )
    return segments


def merge_close_segments(
    segments: list[dict[str, Any]], max_gap: float
) -> list[dict[str, Any]]:
    """Merge segments whose horizontal gap is at most `max_gap` pixels."""
    if max_gap <= 0 or len(segments) < 2:
        return segments
    ordered = sorted(segments, key=lambda s: s["x0"])
    merged: list[dict[str, Any]] = [dict(ordered[0])]
    for segment in ordered[1:]:
        last = merged[-1]
        if segment["x0"] - last["x1"] <= max_gap:
            pixels = last["mask_pixels"] + segment["mask_pixels"]
            last["x1"] = segment["x1"]
            last["top"] = min(last["top"], segment["top"])
            last["bottom"] = max(last["bottom"], segment["bottom"])
            last["width"] = last["x1"] - last["x0"]
            last["mask_pixels"] = pixels
            last["center_x"] = (last["x0"] + last["x1"]) / 2.0
        else:
            merged.append(dict(segment))
    return merged


def trace_columns(
    mask: np.ndarray,
    bounds: Sequence[float],
    max_step: float | None = None,
    min_support: int = 1,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Follow a stroke left-to-right; empty columns become explicit gaps."""
    left, top, right, bottom = clip_bounds(bounds, mask.shape)
    sub = mask[top:bottom, left:right]
    points: list[dict[str, Any]] = []
    gaps: list[int] = []
    previous_y: float | None = None
    for column in range(sub.shape[1]):
        rows = np.where(sub[:, column])[0]
        if rows.size < max(1, int(min_support)):
            gaps.append(int(left + column))
            continue
        if max_step is not None and previous_y is not None:
            candidates = _runs(rows)
            chosen = min(candidates, key=lambda run: abs(float(np.median(run)) - previous_y))
        else:
            chosen = rows
        y = float(top + np.median(chosen))
        points.append(
            {
                "x": int(left + column),
                "y": y,
                "support": int(chosen.size),
            }
        )
        previous_y = y
    return points, gaps


def cluster_points(
    mask: np.ndarray,
    bounds: Sequence[float],
    min_size: int = 4,
    max_size: int = 400,
) -> list[dict[str, Any]]:
    """8-connected components of the mask, sized and screened as markers."""
    left, top, right, bottom = clip_bounds(bounds, mask.shape)
    sub = np.ascontiguousarray(mask[top:bottom, left:right])
    height, width = sub.shape
    visited = np.zeros_like(sub, dtype=bool)
    offsets = (
        (-1, -1), (-1, 0), (-1, 1),
        (0, -1), (0, 1),
        (1, -1), (1, 0), (1, 1),
    )
    clusters: list[dict[str, Any]] = []
    rows, cols = np.nonzero(sub)
    for row, col in zip(rows.tolist(), cols.tolist()):
        if visited[row, col]:
            continue
        stack = [(row, col)]
        visited[row, col] = True
        members: list[tuple[int, int]] = []
        while stack:
            y, x = stack.pop()
            members.append((y, x))
            for dy, dx in offsets:
                ny, nx = y + dy, x + dx
                if 0 <= ny < height and 0 <= nx < width and sub[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    stack.append((ny, nx))
        ys = np.array([m[0] for m in members], dtype=float)
        xs = np.array([m[1] for m in members], dtype=float)
        size = int(ys.size)
        box_w = float(xs.max() - xs.min() + 1)
        box_h = float(ys.max() - ys.min() + 1)
        if size < max(1, int(min_size)):
            status = "rejected_too_small"
        elif size > max(1, int(max_size)):
            status = "rejected_too_large"
        else:
            status = "accepted"
        clusters.append(
            {
                "center_x": float(left + xs.mean()),
                "center_y": float(top + ys.mean()),
                "size_px": size,
                "bbox": [float(left + xs.min()), float(top + ys.min()), box_w, box_h],
                "fill_ratio": float(size / max(1.0, box_w * box_h)),
                "status": status,
            }
        )
    clusters.sort(key=lambda c: (c["center_x"], c["center_y"]))
    return clusters


# --------------------------------------------------------------------------
# panel crops
# --------------------------------------------------------------------------


def crop_region(
    source: RasterSource,
    bbox: Sequence[float],
    out_path: str | Path,
    bounds_mode: str = "normalized",
    review_scale: float = 1.0,
) -> dict[str, Any]:
    """Cut a panel out of a page raster without resampling.

    Agentero's layout index stores `bbox` normalized to the page (0-1), which is
    exactly what this maps onto pixels. The crop keeps the page's own resolution,
    so a measurement taken on it is still an original-pixel measurement — but it
    is a **derived** raster: its parent identity, bbox and offset are recorded so
    the evidence chain stays unbroken.

    `review_scale` produces a nearest-neighbour enlargement for human review. An
    enlarged copy is not a measurement surface, and is flagged as such.
    """
    if bounds_mode not in ("normalized", "pixels"):
        raise ValueError("bounds_mode must be 'normalized' or 'pixels'")
    if len(bbox) != 4:
        raise ValueError("bbox must be [x, y, width, height]")
    if bounds_mode == "normalized":
        pixels = [
            float(bbox[0]) * source.width,
            float(bbox[1]) * source.height,
            float(bbox[2]) * source.width,
            float(bbox[3]) * source.height,
        ]
    else:
        pixels = [float(v) for v in bbox]
    if pixels[2] <= 0 or pixels[3] <= 0:
        raise ValueError(f"bbox has no area: {bbox!r}")
    left = max(0, int(math.floor(pixels[0])))
    top = max(0, int(math.floor(pixels[1])))
    right = min(source.width, int(math.ceil(pixels[0] + pixels[2])))
    bottom = min(source.height, int(math.ceil(pixels[1] + pixels[3])))
    if right - left < 2 or bottom - top < 2:
        raise ValueError("bbox falls outside the raster or is smaller than 2x2 pixels")
    cropped = source.array[top:bottom, left:right]
    image = Image.fromarray(cropped).convert("RGB")
    measurable = abs(float(review_scale) - 1.0) < 1e-9
    if not measurable:
        scale = float(review_scale)
        image = image.resize(
            (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale)))),
            Image.NEAREST,
        )
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target)
    record = {
        "schema": "agentero.figure-digitizer/crop/v1",
        "created_at": utc_now(),
        "parent": source.identity(),
        "bbox": [float(v) for v in bbox],
        "bbox_mode": bounds_mode,
        "pixel_rect": [left, top, right, bottom],
        "offset": [left, top],
        "review_scale": float(review_scale),
        "measurable": measurable,
        "crop": {
            "path": str(target),
            "sha256": sha256_file(target),
            "width": int(image.width),
            "height": int(image.height),
        },
        "notes": [
            "Measure on this crop only while `measurable` is true.",
            "An enlarged review copy carries no new pixel information.",
        ],
    }
    write_json(target.with_suffix(target.suffix + ".crop.json"), record)
    return record


# --------------------------------------------------------------------------
# evidence rendering
# --------------------------------------------------------------------------


def render_overlay(
    source: RasterSource,
    out_path: str | Path,
    plot_bounds: Sequence[float] | None = None,
    anchors: Sequence[tuple[float, float, str]] = (),
    accepted: Sequence[tuple[float, float]] = (),
    rejected: Sequence[tuple[float, float]] = (),
    segments: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Draw the accepted / rejected evidence back onto the original raster."""
    canvas = Image.fromarray(source.array).convert("RGB")
    draw = ImageDraw.Draw(canvas)
    if plot_bounds:
        left, top, right, bottom = (float(v) for v in plot_bounds)
        draw.rectangle([left, top, right - 1, bottom - 1], outline=(0, 102, 255), width=2)
    for segment in segments:
        draw.rectangle(
            [segment["x0"], segment["top"], segment["x1"] - 1, segment["bottom"] - 1],
            outline=(255, 0, 255),
            width=2,
        )
    for px, py, _label in anchors:
        draw.line([px - 6, py, px + 6, py], fill=(0, 153, 0), width=2)
        draw.line([px, py - 6, px, py + 6], fill=(0, 153, 0), width=2)
    for px, py in accepted:
        draw.ellipse([px - 4, py - 4, px + 4, py + 4], outline=(255, 0, 255), width=2)
    for px, py in rejected:
        draw.line([px - 5, py - 5, px + 5, py + 5], fill=(255, 128, 0), width=2)
        draw.line([px - 5, py + 5, px + 5, py - 5], fill=(255, 128, 0), width=2)
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path)
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "accepted_marks": len(accepted),
        "rejected_marks": len(rejected),
        "segments": len(segments),
    }


def render_recreated(
    source: RasterSource,
    out_path: str | Path,
    plot_bounds: Sequence[float],
    calibration: Calibration,
    points: Sequence[tuple[float, float]],
    bars: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Replay extracted values through the calibration on a same-size canvas.

    This is a structural check: a value that does not land where it was measured
    means the calibration and the extraction disagree. It is not a redraw of the
    published figure and carries no styling evidence.
    """
    canvas = Image.new("RGB", (source.width, source.height), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    left, top, right, bottom = (float(v) for v in plot_bounds)
    draw.rectangle([left, top, right - 1, bottom - 1], outline=(0, 0, 0), width=1)
    for bar in bars:
        px_left, px_right = calibration.x.to_pixel(bar["x0"]), calibration.x.to_pixel(bar["x1"])
        px_top = calibration.y.to_pixel(bar["value"])
        draw.rectangle(
            [min(px_left, px_right), px_top, max(px_left, px_right), bottom - 1],
            outline=(120, 120, 120),
            width=1,
        )
    for x, y in points:
        px, py = calibration.to_pixel(x, y)
        draw.ellipse([px - 2, py - 2, px + 2, py + 2], fill=(200, 0, 0))
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path)
    return {"path": str(path), "canvas": [source.width, source.height],
            "points": len(points), "bars": len(bars)}


# --------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------


def write_csv(path: str | Path, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(header)]
    for row in rows:
        cells = []
        for cell in row:
            text = "" if cell is None else str(cell)
            if any(ch in text for ch in ',"\n'):
                text = '"' + text.replace('"', '""') + '"'
            cells.append(text)
        lines.append(",".join(cells))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(target)


def write_json(path: str | Path, payload: Any) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return str(target)


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------


def _dominant_colors(array: np.ndarray, limit: int = 8) -> list[dict[str, Any]]:
    """Most common colors, reported as the mean of each 16-step bucket.

    Bucketing keeps this cheap on large rasters; averaging inside the bucket
    keeps the reported value honest instead of snapping to a bucket centre.
    """
    flat = array.reshape(-1, 3).astype(np.int64)
    buckets = (flat[:, 0] // 16) * 256 + (flat[:, 1] // 16) * 16 + (flat[:, 2] // 16)
    size = int(16 * 16 * 16)
    counts = np.bincount(buckets, minlength=size)
    channel_sums = [
        np.bincount(buckets, weights=flat[:, channel], minlength=size) for channel in range(3)
    ]
    order = np.argsort(counts)[::-1][:limit]
    total = float(flat.shape[0])
    out: list[dict[str, Any]] = []
    for bucket in order:
        count = int(counts[bucket])
        if count == 0:
            continue
        mean = [float(channel_sums[channel][bucket] / count) for channel in range(3)]
        out.append({
            "color": format_color([int(round(value)) for value in mean]),
            "share": float(count / total),
        })
    return out


def preflight(source: RasterSource, chart_type: str | None = None) -> dict[str, Any]:
    """Record input composition and propose a route. Never authorises values."""
    colors = _dominant_colors(source.array)
    background = colors[0]["color"] if colors else "#ffffff"
    background_rgb = parse_color(background)
    background_mask = color_mask(source.array, background_rgb, 24)
    content_bbox = mask_bbox(~background_mask)
    route = resolve_route(chart_type)
    notes: list[str] = []
    if route is None:
        status = "needs_chart_type_confirmation"
        notes.append(
            "No chart type confirmed yet. Inspect the figure with the user, then set "
            "figure.chart_type in the spec — a proposed route never authorises values."
        )
    else:
        status = "proposed_route"
        notes.append(
            f"Proposed route `{route['id']}` ({route['maturity']}). Confirm the panel, "
            "axes and target series before filling the spec."
        )
    if not content_bbox:
        notes.append("The raster has no content distinct from its background.")
    return {
        "schema": PREFLIGHT_SCHEMA,
        "tool_version": TOOL_VERSION,
        "created_at": utc_now(),
        "input": source.identity(),
        "content": {
            "background": background,
            "content_bbox": content_bbox,
            "dominant_colors": colors,
        },
        "proposal": {
            "chart_type": chart_type,
            "route": route["id"] if route else None,
            "maturity": route["maturity"] if route else None,
            "grammar": route["grammar"] if route else None,
            "refuses": route["refuses"] if route else [],
            "status": status,
        },
        "notes": notes,
        "numeric_output_authorized": False,
    }


def spec_template(source: RasterSource, chart_type: str | None = None) -> dict[str, Any]:
    route = resolve_route(chart_type)
    return {
        "schema": SPEC_SCHEMA,
        "source": {**source.identity(), "sha256": source.sha256},
        "figure": {
            "paper": "",
            "figure_id": "",
            "page": None,
            "panel": "",
            "chart_type": chart_type,
            "route": route["id"] if route else None,
            "source_crop": None,
            "verified": False,
            "verified_by": "",
        },
        "plot_bounds": None,
        "calibration": {
            "x": {"scale": "linear", "anchors": [], "verified": False},
            "y": {"scale": "linear", "anchors": [], "verified": False},
        },
        "series": [],
        "options": {
            "min_height_px": 2,
            "max_gap_px": 1,
            "min_component_px": 6,
            "max_component_px": 400,
            "max_step_px": None,
            "exclude_regions": [],
            "baseline_value": None,
        },
    }


def validate_spec(spec: dict[str, Any]) -> list[str]:
    """Static spec validation. Returns human-readable problems ([] = valid)."""
    problems: list[str] = []
    if not isinstance(spec, dict):
        return ["spec must be a JSON object"]
    if spec.get("schema") != SPEC_SCHEMA:
        problems.append(f"spec.schema must be {SPEC_SCHEMA!r}")
    source = spec.get("source")
    if not isinstance(source, dict) or not source.get("sha256"):
        problems.append("spec.source.sha256 is required (original raster identity)")
    figure = spec.get("figure")
    if not isinstance(figure, dict):
        problems.append("spec.figure is required")
        figure = {}
    chart_type = figure.get("chart_type")
    route_id = figure.get("route")
    route = _ROUTE_BY_ID.get(str(route_id)) if route_id else resolve_route(chart_type)
    if route is None:
        problems.append(
            "no registered route: set figure.chart_type to a type listed by `routes`"
        )
    elif route_id and route_id not in _ROUTE_BY_ID:
        problems.append(f"unknown figure.route {route_id!r}")
    elif chart_type and route and str(chart_type).lower() not in route["chart_types"]:
        problems.append(
            f"figure.chart_type {chart_type!r} does not belong to route {route['id']!r}"
        )
    bounds = spec.get("plot_bounds")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        problems.append("plot_bounds must be [left, top, right, bottom] in original pixels")
    elif not (float(bounds[2]) > float(bounds[0]) and float(bounds[3]) > float(bounds[1])):
        problems.append("plot_bounds must satisfy right > left and bottom > top")
    calibration = spec.get("calibration")
    if not isinstance(calibration, dict):
        problems.append("calibration is required")
    else:
        for axis in ("x", "y"):
            axis_payload = calibration.get(axis)
            if not isinstance(axis_payload, dict):
                problems.append(f"calibration.{axis} is required")
                continue
            anchors = axis_payload.get("anchors")
            if not isinstance(anchors, list) or len(anchors) < 2:
                problems.append(f"calibration.{axis}.anchors needs at least two [pixel, value] pairs")
            if not axis_payload.get("verified"):
                problems.append(
                    f"calibration.{axis}.verified must be true once the anchors are checked against the pixels"
                )
    if route and route["id"] in ("line", "bar", "scatter", "histogram"):
        series = spec.get("series")
        if not isinstance(series, list) or not series:
            problems.append("series is required: one entry per stroke/bar/marker colour")
        else:
            for index, entry in enumerate(series):
                if not isinstance(entry, dict):
                    problems.append(f"series[{index}] must be an object")
                    continue
                if not entry.get("color"):
                    problems.append(f"series[{index}].color is required")
                else:
                    try:
                        parse_color(entry["color"])
                    except ValueError as error:
                        problems.append(f"series[{index}].color invalid: {error}")
    if not figure.get("verified"):
        problems.append(
            "figure.verified must be true: confirm panel, axes and target series with the "
            "user before any numeric output"
        )
    return problems


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def _series_color(entry: dict[str, Any]) -> tuple[int, int, int]:
    return parse_color(entry["color"])


def _tolerance(entry: dict[str, Any], default: float = 30.0) -> float:
    value = entry.get("tolerance")
    return float(default if value in (None, "") else value)


def _excluded(mask: np.ndarray, regions: Sequence[Sequence[float]]) -> np.ndarray:
    if not regions:
        return mask
    cleaned = mask.copy()
    for region in regions:
        left, top, right, bottom = clip_bounds(region, mask.shape)
        cleaned[top:bottom, left:right] = False
    return cleaned


def _plot_mask(array: np.ndarray, bounds: Sequence[float]) -> np.ndarray:
    left, top, right, bottom = clip_bounds(bounds, array.shape)
    mask = np.zeros(array.shape[:2], dtype=bool)
    mask[top:bottom, left:right] = True
    return mask


def _gate(
    route: dict[str, Any],
    figure_verified: bool,
    calibration: Calibration,
    residual_limit: float,
    diagnostics: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Quality gates: only these authorise numeric output."""
    blockers: list[str] = []
    if not figure_verified:
        blockers.append("figure.verified is false — the spec was never confirmed")
    for axis in (calibration.x, calibration.y):
        residual = axis.residual()["max_data_residual"]
        span = abs(float(axis.anchors[-1][1]) - float(axis.anchors[0][1])) or 1.0
        if residual > residual_limit * span:
            blockers.append(
                f"{axis.name} anchors disagree by {residual:.4g} (>{residual_limit:.0%} of the axis span)"
            )
    if diagnostics.get("unresolved_conflicts"):
        blockers.append(
            f"{diagnostics['unresolved_conflicts']} ambiguous mark(s) need review before authorisation"
        )
    if route["maturity"] == MATURITY_ASSISTED:
        blockers.append("assisted routes never authorise numeric output on their own")
    return (not blockers), blockers


def _report_shell(
    source: RasterSource,
    spec: dict[str, Any],
    route: dict[str, Any],
    calibration: Calibration,
) -> dict[str, Any]:
    figure = spec.get("figure") or {}
    return {
        "schema": REPORT_SCHEMA,
        "tool_version": TOOL_VERSION,
        "created_at": utc_now(),
        "source": source.identity(),
        "figure": {
            "paper": figure.get("paper") or "",
            "figure_id": figure.get("figure_id") or "",
            "page": figure.get("page"),
            "panel": figure.get("panel") or "",
            "chart_type": figure.get("chart_type"),
            "source_crop": figure.get("source_crop"),
            "verified": bool(figure.get("verified")),
            "verified_by": figure.get("verified_by") or "",
        },
        "route": {"id": route["id"], "maturity": route["maturity"]},
        "plot_bounds": [float(v) for v in spec["plot_bounds"]],
        "calibration": calibration.describe(),
        "series": [],
        "diagnostics": {},
        "numeric_output_authorized": False,
        "authorization_blockers": [],
        "status": STATUS_NOT_EXTRACTED,
    }


def extract_line(
    source: RasterSource,
    spec: dict[str, Any],
    route: dict[str, Any],
    calibration: Calibration,
    output_dir: Path,
) -> dict[str, Any]:
    options = spec.get("options") or {}
    bounds = spec["plot_bounds"]
    report = _report_shell(source, spec, route, calibration)
    rows: list[list[Any]] = []
    accepted: list[tuple[float, float]] = []
    rejected: list[tuple[float, float]] = []
    measured = 0
    partial = 0
    for entry in spec["series"]:
        name = str(entry.get("name") or entry.get("color"))
        mask = _excluded(
            color_mask(source.array, _series_color(entry), _tolerance(entry)),
            options.get("exclude_regions") or [],
        )
        mask &= _plot_mask(source.array, bounds)
        points, gaps = trace_columns(
            mask,
            bounds,
            max_step=options.get("max_step_px"),
            min_support=int(entry.get("min_support", 1) or 1),
        )
        series_rows: list[list[Any]] = []
        for point in points:
            x_value, y_value = calibration.to_data(point["x"], point["y"])
            series_rows.append(
                [
                    name,
                    f"{x_value:.6g}",
                    f"{y_value:.6g}",
                    point["x"],
                    f"{point['y']:.2f}",
                    point["support"],
                    STATUS_EXTRACTED,
                ]
            )
            accepted.append((point["x"], point["y"]))
        measured += len(series_rows)
        series_status = STATUS_EXTRACTED
        if not series_rows:
            series_status = STATUS_NOT_EXTRACTED
            partial += 1
        elif gaps:
            series_status = STATUS_PARTIAL
            partial += 1
        report["series"].append(
            {
                "name": name,
                "color": format_color(_series_color(entry)),
                "status": series_status,
                "sample_count": len(series_rows),
                "gap_columns": len(gaps),
                "gap_ranges": _gap_ranges(gaps),
            }
        )
        rows.extend(series_rows)
    diagnostics = {
        "measured_columns": measured,
        "series_with_gaps_or_missing": partial,
        "unresolved_conflicts": 0,
        "gaps_are_missing_not_zero": True,
    }
    return _finish(
        source, spec, route, calibration, report, rows,
        header=["series", "x", "y", "pixel_x", "pixel_y", "support", "status"],
        diagnostics=diagnostics, accepted=accepted, rejected=rejected,
        output_dir=output_dir,
        recreate_points=[(float(row[1]), float(row[2])) for row in rows],
        extra_note="Empty columns are recorded as gaps; they are never interpolated.",
    )


def _gap_ranges(gaps: Sequence[int]) -> list[list[int]]:
    if not gaps:
        return []
    ordered = sorted(int(g) for g in gaps)
    ranges: list[list[int]] = [[ordered[0], ordered[0]]]
    for value in ordered[1:]:
        if value == ranges[-1][1] + 1:
            ranges[-1][1] = value
        else:
            ranges.append([value, value])
    return ranges


def extract_histogram(
    source: RasterSource,
    spec: dict[str, Any],
    route: dict[str, Any],
    calibration: Calibration,
    output_dir: Path,
) -> dict[str, Any]:
    options = spec.get("options") or {}
    bounds = spec["plot_bounds"]
    report = _report_shell(source, spec, route, calibration)
    rows: list[list[Any]] = []
    accepted: list[tuple[float, float]] = []
    segments_all: list[dict[str, Any]] = []
    for entry in spec["series"]:
        name = str(entry.get("name") or entry.get("color"))
        mask = _excluded(
            color_mask(source.array, _series_color(entry), _tolerance(entry)),
            options.get("exclude_regions") or [],
        )
        mask &= _plot_mask(source.array, bounds)
        segments = column_segments(
            mask,
            bounds,
            min_height=int(options.get("min_height_px", 2) or 2),
            max_gap=int(options.get("max_gap_px", 1) or 0),
        )
        segments = merge_close_segments(segments, float(options.get("merge_gap_px", 0) or 0))
        series_rows: list[list[Any]] = []
        for index, segment in enumerate(segments):
            top_value = calibration.y.to_value(segment["top"])
            left_value = calibration.x.to_value(segment["x0"])
            right_value = calibration.x.to_value(segment["x1"])
            series_rows.append(
                [
                    name,
                    index,
                    f"{min(left_value, right_value):.6g}",
                    f"{max(left_value, right_value):.6g}",
                    f"{top_value:.6g}",
                    int(segment["x0"]),
                    int(segment["x1"]),
                    f"{segment['top']:.2f}",
                    f"{segment['bottom']:.2f}",
                    STATUS_EXTRACTED,
                ]
            )
            accepted.append((segment["center_x"], segment["top"]))
            segments_all.append(segment)
        report["series"].append(
            {
                "name": name,
                "color": format_color(_series_color(entry)),
                "status": STATUS_EXTRACTED if series_rows else STATUS_NOT_EXTRACTED,
                "bin_count": len(series_rows),
            }
        )
        rows.extend(series_rows)
    diagnostics = {
        "measured_bins": len(rows),
        "baseline_is_visible": True,
        "values_are_visible_heights": True,
        "unresolved_conflicts": 0,
    }
    return _finish(
        source, spec, route, calibration, report, rows,
        header=[
            "series", "bin_index", "bin_start", "bin_end", "height",
            "pixel_left", "pixel_right", "pixel_top", "pixel_bottom", "status",
        ],
        diagnostics=diagnostics, accepted=accepted, rejected=[], segments=segments_all,
        output_dir=output_dir,
        recreate_bars=[
            {"x0": float(row[2]), "x1": float(row[3]), "value": float(row[4])} for row in rows
        ],
        extra_note="Heights are calibrated visible column tops, not the original observations.",
    )


def extract_bar(
    source: RasterSource,
    spec: dict[str, Any],
    route: dict[str, Any],
    calibration: Calibration,
    output_dir: Path,
) -> dict[str, Any]:
    options = spec.get("options") or {}
    bounds = spec["plot_bounds"]
    baseline_value = options.get("baseline_value")
    if baseline_value is None:
        raise ValueError(
            "options.baseline_value is required for the bar route: read the baseline off the value axis"
        )
    baseline_px = calibration.y.to_pixel(float(baseline_value))
    report = _report_shell(source, spec, route, calibration)
    rows: list[list[Any]] = []
    accepted: list[tuple[float, float]] = []
    segments_all: list[dict[str, Any]] = []
    for entry in spec["series"]:
        name = str(entry.get("name") or entry.get("color"))
        mask = _excluded(
            color_mask(source.array, _series_color(entry), _tolerance(entry)),
            options.get("exclude_regions") or [],
        )
        mask &= _plot_mask(source.array, bounds)
        segments = column_segments(
            mask,
            bounds,
            min_height=int(options.get("min_height_px", 2) or 2),
            max_gap=int(options.get("max_gap_px", 1) or 0),
        )
        segments = merge_close_segments(segments, float(options.get("merge_gap_px", 0) or 0))
        series_rows: list[list[Any]] = []
        for index, segment in enumerate(segments):
            status = STATUS_EXTRACTED
            if baseline_px >= segment["bottom"] and baseline_px <= segment["top"]:
                # The baseline is covered by plotted geometry: the bar's value is
                # still readable from its top, but the anchor itself is occluded.
                status = "occluded_by_overlay"
            value = calibration.y.to_value(segment["top"])
            series_rows.append(
                [
                    name,
                    index,
                    f"{value:.6g}",
                    int(segment["x0"]),
                    int(segment["x1"]),
                    f"{segment['top']:.2f}",
                    f"{segment['bottom']:.2f}",
                    status,
                ]
            )
            accepted.append((segment["center_x"], segment["top"]))
            segments_all.append(segment)
        report["series"].append(
            {
                "name": name,
                "color": format_color(_series_color(entry)),
                "status": STATUS_EXTRACTED if series_rows else STATUS_NOT_EXTRACTED,
                "bar_count": len(series_rows),
            }
        )
        rows.extend(series_rows)
    diagnostics = {
        "measured_bars": len(rows),
        "baseline_value": float(baseline_value),
        "baseline_pixel": float(baseline_px),
        "values_are_visible_rectangle_tops": True,
        "unresolved_conflicts": 0,
    }
    return _finish(
        source, spec, route, calibration, report, rows,
        header=[
            "series", "bar_index", "value", "pixel_left", "pixel_right",
            "pixel_top", "pixel_bottom", "status",
        ],
        diagnostics=diagnostics, accepted=accepted, rejected=[], segments=segments_all,
        output_dir=output_dir,
        recreate_bars=[
            {
                "x0": calibration.x.to_value(row[3]),
                "x1": calibration.x.to_value(row[4]),
                "value": float(row[2]),
            }
            for row in rows
        ],
        extra_note="Values are visible rectangle tops against the declared baseline.",
    )


def extract_scatter(
    source: RasterSource,
    spec: dict[str, Any],
    route: dict[str, Any],
    calibration: Calibration,
    output_dir: Path,
) -> dict[str, Any]:
    options = spec.get("options") or {}
    bounds = spec["plot_bounds"]
    report = _report_shell(source, spec, route, calibration)
    rows: list[list[Any]] = []
    accepted: list[tuple[float, float]] = []
    rejected: list[tuple[float, float]] = []
    unresolved = 0
    for entry in spec["series"]:
        name = str(entry.get("name") or entry.get("color"))
        mask = _excluded(
            color_mask(source.array, _series_color(entry), _tolerance(entry)),
            options.get("exclude_regions") or [],
        )
        mask &= _plot_mask(source.array, bounds)
        clusters = cluster_points(
            mask,
            bounds,
            min_size=int(options.get("min_component_px", 6) or 1),
            max_size=int(options.get("max_component_px", 400) or 10**9),
        )
        series_rows: list[list[Any]] = []
        for cluster in clusters:
            if cluster["status"] != "accepted":
                rejected.append((cluster["center_x"], cluster["center_y"]))
                unresolved += 1
                continue
            x_value, y_value = calibration.to_data(cluster["center_x"], cluster["center_y"])
            series_rows.append(
                [
                    name,
                    f"{x_value:.6g}",
                    f"{y_value:.6g}",
                    f"{cluster['center_x']:.2f}",
                    f"{cluster['center_y']:.2f}",
                    cluster["size_px"],
                    f"{cluster['fill_ratio']:.2f}",
                    STATUS_EXTRACTED,
                ]
            )
            accepted.append((cluster["center_x"], cluster["center_y"]))
        report["series"].append(
            {
                "name": name,
                "color": format_color(_series_color(entry)),
                "status": STATUS_EXTRACTED if series_rows and not unresolved else (
                    STATUS_PARTIAL if series_rows else STATUS_NOT_EXTRACTED
                ),
                "point_count": len(series_rows),
                "rejected_components": len(clusters) - len(series_rows),
            }
        )
        rows.extend(series_rows)
    diagnostics = {
        "measured_points": len(rows),
        "rejected_components": len(rejected),
        "unresolved_conflicts": unresolved,
        "merged_components_are_not_split": True,
    }
    return _finish(
        source, spec, route, calibration, report, rows,
        header=["series", "x", "y", "pixel_x", "pixel_y", "size_px", "fill_ratio", "status"],
        diagnostics=diagnostics, accepted=accepted, rejected=rejected,
        output_dir=output_dir,
        recreate_points=[(float(row[1]), float(row[2])) for row in rows],
        extra_note=(
            "Merged or oversized components stay rejected; they are never split into "
            "invented points."
        ),
    )


EXTRACTORS = {
    "line": extract_line,
    "histogram": extract_histogram,
    "bar": extract_bar,
    "scatter": extract_scatter,
}


def _finish(
    source: RasterSource,
    spec: dict[str, Any],
    route: dict[str, Any],
    calibration: Calibration,
    report: dict[str, Any],
    rows: list[list[Any]],
    header: list[str],
    diagnostics: dict[str, Any],
    accepted: list[tuple[float, float]],
    rejected: list[tuple[float, float]],
    output_dir: Path,
    extra_note: str = "",
    segments: Sequence[dict[str, Any]] = (),
    recreate_points: Sequence[tuple[float, float]] = (),
    recreate_bars: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    figure = spec.get("figure") or {}
    authorized, blockers = _gate(
        route, bool(figure.get("verified")), calibration,
        float((spec.get("options") or {}).get("residual_limit", 0.01)),
        diagnostics,
    )
    report["diagnostics"] = diagnostics
    report["numeric_output_authorized"] = authorized
    report["authorization_blockers"] = blockers
    if blockers:
        report["status"] = STATUS_LOW if rows else STATUS_NOT_EXTRACTED
    elif not rows:
        report["status"] = STATUS_NOT_EXTRACTED
    elif any(entry["status"] != STATUS_EXTRACTED for entry in report["series"]):
        report["status"] = STATUS_PARTIAL
    else:
        report["status"] = STATUS_EXTRACTED
    report["notes"] = [note for note in (extra_note,) if note]

    output_dir = Path(output_dir)
    artifacts: dict[str, Any] = {}
    # The immutable extraction table is written before any review artifact, and
    # no later step rewrites it.
    csv_path = write_csv(output_dir / "data.csv", header, rows)
    artifacts["data_csv"] = csv_path
    overlay = render_overlay(
        source,
        output_dir / "overlay.png",
        plot_bounds=spec["plot_bounds"],
        anchors=_anchor_marks(calibration),
        accepted=accepted,
        rejected=rejected,
        segments=segments,
    )
    artifacts["overlay_png"] = overlay
    if recreate_points or recreate_bars:
        artifacts["recreated_png"] = render_recreated(
            source,
            output_dir / "recreated.png",
            spec["plot_bounds"],
            calibration,
            recreate_points,
            recreate_bars,
        )
    report["artifacts"] = artifacts
    report["row_count"] = len(rows)
    write_json(output_dir / "report.json", report)
    return report


def _anchor_marks(calibration: Calibration) -> list[tuple[float, float, str]]:
    marks: list[tuple[float, float, str]] = []
    x0, x1 = calibration.x.anchors[0], calibration.x.anchors[-1]
    y0, y1 = calibration.y.anchors[0], calibration.y.anchors[-1]
    for pixel, value in calibration.x.anchors:
        marks.append((float(pixel), float(y0[0]), f"x={value:g}"))
    for pixel, value in calibration.y.anchors:
        marks.append((float(x0[0]), float(pixel), f"y={value:g}"))
    return marks


def run_extraction(
    spec: dict[str, Any],
    source: RasterSource,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Validate, gate and run the registered extractor for a spec."""
    problems = validate_spec(spec)
    if problems:
        raise ValueError("spec validation failed:\n- " + "\n- ".join(problems))
    identity_problems = source.check_identity(spec.get("source"))
    if identity_problems:
        raise ValueError("source contract mismatch:\n- " + "\n- ".join(identity_problems))
    figure = spec["figure"]
    route = _ROUTE_BY_ID.get(str(figure.get("route") or "")) or resolve_route(figure.get("chart_type"))
    if route is None:
        raise ValueError(f"no registered route for chart type {figure.get('chart_type')!r}")
    if not figure.get("verified"):
        # Refuse before touching pixels: an unverified spec is not an extraction request.
        raise ValueError(
            "figure.verified is false — confirm the panel, axes and series with the user first"
        )
    calibration = calibration_from_spec(spec)
    extractor = EXTRACTORS[route["id"]]
    return extractor(source, spec, route, calibration, Path(output_dir))
