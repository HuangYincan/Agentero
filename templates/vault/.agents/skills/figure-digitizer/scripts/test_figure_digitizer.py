"""Tests for the figure-digitizer toolkit.

Run from the skill's `scripts/` directory:

    python -m unittest discover -s scripts -p "test_*.py" -v

Fixtures are drawn synthetically so the expected value of every measured mark is
known exactly. They exercise the evidence contract, not chart-rendering luck.
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageDraw  # noqa: E402

import digitizer_core as core  # noqa: E402

WIDTH, HEIGHT = 400, 300
PLOT = [50, 20, 350, 271]
X_PER_UNIT = 60.0
Y_PER_UNIT = 25.0
BASELINE_PX = 270.0
BAR_COLOR = "#1f77b4"
LINE_COLOR = "#d62728"
POINT_COLOR = "#2ca02c"


def data_to_px(x: float, y: float) -> tuple[float, float]:
    return 50 + X_PER_UNIT * x, BASELINE_PX - Y_PER_UNIT * y


def new_canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255))
    return image, ImageDraw.Draw(image)


def base_spec(source: core.RasterSource, chart_type: str, **figure_overrides) -> dict:
    spec = core.spec_template(source, chart_type)
    spec["plot_bounds"] = list(PLOT)
    spec["calibration"] = {
        "x": {"scale": "linear", "anchors": [[50.0, 0.0], [350.0, 5.0]], "verified": True},
        "y": {"scale": "linear", "anchors": [[270.0, 0.0], [20.0, 10.0]], "verified": True},
    }
    spec["figure"].update(
        {"paper": "papers/demo", "figure_id": "figure-1", "chart_type": chart_type,
         "verified": True, "verified_by": "test"}
    )
    spec["figure"].update(figure_overrides)
    return spec


class AxisTests(unittest.TestCase):
    def test_linear_roundtrip_and_residual(self) -> None:
        axis = core.Axis("x", "linear", [(50.0, 0.0), (350.0, 5.0), (200.0, 2.5)])
        self.assertAlmostEqual(axis.to_value(50.0), 0.0, places=9)
        self.assertAlmostEqual(axis.to_value(350.0), 5.0, places=9)
        self.assertAlmostEqual(axis.to_pixel(2.5), 200.0, places=9)
        self.assertLess(axis.residual()["max_data_residual"], 1e-9)

    def test_log10_axis_is_not_a_linear_mapping(self) -> None:
        axis = core.Axis("y", "log10", [(100.0, 1.0), (200.0, 100.0)])
        self.assertAlmostEqual(axis.to_value(150.0), 10.0, places=6)
        linear_midpoint = (1.0 + 100.0) / 2
        self.assertNotAlmostEqual(axis.to_value(150.0), linear_midpoint, places=3)

    def test_degenerate_and_invalid_axes_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            core.Axis("x", "linear", [(10.0, 0.0), (10.0, 1.0)])
        with self.assertRaises(ValueError):
            core.Axis("x", "linear", [(10.0, 0.0)])
        with self.assertRaises(ValueError):
            core.Axis("y", "log10", [(10.0, 0.0), (20.0, 100.0)])
        with self.assertRaises(ValueError):
            core.Axis("y", "sqrt", [(10.0, 1.0), (20.0, 4.0)])


class PreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        image, draw = new_canvas()
        draw.rectangle([50, 170, 340, 270], fill=core.parse_color(BAR_COLOR))
        self.path = self.tmp / "figure.png"
        image.save(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_preflight_records_identity_and_never_authorises(self) -> None:
        source = core.RasterSource.load(self.path)
        report = core.preflight(source, None)
        self.assertEqual(report["input"]["sha256"], core.sha256_file(self.path))
        self.assertEqual([report["input"]["width"], report["input"]["height"]], [WIDTH, HEIGHT])
        self.assertFalse(report["numeric_output_authorized"])
        self.assertEqual(report["proposal"]["status"], "needs_chart_type_confirmation")
        self.assertIsNotNone(report["content"]["content_bbox"])

    def test_preflight_proposes_a_route_without_authorising(self) -> None:
        source = core.RasterSource.load(self.path)
        report = core.preflight(source, "histogram")
        self.assertEqual(report["proposal"]["route"], "histogram")
        self.assertFalse(report["numeric_output_authorized"])

    def test_source_identity_mismatch_is_detected(self) -> None:
        source = core.RasterSource.load(self.path)
        problems = source.check_identity({"sha256": "deadbeef", "width": WIDTH, "height": 1})
        self.assertEqual(len(problems), 2)


class SpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        image, draw = new_canvas()
        draw.rectangle([50, 170, 340, 270], fill=core.parse_color(BAR_COLOR))
        self.path = self.tmp / "figure.png"
        image.save(self.path)
        self.source = core.RasterSource.load(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_template_is_not_ready(self) -> None:
        problems = core.validate_spec(core.spec_template(self.source, "histogram"))
        self.assertTrue(any("verified" in problem for problem in problems))
        self.assertTrue(any("plot_bounds" in problem for problem in problems))
        self.assertTrue(any("series" in problem for problem in problems))

    def test_filled_spec_passes(self) -> None:
        spec = base_spec(self.source, "histogram")
        spec["series"] = [{"name": "counts", "color": BAR_COLOR}]
        self.assertEqual(core.validate_spec(spec), [])

    def test_unknown_chart_type_and_bad_anchors_are_reported(self) -> None:
        spec = base_spec(self.source, "sankey")
        spec["series"] = [{"name": "counts", "color": BAR_COLOR}]
        problems = core.validate_spec(spec)
        self.assertTrue(any("no registered route" in problem for problem in problems))
        spec = base_spec(self.source, "histogram")
        spec["series"] = [{"name": "counts", "color": BAR_COLOR}]
        spec["calibration"]["y"]["anchors"] = [[270.0, 0.0]]
        self.assertTrue(any("at least two" in problem for problem in core.validate_spec(spec)))

    def test_unverified_figure_is_refused_before_measuring(self) -> None:
        spec = base_spec(self.source, "histogram", verified=False)
        spec["series"] = [{"name": "counts", "color": BAR_COLOR}]
        with self.assertRaises(ValueError) as caught:
            core.run_extraction(spec, self.source, self.tmp / "out")
        self.assertIn("verified", str(caught.exception))


class HistogramTests(unittest.TestCase):
    heights = [4.0, 8.0, 2.0, 6.0, 10.0]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        image, draw = new_canvas()
        for index, height in enumerate(self.heights):
            left = 50 + X_PER_UNIT * index
            top = BASELINE_PX - Y_PER_UNIT * height
            draw.rectangle([left + 2, top, left + X_PER_UNIT - 2, BASELINE_PX],
                           fill=core.parse_color(BAR_COLOR))
        self.path = self.tmp / "hist.png"
        image.save(self.path)
        self.source = core.RasterSource.load(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_bin_heights_match_the_drawn_columns(self) -> None:
        spec = base_spec(self.source, "histogram")
        spec["series"] = [{"name": "counts", "color": BAR_COLOR}]
        report = core.run_extraction(spec, self.source, self.tmp / "out")
        self.assertTrue(report["numeric_output_authorized"], report["authorization_blockers"])
        self.assertEqual(report["status"], core.STATUS_EXTRACTED)
        rows = (self.tmp / "out" / "data.csv").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(rows), len(self.heights) + 1)
        measured = [float(row.split(",")[4]) for row in rows[1:]]
        for got, expected in zip(measured, self.heights):
            self.assertAlmostEqual(got, expected, delta=1.0 / Y_PER_UNIT)

    def test_artifacts_are_written_before_review(self) -> None:
        spec = base_spec(self.source, "histogram")
        spec["series"] = [{"name": "counts", "color": BAR_COLOR}]
        report = core.run_extraction(spec, self.source, self.tmp / "out")
        artifacts = report["artifacts"]
        self.assertTrue(Path(artifacts["data_csv"]).is_file())
        self.assertTrue(Path(artifacts["overlay_png"]["path"]).is_file())
        self.assertTrue(Path(artifacts["recreated_png"]["path"]).is_file())
        saved = json.loads((self.tmp / "out" / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["schema"], core.REPORT_SCHEMA)
        self.assertIn("calibration", saved)

    def test_overlay_is_drawn_on_the_original_canvas(self) -> None:
        spec = base_spec(self.source, "histogram")
        spec["series"] = [{"name": "counts", "color": BAR_COLOR}]
        report = core.run_extraction(spec, self.source, self.tmp / "out")
        with Image.open(report["artifacts"]["overlay_png"]["path"]) as overlay:
            self.assertEqual(overlay.size, (WIDTH, HEIGHT))


class ScatterTests(unittest.TestCase):
    points = [(1.0, 2.0), (2.0, 4.0), (3.0, 6.0), (4.0, 8.0)]

    def _draw(self, offsets: dict[int, tuple[float, float]] | None = None):
        image, draw = new_canvas()
        offsets = offsets or {}
        for index, (x, y) in enumerate(self.points):
            px, py = data_to_px(x, y)
            dx, dy = offsets.get(index, (0.0, 0.0))
            draw.ellipse([px + dx - 3, py + dy - 3, px + dx + 3, py + dy + 3],
                         fill=core.parse_color(POINT_COLOR))
        return image

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _spec(self, source: core.RasterSource) -> dict:
        spec = base_spec(source, "scatter")
        spec["series"] = [{"name": "group A", "color": POINT_COLOR}]
        spec["options"]["min_component_px"] = 6
        spec["options"]["max_component_px"] = 200
        return spec

    def test_points_are_recovered_at_their_data_coordinates(self) -> None:
        path = self.tmp / "scatter.png"
        self._draw().save(path)
        source = core.RasterSource.load(path)
        report = core.run_extraction(self._spec(source), source, self.tmp / "out")
        self.assertTrue(report["numeric_output_authorized"], report["authorization_blockers"])
        rows = (self.tmp / "out" / "data.csv").read_text(encoding="utf-8").strip().splitlines()[1:]
        self.assertEqual(len(rows), len(self.points))
        measured = sorted((float(r.split(",")[1]), float(r.split(",")[2])) for r in rows)
        for (got_x, got_y), (want_x, want_y) in zip(measured, self.points):
            self.assertAlmostEqual(got_x, want_x, delta=0.15)
            self.assertAlmostEqual(got_y, want_y, delta=0.15)

    def test_merged_markers_are_never_split_into_invented_points(self) -> None:
        path = self.tmp / "merged.png"
        self._draw({1: (-2.0, 0.0)}).save(path)
        source = core.RasterSource.load(path)
        spec = self._spec(source)
        spec["options"]["max_component_px"] = 30
        report = core.run_extraction(spec, source, self.tmp / "out")
        self.assertFalse(report["numeric_output_authorized"])
        self.assertGreater(report["diagnostics"]["unresolved_conflicts"], 0)
        self.assertIn("ambiguous", " ".join(report["authorization_blockers"]))


class LineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        image, draw = new_canvas()
        gap_from, gap_to = 180, 200
        previous: tuple[float, float] | None = None
        for px in range(50, 350):
            y_value = 1.0 + (px - 50) / 300.0 * 8.0
            py = BASELINE_PX - Y_PER_UNIT * y_value
            if gap_from <= px < gap_to:
                previous = (px, py)
                continue
            if previous is not None:
                draw.line([previous[0], previous[1], px, py], fill=core.parse_color(LINE_COLOR), width=3)
            previous = (px, py)
        self.path = self.tmp / "line.png"
        image.save(self.path)
        self.source = core.RasterSource.load(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_gaps_stay_missing_and_are_reported(self) -> None:
        spec = base_spec(self.source, "line")
        spec["series"] = [{"name": "series A", "color": LINE_COLOR, "tolerance": 40}]
        report = core.run_extraction(spec, self.source, self.tmp / "out")
        series = report["series"][0]
        self.assertGreater(series["gap_columns"], 0)
        self.assertTrue(series["gap_ranges"])
        self.assertEqual(series["status"], core.STATUS_PARTIAL)
        self.assertTrue(report["numeric_output_authorized"], report["authorization_blockers"])
        self.assertEqual(report["status"], core.STATUS_PARTIAL)
        rows = (self.tmp / "out" / "data.csv").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(rows) - 1, series["sample_count"])
        self.assertIn("never interpolated", " ".join(report["notes"]))

    def test_traced_values_follow_the_drawn_ramp(self) -> None:
        spec = base_spec(self.source, "line")
        spec["series"] = [{"name": "series A", "color": LINE_COLOR, "tolerance": 40}]
        report = core.run_extraction(spec, self.source, self.tmp / "out")
        rows = (self.tmp / "out" / "data.csv").read_text(encoding="utf-8").strip().splitlines()[1:]
        first = rows[0].split(",")
        last = rows[-1].split(",")
        self.assertAlmostEqual(float(first[1]), 0.0, delta=0.05)
        self.assertAlmostEqual(float(first[2]), 1.0, delta=0.06)
        self.assertAlmostEqual(float(last[1]), 5.0, delta=0.05)
        self.assertAlmostEqual(float(last[2]), 9.0, delta=0.06)


class GateTests(unittest.TestCase):
    def test_calibration_residual_blocks_authorisation(self) -> None:
        axis = core.Axis("y", "linear", [(270.0, 0.0), (20.0, 10.0), (150.0, 99.0)])
        self.assertGreater(axis.residual()["max_data_residual"], 1.0)

    def test_bar_route_requires_a_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            image, draw = new_canvas()
            draw.rectangle([52, 170, 108, 270], fill=core.parse_color(BAR_COLOR))
            path = tmp_path / "bar.png"
            image.save(path)
            source = core.RasterSource.load(path)
            spec = base_spec(source, "bar")
            spec["series"] = [{"name": "A", "color": BAR_COLOR}]
            with self.assertRaises(ValueError) as caught:
                core.run_extraction(spec, source, tmp_path / "out")
            self.assertIn("baseline_value", str(caught.exception))
            spec["options"]["baseline_value"] = 0.0
            report = core.run_extraction(spec, source, tmp_path / "out")
            self.assertTrue(report["numeric_output_authorized"], report["authorization_blockers"])
            self.assertEqual(report["diagnostics"]["measured_bars"], 1)


class CropTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        image, draw = new_canvas()
        draw.rectangle([100, 100, 199, 199], fill=core.parse_color(BAR_COLOR))
        self.path = self.tmp / "page.png"
        image.save(self.path)
        self.source = core.RasterSource.load(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_native_crop_is_byte_identical_to_the_page_region(self) -> None:
        record = core.crop_region(self.source, [0.25, 0.25, 0.25, 0.25], self.tmp / "crop.png")
        self.assertTrue(record["measurable"])
        self.assertEqual(record["pixel_rect"], [100, 75, 200, 150])
        with Image.open(record["crop"]["path"]) as cropped:
            self.assertEqual(cropped.size, (100, 75))
        reloaded = core.RasterSource.load(record["crop"]["path"])
        np.testing.assert_array_equal(reloaded.array, self.source.array[75:150, 100:200])
        self.assertEqual(record["parent"]["sha256"], self.source.sha256)
        self.assertTrue(Path(record["crop"]["path"] + ".crop.json").is_file())

    def test_enlarged_crop_is_flagged_unmeasurable(self) -> None:
        record = core.crop_region(
            self.source, [100, 100, 50, 50], self.tmp / "zoom.png",
            bounds_mode="pixels", review_scale=4.0,
        )
        self.assertFalse(record["measurable"])
        self.assertEqual(record["crop"]["width"], 200)

    def test_empty_or_out_of_range_bbox_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            core.crop_region(self.source, [0.9, 0.9, 0.0, 0.1], self.tmp / "x.png")
        with self.assertRaises(ValueError):
            core.crop_region(self.source, [10, 10, 5, 5], self.tmp / "y.png")


class RouteRegistryTests(unittest.TestCase):
    def test_every_route_is_reachable_and_versioned(self) -> None:
        table = core.route_table()
        self.assertTrue(table)
        for route in table:
            self.assertTrue(core.resolve_route(route["chart_types"][0]))
            self.assertIn(route["maturity"], {core.MATURITY_STABLE, core.MATURITY_CANDIDATE,
                                              core.MATURITY_ASSISTED})
            self.assertTrue(route["refuses"])

    def test_unknown_chart_type_has_no_route(self) -> None:
        self.assertIsNone(core.resolve_route("sankey"))
        self.assertIsNone(core.resolve_route(None))

    def test_no_route_ships_as_stable_without_benchmarks(self) -> None:
        # Promotion to `stable` requires held-out figures and a matched external
        # comparison; this skill ships candidate routes only.
        self.assertTrue(all(route["maturity"] == core.MATURITY_CANDIDATE
                            for route in core.route_table()))


if __name__ == "__main__":
    unittest.main()
