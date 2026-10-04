from __future__ import annotations
import unittest
from pathlib import Path
from shapely.geometry import box
from shapely.ops import unary_union
from fluidnc_laser_writer.core import (
    DesignRules,
    LayerInfo,
    PathSettings,
    analyze_gcode,
    build_gcode,
    generate_serpentine_plan,
    run_drc,
)


class CoreTests(unittest.TestCase):

    def test_x_compensation_range_and_default(self):
        self.assertEqual(PathSettings().x_backlash_um, 0)
        for value in (-100, -0.5, 0, 0.5, 100):
            self.assertEqual(PathSettings(x_backlash_um=value).x_backlash_um, value)
        for value in (-100.001, 100.001, float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PathSettings(x_backlash_um=value)

    def test_x_compensation_translates_only_reverse_rows_preserves_width(self):
        geometry = unary_union([box(0, 0, 0.2, 0.3), box(0.3, 0, 0.5, 0.3)])
        base = dict(hatch_spacing_mm=0.05, x_steps_per_mm=3200, y_steps_per_mm=3200)
        normal = generate_serpentine_plan(geometry, PathSettings(**base))
        for value in (-100, -20, 0, 1, 20, 100):
            with self.subTest(value=value):
                settings = PathSettings(**base, x_backlash_um=value)
                plan = generate_serpentine_plan(geometry, settings)
                self.assertEqual(len(plan.rows), len(normal.rows))
                self.assertEqual(
                    (plan.left_mm, plan.right_mm), (normal.left_mm, normal.right_mm)
                )
                expected = -round(value / 1000 * 3200) / 3200
                for old, row in zip(normal.rows, plan.rows):
                    self.assertEqual(row.y_mm, old.y_mm)
                    self.assertEqual(row.left_to_right, old.left_to_right)
                    for (a, b), (c, d) in zip(
                        old.exposure_intervals, row.exposure_intervals
                    ):
                        delta = 0 if row.left_to_right else expected
                        self.assertAlmostEqual(c - a, delta)
                        self.assertAlmostEqual(d - b, delta)
                        self.assertAlmostEqual(d - c, b - a)
                        self.assertGreaterEqual(c, plan.left_mm)
                        self.assertLessEqual(d, plan.right_mm)
                program = build_gcode(
                    plan,
                    settings,
                    source_path=Path("test.gds"),
                    cell_name="TOP",
                    layer_info=LayerInfo(1, 0, 2, 0, 0, 0.5, 0.3),
                    machine_width_mm=1,
                    machine_height_mm=1,
                )
                self.assertTrue(program.analysis.valid)
                burn = [s for s in program.analysis.segments if s.laser_on]
                self.assertEqual(
                    len(burn), sum((len(r.exposure_intervals) for r in plan.rows))
                )
                pos = 0
                for row in plan.rows:
                    intervals = (
                        row.exposure_intervals
                        if row.left_to_right
                        else tuple(reversed(row.exposure_intervals))
                    )
                    for a, b in intervals:
                        segment = burn[pos]
                        pos += 1
                        expected_start, expected_end = (
                            (a, b) if row.left_to_right else (b, a)
                        )
                        self.assertAlmostEqual(
                            segment.start_x_mm, expected_start, delta=5.1e-05
                        )
                        self.assertAlmostEqual(
                            segment.end_x_mm, expected_end, delta=5.1e-05
                        )
                self.assertTrue(
                    all(
                        (
                            s.feed_mm_min == settings.feed_mm_min
                            for s in program.analysis.segments
                        )
                    )
                )
                if value == 0:
                    self.assertEqual(plan.rows, normal.rows)
                    self.assertNotIn("; X bidirectional compensation:", program.text)
                else:
                    self.assertIn("; X bidirectional compensation:", program.text)

    def test_compensation_rejects_insufficient_overscan_without_clipping(self):
        for value in (-100, 100):
            with self.assertRaisesRegex(ValueError, "overscan"):
                generate_serpentine_plan(
                    box(0, 0, 1, 0.1),
                    PathSettings(
                        overscan_mm=0.05, x_backlash_um=value, x_steps_per_mm=3200
                    ),
                )

    def test_compensation_counts_real_rows_when_empty_scan_rows_skipped(self):
        shape = unary_union([box(0, 0, 1, 0.01), box(0, 0.2, 1, 0.3)])
        plan = generate_serpentine_plan(
            shape,
            PathSettings(hatch_spacing_mm=0.05, x_backlash_um=20, x_steps_per_mm=3200),
        )
        self.assertTrue(plan.rows[0].left_to_right)
        self.assertFalse(plan.rows[1].left_to_right)
        self.assertAlmostEqual(plan.rows[1].exposure_intervals[0][0], 0.18)

    def test_motion_caps_cannot_be_bypassed(self):
        for key in ("feed_mm_min", "acceleration_mm_s2"):
            for value in (0, -1, 10.01, float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    PathSettings(**{key: value})
        self.assertEqual(
            PathSettings(feed_mm_min=10, acceleration_mm_s2=10).feed_mm_min, 10
        )
        result = analyze_gcode(
            "G21\nG90\nG94\nM5\nG1 X1 F11\nM5\n",
            machine_width_mm=100,
            machine_height_mm=100,
            max_feed_mm_min=1000,
            max_laser_power=1000,
        )
        self.assertFalse(result.valid)

    def test_100um_drc_passes_valid_geometry(self) -> None:
        polygons = [box(0.0, 0.0, 0.2, 0.2), box(0.3, 0.0, 0.5, 0.2)]
        result = run_drc(polygons, DesignRules())
        self.assertTrue(result.passed, result.report_text())
        self.assertAlmostEqual(result.min_spacing_mm or 0.0, 0.1, places=6)

    def test_100um_drc_rejects_small_feature(self) -> None:
        result = run_drc([box(0.0, 0.0, 0.08, 0.2)], DesignRules())
        self.assertFalse(result.passed)
        self.assertLess(result.min_feature_mm, 0.1)

    def test_safe_serpentine_has_no_rapid_motion(self) -> None:
        geometry = unary_union([box(0.0, 0.0, 1.0, 0.2), box(1.2, 0.0, 1.4, 0.2)])
        settings = PathSettings(
            hatch_spacing_mm=0.025,
            feed_mm_min=5.0,
            acceleration_mm_s2=5.0,
            overscan_mm=0.2,
            row_settle_ms=20,
            x_steps_per_mm=80.0,
            y_steps_per_mm=80.0,
        )
        plan = generate_serpentine_plan(geometry, settings)
        info = LayerInfo(1, 0, 2, 0.0, 0.0, 1.4, 0.2)
        program = build_gcode(
            plan,
            settings,
            source_path=Path("test.gds"),
            cell_name="TOP",
            layer_info=info,
            machine_width_mm=100.0,
            machine_height_mm=100.0,
        )
        self.assertNotIn("G0 ", program.text)
        self.assertIn("M3 S0", program.text)
        self.assertIn("S500", program.text)
        self.assertTrue(program.analysis.valid, program.analysis.errors)
        segments = program.analysis.segments
        self.assertTrue(any((not s.laser_on for s in segments)))
        self.assertTrue(all((s.feed_mm_min == 5 for s in segments)))
        for i, segment in enumerate(segments):
            if segment.laser_on:
                self.assertEqual(segment.feed_mm_min, 5)
                if i and (not segments[i - 1].laser_on):
                    self.assertEqual(segments[i - 1].feed_mm_min, 5)
        unsafe = analyze_gcode(
            "G21\nG90\nG94\nM3 S500\nG1 X1 F20\nM5\n",
            machine_width_mm=100,
            machine_height_mm=100,
            max_feed_mm_min=5,
            max_laser_power=1000,
        )
        self.assertFalse(unsafe.valid)
        dark_unsafe = analyze_gcode(
            "G21\nG90\nG94\nM5\nG1 X1 F20 S0\nM5\n",
            machine_width_mm=100,
            machine_height_mm=100,
            max_feed_mm_min=5,
            max_laser_power=1000,
        )
        self.assertFalse(dark_unsafe.valid)
        self.assertTrue(
            all((not segment.rapid for segment in program.analysis.segments))
        )
        self.assertGreater(program.analysis.burn_length_mm, 0.0)
        self.assertTrue(plan.rows[0].left_to_right)
        self.assertFalse(plan.rows[1].left_to_right)

    def test_gcode_review_rejects_out_of_range_and_missing_m5(self) -> None:
        text = "G21\nG90\nG94\nM3 S500\nG1 X101 Y0 F100\n"
        result = analyze_gcode(
            text,
            machine_width_mm=100.0,
            machine_height_mm=100.0,
            max_feed_mm_min=100.0,
            max_laser_power=1000,
        )
        self.assertFalse(result.valid)
        self.assertTrue(
            any(("exceeds machine travel" in item for item in result.errors))
        )
        self.assertTrue(any(("M5" in item for item in result.errors)))


if __name__ == "__main__":
    unittest.main()
