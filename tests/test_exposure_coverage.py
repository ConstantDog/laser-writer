import math
import tempfile
import unittest
from pathlib import Path
from shapely.geometry import box, Polygon
from shapely.ops import unary_union
from matplotlib.figure import Figure
from fluidnc_laser_writer.core import (
    PathSettings,
    LayerInfo,
    MotionSegment,
    generate_serpentine_plan,
    build_gcode,
    exposure_footprints,
    add_exposure_coverage,
    save_simulation_png,
)


class ExposureCoverageTests(unittest.TestCase):

    def settings(self, **overrides):
        values = dict(
            laser_spot_mm=0.04,
            hatch_spacing_mm=0.04,
            x_steps_per_mm=3200,
            y_steps_per_mm=3200,
        )
        values.update(overrides)
        return PathSettings(**values)

    def test_100um_strip_40um_width_centers_20_50_80(self):
        plan = generate_serpentine_plan(box(0, 0, 1, 0.1), self.settings())
        self.assertEqual([row.y_mm for row in plan.rows], [0.02, 0.05, 0.08])
        self.assertEqual([row.left_to_right for row in plan.rows], [True, False, True])

    def test_two_rows_for_50um_width_and_three_for_40um(self):
        plan = generate_serpentine_plan(
            box(0, 0, 1, 0.1), self.settings(laser_spot_mm=0.05, hatch_spacing_mm=0.05)
        )
        self.assertEqual([row.y_mm for row in plan.rows], [0.025, 0.075])
        self.assertEqual(plan.exposure_width_mm, 0.05)

    def test_tighter_maximum_spacing_is_honored(self):
        plan = generate_serpentine_plan(
            box(0, 0, 1, 0.1), self.settings(hatch_spacing_mm=0.02)
        )
        self.assertEqual([r.y_mm for r in plan.rows], [0.02, 0.04, 0.06, 0.08])

    def test_larger_maximum_spacing_is_capped_by_width_and_warned(self):
        plan = generate_serpentine_plan(
            box(0, 0, 1, 0.1), self.settings(hatch_spacing_mm=0.1)
        )
        self.assertEqual([r.y_mm for r in plan.rows], [0.02, 0.05, 0.08])
        self.assertTrue(any(("Maximum hatch" in w for w in plan.warnings)))

    def test_eight_gds_like_strips_do_not_lose_boundary_rows(self):
        parts = [
            box(
                -0.15,
                i * 0.20000000000000007 - 0.05000000000000001,
                0.15,
                i * 0.20000000000000007 + 0.05000000000000001,
            )
            for i in range(8)
        ]
        plan = generate_serpentine_plan(unary_union(parts), self.settings())
        self.assertEqual(len(plan.rows), 24)
        for i in range(8):
            for row, offset in zip(plan.rows[i * 3 : i * 3 + 3], [0.02, 0.05, 0.08]):
                self.assertAlmostEqual(row.y_mm, i * 0.2 + offset)

    def test_components_do_not_expose_at_each_others_extra_rows(self):
        geometry = unary_union([box(0, 0, 0.2, 0.1), box(0.4, 0.005, 0.6, 0.105)])
        plan = generate_serpentine_plan(geometry, self.settings())
        self.assertEqual(len(plan.rows), 6)
        self.assertTrue(all((len(r.exposure_intervals) == 1 for r in plan.rows)))
        left_rows = [r.y_mm for r in plan.rows if r.exposure_intervals[0][0] < 0.4]
        right_rows = [r.y_mm for r in plan.rows if r.exposure_intervals[0][0] > 0.4]
        self.assertEqual(left_rows, [0.02, 0.05, 0.08])
        self.assertEqual(right_rows, [0.025, 0.055, 0.085])

    def test_quantization_does_not_exceed_maximum_gap(self):
        settings = self.settings(hatch_spacing_mm=0.027, y_steps_per_mm=3200)
        plan = generate_serpentine_plan(box(0, 0, 1, 0.173), settings)
        ys = [r.y_mm for r in plan.rows]
        self.assertTrue(all((0 < b - a <= 0.027 + 1e-10 for a, b in zip(ys, ys[1:]))))
        self.assertLessEqual(abs(ys[0] - 0.02), 0.5 / 3200)
        self.assertLessEqual(abs(ys[-1] - (0.173 - 0.02)), 0.5 / 3200)

    def test_narrow_component_is_centered_and_warned(self):
        plan = generate_serpentine_plan(box(0, 0, 1, 0.02), self.settings())
        self.assertEqual([r.y_mm for r in plan.rows], [0.01])
        self.assertTrue(any(("narrower" in w for w in plan.warnings)))

    def test_hole_is_not_filled_by_exposure_centerlines(self):
        geometry = box(0, 0, 1, 0.3).difference(box(0.3, 0.1, 0.7, 0.2))
        plan = generate_serpentine_plan(geometry, self.settings())
        self.assertTrue(any(("holed" in w for w in plan.warnings)))
        for row in plan.rows:
            if 0.1 < row.y_mm < 0.2:
                self.assertEqual(len(row.exposure_intervals), 2)
                self.assertLessEqual(row.exposure_intervals[0][1], 0.5 + 1e-08)
                self.assertGreaterEqual(row.exposure_intervals[1][0], 0.9 - 1e-08)

    def test_width_hatch_and_steps_must_be_finite_positive(self):
        for name in (
            "laser_spot_mm",
            "hatch_spacing_mm",
            "x_steps_per_mm",
            "y_steps_per_mm",
        ):
            for value in (0, -1, math.inf, math.nan):
                with self.subTest(name=name, value=value), self.assertRaises(
                    ValueError
                ):
                    self.settings(**{name: value})

    def test_footprint_is_mm_geometry_not_screen_linewidth(self):
        segment = MotionSegment(0, 0, 0.02, 1, 0.02, 5, True, 500, False)
        footprint = exposure_footprints([segment], 0.04)[0]
        for actual, expected in zip(footprint.bounds, (-0.02, 0, 1.02, 0.04)):
            self.assertAlmostEqual(actual, expected)
        fig = Figure()
        ax = fig.add_subplot(111)
        collection = add_exposure_coverage(ax, [segment], 0.04)
        before = collection.get_paths()[0].vertices.copy()
        ax.set_xlim(0, 0.1)
        self.assertTrue((before == collection.get_paths()[0].vertices).all())

    def test_generated_gcode_and_preview_have_same_centers_and_width(self):
        plan = generate_serpentine_plan(box(0, 0, 1, 0.1), self.settings())
        program = build_gcode(
            plan,
            self.settings(),
            source_path=Path("test.gds"),
            cell_name="TOP",
            layer_info=LayerInfo(1, 0, 1, 0, 0, 1, 0.1),
            machine_width_mm=5,
            machine_height_mm=5,
        )
        burn = [s for s in program.analysis.segments if s.laser_on]
        self.assertEqual([s.start_y_mm for s in burn], [0.02, 0.05, 0.08])
        coverage = unary_union(exposure_footprints(burn, plan.exposure_width_mm))
        self.assertTrue(coverage.covers(box(0.3, 0, 1.1, 0.1)))
        self.assertAlmostEqual(coverage.bounds[1], 0)
        self.assertAlmostEqual(coverage.bounds[3], 0.1)
        self.assertIn("Measured writing width: 0.0400 mm", program.text)
        with tempfile.TemporaryDirectory() as tmp:
            file = save_simulation_png(program, Path(tmp) / "preview.png", 0.2)
            self.assertGreater(file.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
