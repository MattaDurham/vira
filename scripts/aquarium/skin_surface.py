"""Continuous, opaque koi skin coordinates and unlit PBR pigment maps.

The parameterization follows the authored body profile, not a side photograph.
Its periodic seam is on the white ventral surface. Pigment is an authored
Kohaku pattern; no photograph, scale outline, reflection or shadow is baked in.
Only build_material/assign_uv require Blender. The numerical surface and PNG
writer have a standard-library path; authoring uses optional NumPy row batches
when available, without changing the pigment field or encoded pixels.
"""
import binascii
import hashlib
import math
import struct
import zlib
from pathlib import Path


TAU = math.tau
PBR_DEFAULTS = {"ior": 1.04, "coat": .08, "coat_roughness": .35,
                "subsurface": .08, "metallic": 0.0, "specular": .6}


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def _smooth(value):
    t = _clamp(value)
    return t*t*t*(10+t*(-15+6*t))


def scale_onset(x, theta, row_spacing=.0235):
    """Authored C2 emergence of real scale relief over two material rows.

    A small periodic angular offset and stagger avoid one raised collar. This
    is an authoring transition, not a measured boundary of a specimen's scales.
    Use this on BOTH the scale's central lift and edge/rim lift. scale_offset
    grows each from a shallow buried attachment, avoiding coincident triangles;
    changing only color would leave the geometric collar in place.
    """
    if not all(math.isfinite(v) for v in (x, theta, row_spacing)) or row_spacing <= 0:
        raise ValueError("Scale emergence needs finite coordinates and positive row spacing")
    start = -.386 + .0045*math.sin(theta+.4) + .0025*math.sin(3*theta-.7)
    return _smooth((x-start)/(2*row_spacing))


def scale_offset(x, theta, full_lift, row_spacing=.0235, burial=.00035):
    """Grow a plate out of the dermis, rather than z-fighting at zero lift."""
    if not math.isfinite(full_lift) or full_lift < 0 or not math.isfinite(burial) or burial <= 0:
        raise ValueError("Scale lift and buried attachment must be finite and nonnegative")
    return -burial+(full_lift+burial)*scale_onset(x, theta, row_spacing)


def _chunk(stream, kind, data):
    stream.write(struct.pack(">I", len(data)))
    stream.write(kind)
    stream.write(data)
    stream.write(struct.pack(">I", binascii.crc32(kind+data) & 0xffffffff))


