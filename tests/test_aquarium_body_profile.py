"""Check the independently authored top-width and its attachment boundaries."""
import math
import unittest

from scripts.aquarium.body_profile import (
    CONTROL_POINTS,
    HEAD_BLEND_END,
    PRESERVE_HEAD_THROUGH,
    PRESERVE_TAIL_FROM,
    TAIL_BLEND_START,
    profile_width,
    target_width,
)


# Sampled current side-derived envelope. This fixture has its broad maximum at
# 45% of flesh length and the existing narrow caudal attachment. It is a shape
# baseline, not a claim about a photographed animal or a copy of PCHIP logic.
_BASELINE = [
    (-.66, .0245), (-.62, .042377), (-.57, .065236), (-.534, .080074),
    (-.505, .089434), (-.47, .099326), (-.415, .113993), (-.386, .120565),
    (-.37, .123841), (-.31, .133211), (-.24, .138544), (-.13, .143855),
    (-.102, .144399), (0, .141876), (.13, .124641), (.26, .097719),
    (.37, .070148), (.42, .044549), (.43, .038216), (.44, .032417),
    (.454, .026), (.525, .014), (.54, .009), (.58, .0008),
]


def baseline(x):
    if x <= _BASELINE[0][0]:
        return _BASELINE[0][1]
    for a, b in zip(_BASELINE, _BASELINE[1:]):
        if x <= b[0]:
            t = (x - a[0]) / (b[0] - a[0])
            return a[1] + t * (b[1] - a[1])
    return _BASELINE[-1][1]


class AquariumBodyProfileTest(unittest.TestCase):
    def test_original_rostrum_and_caudal_attachment_are_exact(self):
        for x in [-.8, -.676, -.66, PRESERVE_HEAD_THROUGH,
                  PRESERVE_TAIL_FROM, .525, .54, .58, .7]:
            for original in [.0008, .0245, .08, .2]:
                self.assertEqual(profile_width(x, original), original)

    def test_anterior_shoulders_replace_the_central_ellipse_maximum(self):
        xs = [-.66 + i * .0005 for i in range(2481)]
        old_peak = max(xs, key=baseline)
        new_peak = max(xs, key=lambda x: profile_width(x, baseline(x)))
        self.assertGreater((old_peak + .66) / 1.24, .40)
        self.assertLess((new_peak + .66) / 1.24, .35)
        self.assertAlmostEqual(new_peak, -.31, places=6)
        self.assertAlmostEqual(profile_width(new_peak, baseline(new_peak)), .149)
        self.assertGreater(profile_width(-.37, baseline(-.37)), baseline(-.37) + .015)
        self.assertLess(profile_width(.26, baseline(.26)), baseline(.26) - .025)

    def test_forehead_broadens_before_the_eyes_without_a_needle_head(self):
        # Authored proportions, not a universal ratio inferred from a portrait.
        self.assertGreater(profile_width(-.62, baseline(-.62)), baseline(-.62) + .02)
        self.assertAlmostEqual(profile_width(-.534, baseline(-.534)), .108)
        self.assertGreater(profile_width(-.534, baseline(-.534)), baseline(-.534) + .025)
        self.assertAlmostEqual(profile_width(-.505, baseline(-.505)), .1155)

    def test_posterior_taper_has_no_second_width_bulge(self):
        xs = [-.31 + i * (.454 + .31) / 4000 for i in range(4001)]
        widths = [profile_width(x, baseline(x)) for x in xs]
        for a, b in zip(widths, widths[1:]):
            self.assertLessEqual(b, a + 1e-12)
        self.assertAlmostEqual(widths[-1], .026)

    def test_pchip_keeps_control_stations_and_has_no_interval_overshoot(self):
        for x, width in CONTROL_POINTS:
            self.assertAlmostEqual(target_width(x), width, places=13)
        for a, b in zip(CONTROL_POINTS, CONTROL_POINTS[1:]):
            for i in range(101):
                x = a[0] + (b[0] - a[0]) * i / 100
                self.assertGreaterEqual(target_width(x), min(a[1], b[1]) - 1e-12)
                self.assertLessEqual(target_width(x), max(a[1], b[1]) + 1e-12)

    def test_width_and_first_derivative_are_continuous_at_control_stations(self):
        h = 1e-6
        for x, _ in CONTROL_POINTS[1:-1]:
            left = (target_width(x) - target_width(x - h)) / h
            right = (target_width(x + h) - target_width(x)) / h
            self.assertAlmostEqual(left, right, delta=5e-5)

    def test_boundary_modification_has_zero_first_and_second_derivatives(self):
        # The compact quintic weights make the modification O(distance^3),
        # preserving the caller's boundary value, slope and curvature. Use a
        # smooth analytic baseline to isolate the new modifier from old knots.
        for edge, direction in [(PRESERVE_HEAD_THROUGH, 1), (PRESERVE_TAIL_FROM, -1)]:
            def modification(distance):
                x = edge + direction * distance
                original = .1 + .006 * x + .002 * x*x
                return profile_width(x, original) - original
            self.assertEqual(modification(0), 0)
            for h in [2e-4, 1e-4, 5e-5]:
                large, small = abs(modification(h)), abs(modification(h / 2))
                self.assertLessEqual(small, .14 * large + 1e-15)

    def test_inner_blend_residual_reaches_target_with_zero_slope_and_curvature(self):
        # At the full-target joins only the residual must vanish cubically.
        # PCHIP itself is C1, so this does not demand a C2 target at its knots.
        for edge, direction in [(HEAD_BLEND_END, -1), (TAIL_BLEND_START, 1)]:
            def residual(distance):
                x = edge + direction * distance
                original = .1 + .006 * x + .002 * x*x
                return profile_width(x, original) - target_width(x)
            self.assertAlmostEqual(residual(0), 0, places=14)
            for h in [2e-4, 1e-4, 5e-5]:
                large, small = abs(residual(h)), abs(residual(h / 2))
                self.assertLessEqual(small, .14 * large + 1e-15)

    def test_blends_are_finite_positive_and_between_original_and_target(self):
        for i in range(1001):
            x = PRESERVE_HEAD_THROUGH + (PRESERVE_TAIL_FROM - PRESERVE_HEAD_THROUGH) * i / 1000
            for original in [.0008, .0245, .08, .2, 2]:
                width = profile_width(x, original)
                self.assertTrue(math.isfinite(width) and width > 0)
                self.assertGreaterEqual(width, min(original, target_width(x)) - 1e-12)
                self.assertLessEqual(width, max(original, target_width(x)) + 1e-12)

    def test_invalid_native_coordinates_or_widths_are_rejected(self):
        for x, width in [(math.nan, .1), (math.inf, .1), (0, math.nan), (0, math.inf), (0, 0), (0, -.1)]:
            with self.assertRaises(ValueError):
                profile_width(x, width)
