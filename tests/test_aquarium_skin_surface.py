"""Exercise continuous native skin coordinates and real opaque PNG mip data."""
import binascii
import math
import random
import struct
import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

from scripts.aquarium import face as FACE
from scripts.aquarium import skin_surface as SURFACE


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "aquarium" / "skin_surface.py"


def profile_at(x):
    return .12, .18, .15, .01


def point(x, theta):
    width, upper, lower, center = profile_at(x)
    return (x, width * math.cos(theta), center + (upper if math.sin(theta) >= 0 else lower) * math.sin(theta))


def read_rgb_png(path):
    """Independent stdlib decoder, including standard byte-filter reversal."""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("valid PNG signature")
    offset, compressed, size = 8, bytearray(), None
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        if binascii.crc32(kind + payload) & 0xffffffff != crc:
            raise AssertionError("PNG chunk CRC must match actual data")
        if kind == b"IHDR":
            width, height, bits, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if (bits, color, compression, filtering, interlace) != (8, 2, 0, 0, 0):
                raise AssertionError("atlas is ordinary opaque RGB, without cutout alpha")
            size = width, height
        elif kind == b"IDAT":
            compressed.extend(payload)
        offset += 12 + length
    width, height = size
    raw = zlib.decompress(compressed)
    stride, rows, previous = width * 3, [], [0] * (width * 3)
    if len(raw) != height * (stride + 1):
        raise AssertionError("all RGB rows must be present")
    for y in range(height):
        start = y * (stride + 1)
        mode, row = raw[start], list(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            left = row[i - 3] if i >= 3 else 0
            up, corner = previous[i], previous[i - 3] if i >= 3 else 0
            if mode == 1:
                prediction = left
            elif mode == 2:
                prediction = up
            elif mode == 3:
                prediction = (left + up) // 2
            elif mode == 4:
                candidate = left + up - corner
                distances = abs(candidate - left), abs(candidate - up), abs(candidate - corner)
                prediction = (left, up, corner)[distances.index(min(distances))]
            elif mode == 0:
                prediction = 0
            else:
                raise AssertionError("valid PNG byte filter")
            row[i] = (row[i] + prediction) % 256
        rows.append([tuple(row[x:x + 3]) for x in range(0, stride, 3)])
        previous = row
    return rows


def mip(rows):
    return [[tuple(sum(rows[y + dy][x + dx][c] for dy in (0, 1) for dx in (0, 1)) / 4 for c in range(3))
             for x in range(0, len(rows[0]) - 1, 2)] for y in range(0, len(rows) - 1, 2)]


class IdentityMatrix:
    def __matmul__(self, value):
        return value


class UVLayers:
    def __init__(self, count):
        self.count, self.layers, self.active = count, {}, None

    def get(self, name):
        return self.layers.get(name)

    def new(self, name):
        layer = types.SimpleNamespace(data=[types.SimpleNamespace(uv=[0, 0]) for _ in range(self.count)], active_render=False)
        self.layers[name] = layer
        return layer


class MeshObject(types.SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


def mesh_stub(vertices, faces):
    loops, polygons = [], []
    for face in faces:
        start = len(loops)
        loops.extend(types.SimpleNamespace(vertex_index=i, index=start + j) for j, i in enumerate(face))
        polygons.append(types.SimpleNamespace(loop_indices=range(start, len(loops))))
    data = types.SimpleNamespace(vertices=[types.SimpleNamespace(co=v) for v in vertices],
                                 loops=loops, polygons=polygons, uv_layers=UVLayers(len(loops)))
    return MeshObject(data=data, matrix_world=IdentityMatrix())


class AquariumSkinSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.surface = SURFACE.SkinSurface(profile_at, size=(96, 192))

    def assert_periodic(self, surface):
        for x in (-.65, -.47, -.28, -.09, .20, .43, .56):
            for theta in (-math.pi / 2, -.37, 0, .71, math.pi / 2, 2.71):
                for getter in (surface.albedo_srgb, lambda x, t: (surface.roughness(x, t),)):
                    for order, tolerance in ((0, 1e-11), (1, 1e-7), (2, 3e-4)):
                        h = 1e-4
                        def derivative(t):
                            if order == 0:
                                return getter(x, t)
                            a, b = getter(x, t - h), getter(x, t + h)
                            if order == 1:
                                return tuple((right - left) / (2 * h) for left, right in zip(a, b))
                            center = getter(x, t)
                            return tuple((left - 2 * middle + right) / h ** 2 for left, middle, right in zip(a, center, b))
                        for a, b in zip(derivative(theta), derivative(theta + math.tau)):
                            self.assertAlmostEqual(a, b, delta=tolerance)
            for theta in (-math.pi / 2 - 1e-5, -math.pi / 2, -math.pi / 2 + 1e-5):
                self.assertGreater(min(surface.albedo_srgb(x, theta)), .70, "the periodic cut stays in white ventral pigment")

    def test_periodic_color_rates_white_seam_and_separate_roughness(self):
        self.assert_periodic(self.surface)
        points = [(x / 100, t * math.tau / 37) for x in range(-65, 57, 3) for t in range(37)]
        roughness = [self.surface.roughness(x, theta) for x, theta in points]
        self.assertTrue(all(.33 <= r <= .41 for r in roughness))
        self.assertLess(max(roughness) - min(roughness), .06, "fine roughness must not paint high-contrast pigment shadows")
        self.assertGreater(max(roughness) - min(roughness), .005, "roughness is a real independent surface signal")
        with mock.patch.object(self.surface, "albedo_srgb", return_value=(0, 0, 0)):
            self.assertEqual([self.surface.roughness(x, theta) for x, theta in points], roughness,
                             "a color lookup cannot drive the roughness channel")

    def assert_uv_faces(self, surface):
        vertices = [point(-.30, math.tau * v - math.pi / 2) for v in (.98, .02, .04, .10)]
        vertices.extend(point(.58, theta) for theta in (-math.pi / 2, 0, math.pi / 2, math.pi))
        faces = [(0, 1, 2), (2, 1, 0), (1, 2, 3), (4, 5, 6, 7)]
        obj = mesh_stub(vertices, faces)
        before = [v.co for v in obj.data.vertices]
        surface.assign_uv(obj)
        layer = obj.data.uv_layers.active
        self.assertTrue(layer.active_render)
        averages = []
        for face_number, polygon in enumerate(obj.data.polygons):
            actual = [layer.data[i].uv for i in polygon.loop_indices]
            values = [uv[1] for uv in actual]
            self.assertLessEqual(max(values) - min(values), .5, "a face must not interpolate through the whole pigment atlas")
            if face_number != 3:
                for loop_index, uv in zip(polygon.loop_indices, actual):
                    vertex = obj.data.loops[loop_index].vertex_index
                    expected = surface.uv_for(vertices[vertex])
                    self.assertAlmostEqual(uv[0], expected[0], delta=1e-12)
                    difference = (uv[1] - expected[1]) % 1
                    self.assertLess(min(difference, 1 - difference), 1e-12, "unwrapping changes only each loop's integer lift")
                averages.append(sum(values) / len(values) % 1)
            else:
                self.assertLess(max(values) - min(values), 1e-12, "the all-white terminal cap collapses its circumference")
                self.assertGreater(min(surface.albedo_srgb(.58, math.tau * values[0] - math.pi / 2)), .70)
        self.assertAlmostEqual(averages[0], (.98 + 1.02 + 1.04) / 3 % 1, delta=1e-12)
        self.assertAlmostEqual(averages[1], averages[0], delta=1e-12, msg="face winding cannot change the sampled pigment")
        self.assertEqual([v.co for v in obj.data.vertices], before, "UV assignment leaves geometry untouched")

    def test_native_uv_axes_insets_per_corner_seam_and_cap(self):
        for theta, expected in ((-math.pi / 2, 0), (0, .25), (math.pi / 2, .5), (math.pi, .75)):
            for cycle in (-2, 0, 3):
                u, v = self.surface.uv_for(point(-.1, theta + cycle * math.tau))
                self.assertGreaterEqual(u, 2 / 96)
                self.assertLessEqual(u, 1 - 2 / 96)
                difference = (v - expected) % 1
                self.assertLess(min(difference, 1 - difference), 1e-12)
        self.assertAlmostEqual(self.surface.uv_for(point(-2, 0))[0], 2 / 96)
        self.assertAlmostEqual(self.surface.uv_for(point(2, 0))[0], 1 - 2 / 96)
        self.assert_uv_faces(self.surface)

    def test_actual_rostral_chart_stays_regular_at_the_body_chart_singularity(self):
        # The real collar's shifted centre crosses the body's extrapolated
        # centre near these X values. Reusing the body chart loses its angle.
        body_profile = lambda x: (.0245, .025, .025, -.03335)
        surface = SURFACE.SkinSurface(body_profile, size=(96, 192))
        xs = (-.660, -.671, -.67125, -.6715, -.672)
        sections = 128
        vertices, parameters = [], []
        for x in xs:
            width, upper, lower, center = FACE.rostral_profile(x)
            for i in range(sections + 1):
                theta = math.tau * i / sections
                radius = upper if math.sin(theta) >= 0 else lower
                vertices.append((x, width * math.cos(theta), center + radius * math.sin(theta)))
                parameters.append(((theta + math.pi / 2) / math.tau) % 1)
        faces = []
        for row in range(len(xs) - 1):
            for i in range(sections):
                a = row * (sections + 1) + i
                faces.append((a, a + 1, a + sections + 2, a + sections + 1))
        obj = mesh_stub(vertices, faces)
        obj.material_chart = "rostral"
        result = surface.assign_uv(obj)
        self.assertLessEqual(result["maximum_face_v_span"], .5)
        layer = obj.data.uv_layers.active
        for polygon in obj.data.polygons:
            values = []
            for loop_index in polygon.loop_indices:
                vertex = obj.data.loops[loop_index].vertex_index
                value = layer.data[loop_index].uv[1]
                difference = (value - parameters[vertex]) % 1
                self.assertLess(min(difference, 1 - difference), 1e-11,
                                "outer collar material retains its true circumferential parameter")
                values.append(value)
                if vertex < sections + 1:
                    body_v = surface.uv_for(vertices[vertex])[1]
                    difference = (value - body_v) % 1
                    self.assertLess(min(difference, 1 - difference), 1e-11,
                                    "the proximal collar and adjoining body use the same parameter")
            self.assertLessEqual(max(values) - min(values), .5)
        # Confirm the fixture exercises the real near-singular mis-charting.
        vertex = 3 * (sections + 1) + sections // 4
        wrong_v = surface.uv_for(vertices[vertex])[1]
        difference = (wrong_v - parameters[vertex]) % 1
        self.assertGreater(min(difference, 1 - difference), .2)
        # The extracted profile preserves the existing collar geometry.
        for i in range(25):
            u = i / 24
            x = -.660 - .012 * u
            width, upper, lower, center = FACE.rostral_profile(x)
            self.assertAlmostEqual(width, .0245 - .0005 * u, delta=1e-16)
            self.assertAlmostEqual(upper, .025 - .0175 * u + .0003 * math.sin(math.pi * u), delta=1e-16)
            self.assertEqual(upper, lower)
            self.assertAlmostEqual(center, -.03335 - .00965 * u, delta=1e-16)

    def assert_c2_onset(self, onset):
        for theta in (0, .6, math.pi / 2, math.pi, 3 * math.pi / 2):
            for spacing in (.01175, .0235, .047):
                low, high = -.6, 0
                for _ in range(60):
                    middle = (low + high) / 2
                    if onset(middle, theta, spacing) < .5:
                        low = middle
                    else:
                        high = middle
                middle = (low + high) / 2
                self.assertEqual(onset(middle - 2 * spacing, theta, spacing), 0)
                self.assertEqual(onset(middle + 2 * spacing, theta, spacing), 1)
                samples = [onset(middle + spacing * k / 20, theta, spacing) for k in range(-30, 31)]
                self.assertTrue(all(-1e-12 <= v <= 1 + 1e-12 for v in samples), "fade remains bounded to floating point precision")
                self.assertTrue(all(a <= b + 1e-13 for a, b in zip(samples, samples[1:])))
                for endpoint in (middle - spacing, middle + spacing):
                    residuals = []
                    width = 2 * spacing
                    for divisor in (64, 128, 256):
                        h = width / divisor
                        a, b, c = (onset(endpoint + offset, theta, spacing) for offset in (-h, 0, h))
                        slope = width * abs(c - a) / (2 * h)
                        curvature = width * width * abs(c - 2 * b + a) / (h * h)
                        residuals.append((slope, curvature))
                    for previous, current in zip(residuals, residuals[1:]):
                        self.assertLess(current[0], previous[0] * .60 + 1e-8, "fade slope tends to zero at the rest join")
                        self.assertLess(current[1], previous[1] * .60 + 1e-6, "fade curvature tends to zero at the rest join")
                self.assertAlmostEqual(onset(middle, theta, spacing), onset(middle, theta + math.tau, spacing), delta=1e-12)

    def test_scale_emergence_has_periodic_c2_value_slope_and_curvature(self):
        self.assert_c2_onset(SURFACE.scale_onset)

    def test_center_and_rim_offsets_emerge_from_buried_c2_attachments(self):
        burial = .00035
        for full_lift in (.0008, .00025):
            for theta in (0, .7, math.pi / 2, math.pi):
                self.assertEqual(SURFACE.scale_offset(-.7, theta, full_lift), -burial,
                                 "early center and rim material stay below the dermis")
                self.assertAlmostEqual(SURFACE.scale_offset(.2, theta, full_lift), full_lift, delta=1e-15,
                                       msg="fully emerged material retains its authored center or rim height")

            def normalized_offset(x, theta, spacing):
                offset = SURFACE.scale_offset(x, theta, full_lift, row_spacing=spacing, burial=burial)
                return (offset + burial) / (full_lift + burial)

            self.assert_c2_onset(normalized_offset)

    def test_written_rgb_atlases_and_actual_mip_footprints_have_no_cutout_bleed(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = self.surface.write_atlases(directory)
            self.assertEqual(metadata["size"], [96, 192])
            color, rough = read_rgb_png(metadata["albedo"]), read_rgb_png(metadata["roughness"])
            for rows in (color, rough):
                self.assertEqual((len(rows[0]), len(rows)), (96, 192))
                self.assertEqual(rows[0], rows[-1], "actual encoded angular endpoints agree")
                for _ in range(4):
                    maximum = max(abs(a - b) for top, bottom in zip(rows[0], rows[-1]) for a, b in zip(top, bottom))
                    self.assertLessEqual(maximum, 3, "actual 2x2 mip footprints remain continuous across the ventral seam")
                    rows = mip(rows)
            self.assertGreater(min(channel for row in color for pixel in row for channel in pixel), 6, "opaque color contains no black transparent-margin RGB")
            self.assertGreater(min(channel for row in (color[0], color[-1]) for pixel in row for channel in pixel), 178,
                               "white belly gutters remain bright before mip averaging")
            self.assertTrue(all(r == g == b for row in rough for r, g, b in row), "roughness PNG is an independent linear scalar")
            for column, row in ((17, 29), (46, 83), (74, 149)):
                x = self.surface.x_bounds[0] + (self.surface.x_bounds[1] - self.surface.x_bounds[0]) * column / 95
                theta = math.tau * (1 - row / 191) - math.pi / 2
                expected = self.surface.albedo_srgb(x, theta)
                self.assertEqual(color[row][column], tuple(round(c * 255) for c in expected), "atlas stores the declared sRGB pigment without a second transfer")
                self.assertEqual(rough[row][column][1], round(self.surface.roughness(x, theta) * 255))

    def test_missing_optional_numpy_uses_the_scalar_field(self):
        surface = SURFACE.SkinSurface(profile_at, size=(32, 16))
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(sys.modules, {"numpy": None}), \
                mock.patch.object(surface, "_sample_row_numpy", side_effect=AssertionError("NumPy path must not run")), \
                mock.patch.object(surface, "_sample", wraps=surface._sample) as sample:
            metadata = surface.write_atlases(directory)
            self.assertEqual(sample.call_count, 32 * 16)
            color = read_rgb_png(metadata["albedo"])
            for row, column in ((0, 0), (7, 13), (15, 31)):
                x = surface.x_bounds[0] + (surface.x_bounds[1] - surface.x_bounds[0]) * column / 31
                theta = math.tau * (1 - row / 15) - math.pi / 2
                expected = surface.albedo_srgb(x, theta)
                self.assertEqual(color[row][column], tuple(round(c * 255) for c in expected))

    def test_numpy_field_and_actual_pngs_match_the_scalar_fallback(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("Optional authoring NumPy is not installed")
        surface = SURFACE.SkinSurface(profile_at, size=(192, 128))
        xs = np.asarray([surface.x_bounds[0] + (surface.x_bounds[1] - surface.x_bounds[0]) * i / 4095
                         for i in range(4096)], dtype=np.float64)
        before = xs.copy()
        rng = random.Random(72151)
        angles = [-math.pi / 2, math.pi / 2, math.pi * 1.5,
                  -math.pi / 2 + 1e-10, math.pi * 1.5 - 1e-10]
        angles.extend(rng.uniform(-math.pi / 2, math.pi * 1.5) for _ in range(11))

        def assert_row_parity(sampler):
            for theta in angles:
                angular = surface._angular_pattern(theta)
                expected = [surface._sample(float(x), theta, angular) for x in xs]
                actual_rgb, actual_rough = sampler(xs, theta, np)
                for actual, reference in ((actual_rgb, np.asarray([pair[0] for pair in expected])),
                                          (actual_rough, np.asarray([pair[1] for pair in expected]))):
                    self.assertLessEqual(float(np.max(np.abs(actual - reference))), 1e-12)
                    self.assertTrue(np.array_equal(np.rint(actual * 255), np.rint(reference * 255)),
                                    "nearest-even encoded channels must agree with the scalar field")

        assert_row_parity(surface._sample_row_numpy)
        self.assertTrue(np.array_equal(xs, before), "row evaluation must not mutate coordinates")
        # Reject a coherent but changed pigment field, not merely array shape.
        def changed_row(x, theta, numpy):
            rgb, rough = surface._sample_row_numpy(x, theta, numpy)
            return rgb + .00001, rough
        with self.assertRaises(AssertionError):
            assert_row_parity(changed_row)

        with tempfile.TemporaryDirectory() as directory:
            fast = surface.write_atlases(Path(directory) / "numpy")
            with mock.patch.dict(sys.modules, {"numpy": None}):
                scalar = surface.write_atlases(Path(directory) / "scalar")
            for name in ("albedo", "roughness"):
                self.assertEqual(read_rgb_png(fast[name]), read_rgb_png(scalar[name]))
                self.assertEqual(Path(fast[name]).read_bytes(), Path(scalar[name]).read_bytes(),
                                 "row batching preserves compression, filtering, and every encoded pixel")
                self.assertEqual(fast[name + "_sha256"], scalar[name + "_sha256"])

    def test_actual_uv_unwrap_and_c2_guards_reject_in_memory_mutants(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        mutations = (("unwrap", source.replace("layer.data[index].uv = (u, v)", "layer.data[index].uv = (u, v % 1)")),
                     ("cubic", source.replace("return t*t*t*(10+t*(-15+6*t))", "return t*t*(3-2*t)")))
        for name, changed in mutations:
            self.assertTrue(changed != source, "mutation injector applies its fault")
            module = types.ModuleType("skin_surface_" + name)
            exec(compile(changed, str(MODULE_PATH), "exec"), module.__dict__)
            with self.assertRaises(AssertionError, msg="the behavioral oracle rejects the actual module fault"):
                if name == "unwrap":
                    self.assert_uv_faces(module.SkinSurface(profile_at, size=(96, 192)))
                else:
                    self.assert_c2_onset(module.scale_onset)
