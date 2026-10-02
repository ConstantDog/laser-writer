import math
import unittest
from fluidnc_laser_writer.pd_controller import PDSample
from fluidnc_laser_writer.pd_reconstruction import reconstruct_high_curve


def samples(values):
    return [PDSample(i * 0.005, i * 5, None, v) for i, v in enumerate(values)]


class ReconstructionTests(unittest.TestCase):

    def test_two_levels_interpolate_high_anchors(self):
        result = reconstruct_high_curve(samples([550, 50, 552, 50, 554, 50, 556]))
        self.assertEqual(result, [550, 551, 552, 553, 554, 555, 556])

    def test_no_trailing_extrapolation(self):
        result = reconstruct_high_curve(samples([550, 50, 550, 50, 550, 50]))
        self.assertTrue(math.isnan(result[-1]))

    def test_single_low_level_is_not_hidden(self):
        self.assertEqual(reconstruct_high_curve(samples([0] * 80)), [0] * 80)
        self.assertEqual(reconstruct_high_curve(samples([50] * 80)), [50] * 80)

    def test_smooth_ramp_preserved(self):
        values = list(range(0, 800, 10))
        self.assertEqual(reconstruct_high_curve(samples(values)), values)

    def test_tracks_changing_high_level(self):
        values = [550, 50] * 20 + [300, 50] * 20
        result = reconstruct_high_curve(samples(values))
        self.assertEqual(result[10], 550)
        self.assertEqual(result[50], 300)


if __name__ == "__main__":
    unittest.main()
