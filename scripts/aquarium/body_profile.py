"""Authored adult-koi top-width prototype, guided by conformation references.

These stations are not measured morphometrics. Breeder portraits may be posed
or oblique; sex, age and condition also change conformation. The independent
top width replaces a width inferred from the generated side silhouette.

Call profile_width(x, original_width) inside the existing profile contract.
Retain its vertical upper/lower/center values and the complete 34-joint rig.
Face surfaces, ribs and fin attachments require registration on a rebuilt
variant; preserving the rostral interval does not preserve the entire face.

Primary anatomy and visual context:
https://zna.jp/en/keep-nishikigoi/nishikigoi-terminology/nishikigoi-terminology-1-body/
https://dainichikoifarm.com/championship-grand-champion/
https://www.kodamakoifarm.com/how-to-take-photos-and-videos-of-koi-fish/
"""
import math


# Native Blender metres: X longitudinal, Y lateral, Z vertical. glTF export
# converts the lateral direction to Z. These are authored half-widths.
CONTROL_POINTS = [
    (-.660, .0245), (-.640, .0450), (-.620, .0670), (-.590, .0840),
    (-.560, .0980), (-.534, .1080), (-.505, .1155), (-.470, .1245),
    (-.415, .1350), (-.370, .1430),
    (-.310, .1490), (-.240, .1470), (-.130, .1370), (.000, .1150),
    (.130, .0910), (.260, .0660), (.370, .0480), (.420, .0400),
    (.454, .0260),
]
PRESERVE_HEAD_THROUGH = -.655
HEAD_BLEND_END = -.610
TAIL_BLEND_START = .420
PRESERVE_TAIL_FROM = .454


def _slopes(points):
    """Shape-preserving PCHIP tangents on nonuniform X intervals."""
    h = [b[0] - a[0] for a, b in zip(points, points[1:])]
    d = [(b[1] - a[1]) / step for a, b, step in zip(points, points[1:], h)]
    slopes = [0.0] * len(points)
    for i in range(1, len(points) - 1):
        if d[i - 1] * d[i] > 0:
            w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
            slopes[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])

    def endpoint(h0, h1, d0, d1):
        value = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
        if value * d0 <= 0:
            return 0.0
        return math.copysign(min(abs(value), 3 * abs(d0)), value)

    slopes[0] = endpoint(h[0], h[1], d[0], d[1])
    slopes[-1] = endpoint(h[-1], h[-2], d[-1], d[-2])
    return slopes


_SLOPES = _slopes(CONTROL_POINTS)


def target_width(x):
    """Independent shoulder-to-peduncle target, before its boundary blends."""
    if not math.isfinite(x):
        raise ValueError("A finite native X coordinate is required")
    if x <= CONTROL_POINTS[0][0]:
        return CONTROL_POINTS[0][1]
    for i, (p, q) in enumerate(zip(CONTROL_POINTS, CONTROL_POINTS[1:])):
        if x <= q[0]:
            h = q[0] - p[0]
            t = (x - p[0]) / h
            return ((2 * t**3 - 3 * t*t + 1) * p[1]
                    + (t**3 - 2 * t*t + t) * h * _SLOPES[i]
                    + (-2 * t**3 + 3 * t*t) * q[1]
                    + (t**3 - t*t) * h * _SLOPES[i + 1])
    return CONTROL_POINTS[-1][1]


def _smooth(x):
    t = max(0.0, min(1.0, x))
    return t*t*t*(10 + t*(-15 + 6*t))


def profile_width(x, original_width):
    """Modify only lateral half-width, preserving rostral and caudal joins.

    The modification has C2 boundary weights and the target is C1 PCHIP.
    This does not assert that the entire original profile was C2. The original
    nose/mouth through -.655 m and flesh/caudal-root interval from +.454 m are
    exact. Forehead/eye width is deliberately changed: the entire orbital,
    opercular and other attachments must be registered to the new surface.
    """
    if not math.isfinite(x) or not math.isfinite(original_width) or original_width <= 0:
        raise ValueError("Finite native X and positive original half-width required")
    if x <= PRESERVE_HEAD_THROUGH or x >= PRESERVE_TAIL_FROM:
        return original_width
    entry = _smooth((x - PRESERVE_HEAD_THROUGH) / (HEAD_BLEND_END - PRESERVE_HEAD_THROUGH))
    exit_weight = 1 - _smooth((x - TAIL_BLEND_START) / (PRESERVE_TAIL_FROM - TAIL_BLEND_START))
    return original_width + entry * exit_weight * (target_width(x) - original_width)