class SkinSurface:
    """profile_at(x) returns width, upper radius, lower radius, centre height.

    Native authoring axes are X longitudinal, Y lateral and Z dorsal. Material
    parameters survive a width/profile change: the same x/theta identifies the
    same pigment on the body, overlapping scales and facial dermis.
    """
    def __init__(self, profile_at, x_bounds=(-.676, .58), size=(4096, 2048)):
        if not callable(profile_at) or len(x_bounds) != 2 or not all(map(math.isfinite, x_bounds)) or x_bounds[0] >= x_bounds[1]:
            raise ValueError("Skin surface needs a profile callback and increasing finite bounds")
        if len(size) != 2 or any(not isinstance(n, int) or n < 16 for n in size):
            raise ValueError("Skin atlas dimensions must be integers of at least 16 pixels")
        self.profile_at = profile_at
        self.x_bounds = tuple(x_bounds)
        self.size = tuple(size)

    def uv_for(self, point):
        return self._uv_with_profile(point, self.profile_at)

    def _uv_with_profile(self, point, profile_at):
        x, lateral, height = point
        if not all(map(math.isfinite, (x, lateral, height))):
            raise ValueError("Skin coordinates must be finite")
        width, upper, lower, center = profile_at(x)
        radius = upper if height >= center else lower
        if not all(map(math.isfinite, (width, radius, center))) or width <= 0 or radius <= 0:
            raise ValueError("Skin profile must provide finite positive radii")
        theta = math.atan2((height-center)/radius, lateral/width)
        u = _clamp((x-self.x_bounds[0])/(self.x_bounds[1]-self.x_bounds[0]))
        # Prevent a terminal U sample from wrapping through the other pole.
        # Both atlas ends also converge to the same white, rough surface.
        inset = 2/self.size[0]
        return _clamp(u, inset, 1-inset), ((theta+math.pi/2)/TAU) % 1

    def _angular_pattern(self, theta):
        dorsal = math.sin(theta)
        lateral = math.cos(theta)
        angle = math.atan2(lateral, dorsal)
        contours = []
        # Independent lobes/notches and unequal flank reaches replace the
        # former elliptical caps. These are authored pigment contours, not a
        # copied champion pattern or measured universal Kohaku proportions.
        for front, rear, low, high, phase, dent, notch, lobe, sashi in (
                (-.592, -.322, -1.85, 2.05, .2, -.55, .72, -1.05, .010),
                (-.244, .062, -2.30, 1.45, 1.1, .65, -.8, .4, .014),
                (.143, .365, -1.40, 1.90, 2.3, -.25, .95, -.7, .008)):
            front += .014*math.sin(1.6*angle+phase) + .020*math.exp(-((angle-dent)/.30)**2)
            front -= .008*math.exp(-((angle+lobe)/.24)**2)
            rear += .018*math.sin(1.25*angle-phase) - .028*math.exp(-((angle-notch)/.26)**2)
            rear += .014*math.exp(-((angle-lobe)/.35)**2)
            contours.append((front, rear, low, high, phase, sashi))
        return dorsal, lateral, angle, contours

    def _sample(self, x, theta, angular=None):
        dorsal, lateral, angle, contours = angular or self._angular_pattern(theta)
        emergence = scale_onset(x, theta)
        row = (x+.37)/.0235
        lattice = (.55*math.sin(TAU*row+.5*math.sin(13*theta))
                   + .30*math.sin(38*theta+.65*math.sin(math.pi*row))
                   + .15*math.sin(17*theta-3.1*row))
        scallop = (.00045+.0021*emergence)*lattice + .0008*math.sin(73*x+7*theta)
        union = 0.0
        for front, rear, low, high, phase, sashi in contours:
            low += .080*math.sin(43*x+phase) + .035*math.sin(97*x-phase)
            high += .090*math.sin(31*x-phase) + .040*math.sin(71*x+phase)
            close = _smooth((angle-low)/.40)*_smooth((high-angle)/.40)
            center = (front+rear)*.5
            half = (rear-front)*.5*(.04+.96*close)
            d_front = x-(center-half)+.45*scallop
            d_rear = center+half-x+scallop
            d_side = .13*min(angle-low, high-angle)+.55*scallop
            core = _smooth((d_front+.00125)/.0025)
            # Sashi is faint anterior underlap, not blur around every edge.
            leading = core+(1-core)*.16*_smooth((d_front+sashi)/sashi)
            mask = leading*_smooth((d_rear+.001)/.002)*_smooth((d_side+.0011)/.0022)
            union = 1-(1-union)*(1-mask)
        # White belly and matching pole gutters make each mip meaningful.
        edge = _smooth((x-self.x_bounds[0])/.025)*_smooth((self.x_bounds[1]-x)/.025)
        pigment = union*edge
        # Dense hi remains consistent: small correlated chroma variation at
        # broad, scale and fine lengths, without pale holes or dark scale grids.
        broad_chroma = (.52*math.sin(31*x+3*theta)
                        + .30*math.sin(67*x-5*theta+.5*math.sin(7*theta))
                        + .18*math.sin(109*x+11*theta))
        scale_grain = (.6*math.sin(267*x+19*theta+math.sin(7*theta)+2.4*math.sin(67*x-7*theta))
                       + .4*math.sin(381*x-31*theta+2*math.sin(103*x+17*theta)))
        fine = .5*math.sin(4319*x+211*theta)*math.sin(3127*x-173*theta)
        warm = .55*math.sin(23*x+3*theta)+.30*math.sin(61*x-7*theta)+.15*math.sin(131*x+11*theta)
        thin = math.exp(-((x+.57)/.12)**2)
        ivory = (.87+edge*(.010*warm+.004*scale_grain+.004*fine),
                 .852+edge*(.006*warm+.003*scale_grain+.002*fine-.006*thin),
                 .80+edge*(.003*warm+.002*scale_grain+.002*fine-.014*thin))
        red = (.81+edge*(.022*broad_chroma+.009*scale_grain+.006*fine),
               .135+edge*(.014*broad_chroma+.006*scale_grain+.003*fine),
               .043+edge*(.004*broad_chroma+.002*scale_grain+.001*fine))
        rgb = tuple(_clamp(a+(b-a)*pigment) for a, b in zip(ivory, red))
        # Separate fine wet-surface variation. It is not a scale-shadow atlas.
        micro = .010*math.sin(x*1037+17*lateral)*math.sin(127*theta-193*x)
        broad = .006*math.sin(71*x+7*theta)*math.sin(19*x-11*theta)
        roughness = .37 + edge*(-.012+.014*emergence+micro+broad)
        return rgb, _clamp(roughness, .33, .41)

    def albedo_srgb(self, x, theta):
        return self._sample(x, theta)[0]

    def roughness(self, x, theta):
        return self._sample(x, theta)[1]

    def _sample_row_numpy(self, xs, theta, np):
        """Batch _sample in float64; the scalar path remains the field oracle.

        NumPy is supplied only by the authoring writer. Preserve operation
        order and Python's nearest-even byte rounding: parity tests compare
        both the field and complete PNG bytes against the stdlib fallback.
        """
        x = np.asarray(xs, dtype=np.float64)
        if x.ndim != 1:
            raise ValueError("Skin row coordinates must be one-dimensional")
        dorsal, lateral, angle, contours = self._angular_pattern(theta)
        sin = np.sin

        def smooth(value):
            t = np.clip(value, 0.0, 1.0)
            return t*t*t*(10+t*(-15+6*t))

        start = -.386 + .0045*math.sin(theta+.4) + .0025*math.sin(3*theta-.7)
        emergence = smooth((x-start)/(2*.0235))
        row = (x+.37)/.0235
        lattice = (.55*sin(TAU*row+.5*math.sin(13*theta))
                   + .30*sin(38*theta+.65*sin(math.pi*row))
                   + .15*sin(17*theta-3.1*row))
        scallop = (.00045+.0021*emergence)*lattice + .0008*sin(73*x+7*theta)
        union = 0.0
        for front, rear, low, high, phase, sashi in contours:
            low = low + (.080*sin(43*x+phase) + .035*sin(97*x-phase))
            high = high + (.090*sin(31*x-phase) + .040*sin(71*x+phase))
            close = smooth((angle-low)/.40)*smooth((high-angle)/.40)
            center = (front+rear)*.5
            half = (rear-front)*.5*(.04+.96*close)
            d_front = x-(center-half)+.45*scallop
            d_rear = center+half-x+scallop
            d_side = .13*np.minimum(angle-low, high-angle)+.55*scallop
            core = smooth((d_front+.00125)/.0025)
            leading = core+(1-core)*.16*smooth((d_front+sashi)/sashi)
            mask = leading*smooth((d_rear+.001)/.002)*smooth((d_side+.0011)/.0022)
            union = 1-(1-union)*(1-mask)
        edge = smooth((x-self.x_bounds[0])/.025)*smooth((self.x_bounds[1]-x)/.025)
        pigment = union*edge
        broad_chroma = (.52*sin(31*x+3*theta)
                        + .30*sin(67*x-5*theta+.5*math.sin(7*theta))
                        + .18*sin(109*x+11*theta))
        scale_grain = (.6*sin(267*x+19*theta+math.sin(7*theta)+2.4*sin(67*x-7*theta))
                       + .4*sin(381*x-31*theta+2*sin(103*x+17*theta)))
        fine = .5*sin(4319*x+211*theta)*sin(3127*x-173*theta)
        warm = .55*sin(23*x+3*theta)+.30*sin(61*x-7*theta)+.15*sin(131*x+11*theta)
        thin = np.exp(-((x+.57)/.12)**2)
        ivory = (.87+edge*(.010*warm+.004*scale_grain+.004*fine),
                 .852+edge*(.006*warm+.003*scale_grain+.002*fine-.006*thin),
                 .80+edge*(.003*warm+.002*scale_grain+.002*fine-.014*thin))
        red = (.81+edge*(.022*broad_chroma+.009*scale_grain+.006*fine),
               .135+edge*(.014*broad_chroma+.006*scale_grain+.003*fine),
               .043+edge*(.004*broad_chroma+.002*scale_grain+.001*fine))
        rgb = np.stack(tuple(np.clip(a+(b-a)*pigment, 0.0, 1.0)
                             for a, b in zip(ivory, red)), axis=1)
        micro = .010*sin(x*1037+17*lateral)*sin(127*theta-193*x)
        broad = .006*sin(71*x+7*theta)*sin(19*x-11*theta)
        roughness = .37 + edge*(-.012+.014*emergence+micro+broad)
        return rgb, np.clip(roughness, .33, .41)

    def assign_uv(self, obj):
        """Set active per-corner UVs, unwrapping only seam-crossing faces.

        The corresponding image sampler repeats V. The all-white final cap
        can use one V coordinate instead of interpolating around an entire
        circumference. Existing geometry, normals and weights remain untouched.
        """
        data = obj.data
        layer = data.uv_layers.get("Continuous skin surface") or data.uv_layers.new(name="Continuous skin surface")
        data.uv_layers.active = layer
        layer.active_render = True
        chart = obj.get("material_chart") if hasattr(obj, "get") else None
        profile_at = self.profile_at
        if chart == "rostral":
            # The oral collar has a moving local centre and radius; the body
            # chart crosses a radial singularity inside that conical surface.
            try:
                from .face import rostral_profile
            except ImportError:
                from face import rostral_profile
            profile_at = rostral_profile
        elif chart is not None:
            raise ValueError("Unknown skin material chart: "+str(chart))
        cache = [self._uv_with_profile(obj.matrix_world @ v.co, profile_at) for v in data.vertices]
        seam_faces = 0
        maximum_span = 0.0
        for poly in data.polygons:
            indices = list(poly.loop_indices)
            values = [cache[data.loops[i].vertex_index] for i in indices]
            cap = all(abs(u-(1-2/self.size[0])) < 1e-8 for u, _ in values)
            vs = [v for _, v in values]
            if cap:
                vs = [.5]*len(vs)
            else:
                ordered = sorted(set(vs))
                gaps = [ordered[(i+1) % len(ordered)]-v+(1 if i == len(ordered)-1 else 0)
                        for i, v in enumerate(ordered)]
                cut = ordered[(max(range(len(gaps)), key=gaps.__getitem__)+1) % len(ordered)]
                unwrapped = [v+1 if v < cut-1e-12 else v for v in vs]
                if any(abs(a-b) > .5 for a, b in zip(vs, unwrapped)):
                    seam_faces += 1
                vs = unwrapped
            maximum_span = max(maximum_span, max(vs)-min(vs))
            for index, (u, _), v in zip(indices, values, vs):
                layer.data[index].uv = (u, v)
        return {"seam_faces": seam_faces, "maximum_face_v_span": maximum_span,
                "seam": "white ventral surface", "coordinates": "rest longitudinal x and profile-normalized circumference"}

    def write_atlases(self, output_dir, basename="koi-skin"):
        """Write fully opaque RGB PNGs; no cutout RGB or alpha is present."""
        # Blender bundles NumPy, but it is not a server/test dependency.
        # Import only while authoring; missing NumPy keeps the stdlib writer.
        try:
            import numpy as np
        except ImportError:
            np = None
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = {"albedo": output_dir/(basename+"-albedo.png"),
                 "roughness": output_dir/(basename+"-roughness.png")}
        width, height = self.size
        xs = None if np is None else np.asarray([
            self.x_bounds[0]+(self.x_bounds[1]-self.x_bounds[0])*column/(width-1)
            for column in range(width)], dtype=np.float64)
        compressors = {name: zlib.compressobj(6) for name in paths}
        streams = {name: path.open("wb") for name, path in paths.items()}
        try:
            for stream in streams.values():
                stream.write(b"\x89PNG\r\n\x1a\n")
                _chunk(stream, b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            for row in range(height):
                # PNG top is V=1; Blender's image/UV convention has V=0 below.
                theta = TAU*(1-row/(height-1))-math.pi/2
                if np is None:
                    angular = self._angular_pattern(theta)
                    colors, rough = bytearray([0]), bytearray([0])
                    for column in range(width):
                        x = self.x_bounds[0]+(self.x_bounds[1]-self.x_bounds[0])*column/(width-1)
                        rgb, value = self._sample(x, theta, angular)
                        colors.extend(round(c*255) for c in rgb)
                        r = round(value*255)
                        rough.extend((r, r, r))
                else:
                    rgb, value = self._sample_row_numpy(xs, theta, np)
                    colors = b"\0"+np.rint(rgb*255).astype(np.uint8).tobytes()
                    rough = b"\0"+np.repeat(np.rint(value*255).astype(np.uint8), 3).tobytes()
                for name, pixels in (("albedo", colors), ("roughness", rough)):
                    block = compressors[name].compress(pixels)
                    if block:
                        _chunk(streams[name], b"IDAT", block)
            for name, stream in streams.items():
                _chunk(stream, b"IDAT", compressors[name].flush())
                _chunk(stream, b"IEND", b"")
        finally:
            for stream in streams.values():
                stream.close()
        return {"albedo": str(paths["albedo"]), "roughness": str(paths["roughness"]),
                "size": list(self.size), "channels": "opaque RGB; albedo sRGB, roughness non-color",
                "albedo_sha256": hashlib.sha256(paths["albedo"].read_bytes()).hexdigest(),
                "roughness_sha256": hashlib.sha256(paths["roughness"].read_bytes()).hexdigest(),
                "pbr": dict(PBR_DEFAULTS), "pigment": "authored Kohaku islands, no baked illumination or scale outlines"}

    def build_material(self, output_dir, basename="koi-skin", name="Pearl skin"):
        """Return (shared Blender material, provenance) before GLB export.

        The named body material is reused when present. This avoids a .001
        suffix breaking the runtime's skin material classification. Both maps
        are saved first, then packed; loading the GLB needs no external paths.
        """
        import bpy
        metadata = self.write_atlases(output_dir, basename)
        material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
        material.use_nodes = True
        nodes, links = material.node_tree.nodes, material.node_tree.links
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
        bsdf.inputs["Metallic"].default_value = PBR_DEFAULTS["metallic"]
        bsdf.inputs["IOR"].default_value = PBR_DEFAULTS["ior"]
        bsdf.inputs["Coat Weight"].default_value = PBR_DEFAULTS["coat"]
        bsdf.inputs["Coat Roughness"].default_value = PBR_DEFAULTS["coat_roughness"]
        bsdf.inputs["Subsurface Weight"].default_value = PBR_DEFAULTS["subsurface"]
        # Nondefault specular exports KHR_materials_specular, which also makes
        # Blender preserve the nondefault IOR instead of silently omitting it.
        bsdf.inputs["Specular IOR Level"].default_value = .5*PBR_DEFAULTS["specular"]
        bsdf.inputs["Alpha"].default_value = 1
        for key, color_space, socket in (("albedo", "sRGB", "Base Color"), ("roughness", "Non-Color", "Roughness")):
            image = bpy.data.images.load(metadata[key], check_existing=False)
            image.colorspace_settings.name = color_space
            image.pack()
            node = nodes.new("ShaderNodeTexImage")
            node.image = image
            node.extension = "REPEAT"
            node.interpolation = "Linear"
            if key == "roughness":
                split = nodes.new("ShaderNodeSeparateColor")
                split.mode = "RGB"
                links.new(node.outputs["Color"], split.inputs["Color"])
                links.new(split.outputs["Green"], bsdf.inputs[socket])
            else:
                links.new(node.outputs["Color"], bsdf.inputs[socket])
        metadata["material"] = material.name
        metadata["uv"] = "Continuous skin surface; periodic V seam on white belly"
        # Build provenance may be published. Absolute authoring paths belong
        # only to image loading, never to the public material metadata.
        metadata["albedo"] = Path(metadata["albedo"]).name
        metadata["roughness"] = Path(metadata["roughness"]).name
        return material, metadata
