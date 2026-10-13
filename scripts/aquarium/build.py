"""Author and render the aquarium benchmark with Blender (no paid assets).

Run: blender --background --factory-startup --python scripts/aquarium/build.py
The glTF contains one rigged, metre-scaled koi. Cycles renders the same asset.
Optional arguments after --: --draft, --no-render, --cpu, --samples N, --output DIR.
Rendering also needs cwebp to encode the browser's WebP comparison asset.
"""

import argparse
import json
import math
import random
import shutil
import subprocess
import sys
from pathlib import Path


def build():
    # Blender owns these modules; the ordinary Vira runtime does not need them.
    import bpy
    from mathutils import Vector

    parser = argparse.ArgumentParser()
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--samples", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    if args.samples is not None and args.samples < 1:
        parser.error("--samples must be positive")
    encoder = shutil.which("cwebp")
    if not args.no_render and not encoder:
        parser.error("Rendering requires cwebp on PATH (install the webp package)")
    root = Path(__file__).resolve().parents[2]
    output = args.output or root / "static" / "aquarium"
    output.mkdir(parents=True, exist_ok=True)
    work = root / ".aquarium-build"
    work.mkdir(exist_ok=True)
    rng = random.Random(913)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.render.engine = "CYCLES"
    scene.cycles.samples = args.samples or (32 if args.draft else 96)
    scene.cycles.use_denoising = True
    scene.cycles.max_bounces = 12
    scene.cycles.transmission_bounces = 8
    if not args.cpu:
        try:
            prefs = bpy.context.preferences.addons["cycles"].preferences
            prefs.compute_device_type = "METAL" if sys.platform == "darwin" else "CUDA"
            prefs.get_devices()
            gpu = False
            for device in prefs.devices:
                device.use = device.type != "CPU"
                gpu |= device.use
            if gpu:
                scene.cycles.device = "GPU"
                print("AQUARIUM: Cycles GPU selected", flush=True)
        except (TypeError, RuntimeError):
            print("AQUARIUM: using Cycles CPU", flush=True)
    scene.render.resolution_x = 1280 if args.draft else 3840
    scene.render.resolution_y = 720 if args.draft else 2160
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.look = "AgX - Medium High Contrast"
    scene.view_settings.exposure = -0.25
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs[0].default_value = (0.025, 0.045, 0.065, 1)
    scene.world.node_tree.nodes["Background"].inputs[1].default_value = 0.23

    def material(name, color, rough=0.3, metal=0, transmission=0):
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        node = mat.node_tree.nodes.get("Principled BSDF")
        node.inputs["Base Color"].default_value = (*color, 1)
        node.inputs["Roughness"].default_value = rough
        node.inputs["Metallic"].default_value = metal
        node.inputs["Transmission Weight"].default_value = transmission
        node.inputs["IOR"].default_value = 1.38
        node.inputs["Coat Weight"].default_value = 0.45
        node.inputs["Coat Roughness"].default_value = 0.12
        return mat

    ivory = material("Pearl skin", (0.70, 0.72, 0.65), 0.27)
    red = material("Vermilion pigment", (0.65, 0.055, 0.012), 0.29)
    scale_materials = []
    for family, base in [("pearl", (0.73, 0.76, 0.69)), ("vermilion", (0.68, 0.067, 0.019))]:
        for i, value in enumerate([0.94, 0.97, 1.0, 1.025, 1.05]):
            mat = material(f"{family} scale {i}", tuple(min(0.95, c * value) for c in base), 0.24 + i * 0.012)
            mat.node_tree.nodes["Principled BSDF"].inputs["Subsurface Weight"].default_value = 0.06
            scale_materials.append(mat)
    fin = material("Ivory fin membrane", (0.42, 0.52, 0.44), 0.34, transmission=0.65)
    fin.node_tree.nodes["Principled BSDF"].inputs["Subsurface Weight"].default_value = 0.16
    fin.node_tree.nodes["Principled BSDF"].inputs["Alpha"].default_value = 0.72
    fin.diffuse_color = (0.42, 0.52, 0.44, 0.72)
    fin_ray = material("Fin rays", (0.48, 0.55, 0.43), 0.36, transmission=0.25)
    lip = material("Soft lips and barbels", (0.63, 0.58, 0.40), 0.28)
    mouth_inside = material("Mouth opening", (0.055, 0.018, 0.017), 0.4)
    iris = material("Golden iris", (0.29, 0.23, 0.095), 0.23, metal=0.08)
    pupil = material("Black pupil", (0.003, 0.009, 0.012), 0.11)
    cornea = material("Corneal highlight", (0.28, 0.40, 0.38), 0.045, transmission=0.9)

    # Nose at negative X; the body narrows to a real caudal peduncle.
    # (X, lateral half-width, dorsal height, ventral depth, centre height).
    profile = [
        (-0.66, 0.037, 0.036, 0.030, -0.023),
        (-0.62, 0.065, 0.072, 0.050, -0.010),
        (-0.54, 0.088, 0.109, 0.075, 0.012),
        (-0.43, 0.111, 0.141, 0.095, 0.019),
        (-0.30, 0.136, 0.156, 0.124, 0.021),
        (-0.14, 0.137, 0.156, 0.137, 0.015),
        (0.04, 0.119, 0.130, 0.125, 0.007),
        (0.20, 0.090, 0.095, 0.090, 0.004),
        (0.34, 0.054, 0.058, 0.053, 0.008),
        (0.46, 0.025, 0.034, 0.032, 0.013),
        (0.54, 0.021, 0.038, 0.038, 0.015),
    ]

    def interpolate(x):
        for i in range(len(profile) - 1):
            if x <= profile[i + 1][0]:
                a, b = profile[i], profile[i + 1]
                p, q = profile[max(0, i - 1)], profile[min(len(profile) - 1, i + 2)]
                t = max(0, min(1, (x - a[0]) / (b[0] - a[0])))
                # Hermite tangents in metre space keep width continuously smooth.
                values = []
                for k in range(1, 5):
                    ma = (b[k] - p[k]) / (b[0] - p[0]) * (b[0] - a[0])
                    mb = (q[k] - a[k]) / (q[0] - a[0]) * (b[0] - a[0])
                    values.append((2*t**3-3*t*t+1)*a[k] + (t**3-2*t*t+t)*ma + (-2*t**3+3*t*t)*b[k] + (t**3-t*t)*mb)
                return values
        return list(profile[-1][1:])

    def surface(x, angle, offset=0):
        width, upper, lower, center = interpolate(x)
        c, s = math.cos(angle), math.sin(angle)
        radius = upper if s >= 0 else lower
        return Vector((x, (width + offset) * c, center + (radius + offset) * s))

    def pigment(x, angle):
        # Three disconnected, asymmetrical Kohaku patches with scalloped edges.
        a = math.atan2(math.sin(angle), math.cos(angle))
        cap = abs(a - math.pi/2) < 1.25
        first = -0.57 < x < -0.34 + 0.025*math.sin(angle*4)
        middle = -0.24 + 0.04*math.sin(angle*3) < x < 0.04 + 0.025*math.cos(angle*5)
        rear = 0.15 + 0.025*math.cos(angle*4) < x < 0.33 + 0.025*math.sin(angle*5)
        return cap and (first or middle or rear)

    objects = []

    def mesh(name, vertices, faces, materials, indices=None):
        data = bpy.data.meshes.new(name)
        data.from_pydata(vertices, [], faces)
        data.update()
        obj = bpy.data.objects.new(name, data)
        scene.collection.objects.link(obj)
        for mat in materials:
            data.materials.append(mat)
        for i, poly in enumerate(data.polygons):
            poly.use_smooth = True
            if indices:
                poly.material_index = indices[i]
        objects.append(obj)
        return obj

    vertices, faces, mat_indices = [], [], []
    rings, sides = 160, 96
    for i in range(rings + 1):
        x = -0.66 + 1.2*i/rings
        for j in range(sides):
            vertices.append(surface(x, math.tau*j/sides))
    for i in range(rings):
        x = -0.66 + 1.2*(i+0.5)/rings
        for j in range(sides):
            a, b = i*sides+j, i*sides+(j+1)%sides
            faces.append((a, b, b+sides, a+sides))
            mat_indices.append(int(pigment(x, math.tau*(j+0.5)/sides)))
    faces += [tuple(reversed(range(sides))), tuple(rings*sides+j for j in range(sides))]
    mat_indices += [0, 0]
    body = mesh("Koi skin", vertices, faces, [ivory, red], mat_indices)

    # Individual overlapping scales have actual curved geometry; the head is smooth.
    vertices, faces, mat_indices = [], [], []
    rows, columns = 35, 38
    for row in range(rows):
        x = -0.37 + row * 0.0235
        for col in range(columns):
            angle = math.tau * (col + (row % 2)*0.5) / columns
            start = len(vertices)
            vertices.append(surface(x, angle, 0.0008))
            n = 12
            for k in range(n):
                t = math.tau*k/n
                # The exposed edge points toward the tail, with a fine raised lip.
                # Wrap each point onto the curved fish rather than putting a
                # flat disk tangent to it. A sub-millimetre rim catches light.
                point = surface(x+math.cos(t)*0.016,
                                angle+math.sin(t)*math.tau/columns*0.58,
                                0.00025+0.0003*max(0, math.cos(t)))
                vertices.append(point)
            material_id = rng.randrange(5) + (5 if pigment(x, angle) else 0)
            for k in range(n):
                faces.append((start, start+1+(k+1)%n, start+1+k))
                mat_indices.append(material_id)
    mesh("Overlapping scales", vertices, faces, scale_materials, mat_indices)

    def sphere(name, location, dimensions, mat, segments=32):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=segments, ring_count=20, location=location)
        obj = bpy.context.object
        obj.name = name
        obj.scale = dimensions
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        obj.data.materials.append(mat)
        for polygon in obj.data.polygons:
            polygon.use_smooth = True
        objects.append(obj)
        return obj

    def curve(name, points, width, mat):
        data = bpy.data.curves.new(name, "CURVE")
        data.dimensions = "3D"
        data.resolution_u = 8
        data.bevel_depth = width
        data.bevel_resolution = 2
        spline = data.splines.new("BEZIER")
        spline.bezier_points.add(len(points)-1)
        for control, point in zip(spline.bezier_points, points):
            control.co = point
            control.handle_left_type = control.handle_right_type = "AUTO"
        obj = bpy.data.objects.new(name, data)
        scene.collection.objects.link(obj)
        obj.data.materials.append(mat)
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.convert(target="MESH")
        objects.append(bpy.context.object)
        return bpy.context.object

    # Eyes are set in the skull; the layered pupil/cornea catches small highlights.
    for sign in [-1, 1]:
        eye = (-0.525, sign*0.081, 0.055)
        sphere("Eye socket", eye, (0.026, 0.015, 0.024), lip)
        sphere("Iris", (-0.528, sign*0.095, 0.057), (0.018, 0.005, 0.018), iris)
        sphere("Pupil", (-0.531, sign*0.0995, 0.057), (0.010, 0.0035, 0.011), pupil)
        sphere("Cornea", (-0.528, sign*0.101, 0.057), (0.019, 0.005, 0.019), cornea)
        # Gill plate crease and two pairs of sensory barbels.
        curve("Operculum crease", [(-0.395, sign*0.070, 0.115), (-0.36, sign*0.12, 0.035), (-0.375, sign*0.112, -0.050)], 0.0012, lip)
        curve("Mouth barbel", [(-0.63, sign*0.035, -0.035), (-0.675, sign*0.055, -0.045), (-0.70, sign*0.064, -0.080)], 0.002, lip)
        curve("Small barbel", [(-0.60, sign*0.049, -0.021), (-0.645, sign*0.077, -0.035)], 0.0013, lip)
    sphere("Mouth cavity", (-0.661, 0, -0.023), (0.004, 0.019, 0.012), mouth_inside)
    for shift in [-0.009, 0.009]:
        curve("Lip", [(-0.664, -0.020, -0.023), (-0.669, 0, -0.023+shift), (-0.664, 0.020, -0.023)], 0.0038, lip)

    def fin_mesh(name, base, boundary, mat=fin):
        # A fan has many segmented rays and a curved, scalloped membrane.
        base = Vector(base)
        rays, rings = 30, 8
        edges = [Vector(p) for p in boundary]
        vertices, faces = [], []
        for i in range(rays+1):
            t = i/rays*(len(edges)-1)
            segment = min(len(edges)-2, int(t))
            a, b = edges[segment], edges[segment+1]
            p, q = edges[max(0, segment-1)], edges[min(len(edges)-1, segment+2)]
            u = t-segment
            edge = 0.5*((2*a)+(-p+b)*u+(2*p-5*a+4*b-q)*u*u+(-p+3*a-3*b+q)*u*u*u)
            for j in range(rings+1):
                s = j/rings
                point = base.lerp(edge, s)
                point.y += math.sin(s*math.pi)*0.007*math.sin(i/rays*math.pi)
                vertices.append(point)
        for i in range(rays):
            for j in range(rings):
                a = i*(rings+1)+j
                faces.append((a, a+rings+1, a+rings+2, a+1))
        obj = mesh(name, vertices, faces, [mat])
        obj.data.materials[0].use_nodes = True
        for i in range(0, rays+1, 2):
            curve(name+" ray", [vertices[i*(rings+1)+j] for j in [0, 2, 5, 8]], 0.00045, fin_ray)
        return obj

    fin_mesh("Caudal membrane", (0.49, 0, 0.015), [(0.60, 0, 0.055), (0.76, 0, 0.21), (0.82, 0, 0.20), (0.72, 0, 0.045), (0.68, 0, 0.016), (0.73, 0, -0.045), (0.82, 0, -0.18), (0.76, 0, -0.19), (0.59, 0, -0.035)])
    fin_mesh("Dorsal membrane", (-0.18, 0, 0.16), [(-0.32, 0, 0.16), (-0.24, 0, 0.27), (-0.13, 0, 0.255), (0.06, 0, 0.205), (0.27, 0, 0.105)])
    for sign in [-1, 1]:
        fin_mesh("Pectoral membrane", (-0.35, sign*0.105, -0.036), [(-0.28, sign*0.25, -0.12), (-0.20, sign*0.28, -0.14), (-0.11, sign*0.21, -0.13), (-0.15, sign*0.15, -0.07)])
        fin_mesh("Pelvic membrane", (0.0, sign*0.075, -0.11), [(0.055, sign*0.16, -0.17), (0.15, sign*0.12, -0.16), (0.11, sign*0.065, -0.095)])
    fin_mesh("Anal membrane", (0.18, 0, -0.077), [(0.22, 0, -0.18), (0.34, 0, -0.11), (0.32, 0, -0.045)])

    # This is an animation rig, not a claim of a complete veterinary skeleton.
    bpy.ops.object.select_all(action="DESELECT")
    armature_data = bpy.data.armatures.new("Koi swimming rig")
    armature = bpy.data.objects.new("KoiRig", armature_data)
    scene.collection.objects.link(armature)
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="EDIT")
    positions = [-0.67, -0.40, -0.29, -0.18, -0.07, 0.04, 0.15, 0.26, 0.37, 0.48, 0.62, 0.84]
    names = []
    previous = None
    for i in range(len(positions)-1):
        bone = armature_data.edit_bones.new(f"spine_{i:02}")
        bone.head = (positions[i], 0, 0)
        bone.tail = (positions[i+1], 0, 0)
        if previous:
            bone.parent = previous
            bone.use_connect = True
        names.append(bone.name)
        previous = bone
    bpy.ops.object.mode_set(mode="OBJECT")
    for obj in objects:
        modifier = obj.modifiers.new("Swim deformation", "ARMATURE")
        modifier.object = armature
        obj.parent = armature
        groups = [obj.vertex_groups.new(name=name) for name in names]
        for vertex in obj.data.vertices:
            x = (obj.matrix_world @ vertex.co).x
            if x <= -0.43:
                groups[0].add([vertex.index], 1, "REPLACE")
                continue
            centers = [(positions[i]+positions[i+1])/2 for i in range(len(names))]
            nearest = sorted(range(len(names)), key=lambda i: abs(centers[i]-x))[:2]
            distances = [max(0.0001, abs(centers[i]-x)) for i in nearest]
            total = sum(1/d for d in distances)
            for i, d in zip(nearest, distances):
                groups[i].add([vertex.index], (1/d)/total, "REPLACE")
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects+[armature]:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = armature
    # Export the same undeformed asset used by the offline render.
    bpy.ops.export_scene.gltf(filepath=str(output/"koi.glb"), export_format="GLB", use_selection=True,
                              export_animations=False, export_extras=False, export_yup=True,
                              export_apply=False, export_normals=True, export_materials="EXPORT")
    print("AQUARIUM: exported rigged koi", flush=True)

    # Aquarium reference: real geometry, volumetric water and overhead softboxes.
    for obj in objects+[armature]:
        obj.select_set(False)
    armature.location = (0.30, -0.14, 0.77)
    armature.rotation_euler.z = -0.12
    # Slight body wave, not an independent translation of the tail.
    for i, name in enumerate(names):
        bone = armature.pose.bones[name]
        bone.rotation_mode = "XYZ"
        bone.rotation_euler.z = 0 if i == 0 else 0.014*(i/10)**2*math.sin(-i/10*4.0)
    floor_mat = material("Dark river gravel", (0.020, 0.027, 0.032), 0.88)
    rock_mat = material("Water worn stone", (0.026, 0.039, 0.042), 0.69)
    for mat, scale, strength in [(floor_mat, 280, 0.35), (rock_mat, 38, 0.45)]:
        nodes, links = mat.node_tree.nodes, mat.node_tree.links
        noise = nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = scale
        noise.inputs["Detail"].default_value = 3
        bump = nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = strength
        bump.inputs["Distance"].default_value = 0.012
        links.new(noise.outputs["Fac"], bump.inputs["Height"])
        links.new(bump.outputs["Normal"], nodes.get("Principled BSDF").inputs["Normal"])
    back_mat = material("Rear pane", (0.005, 0.018, 0.029), 0.6)
    glass = material("Aquarium glass", (0.91, 0.96, 0.98), 0.02, transmission=1)
    glass.node_tree.nodes["Principled BSDF"].inputs["IOR"].default_value = 1.46

    def cube(name, location, dimensions, mat):
        bpy.ops.mesh.primitive_cube_add(size=1, location=location)
        obj = bpy.context.object
        obj.name = name
        obj.dimensions = dimensions
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        obj.data.materials.append(mat)
        return obj

    cube("Substrate", (0, 0.2, -0.025), (4, 1.8, 0.09), floor_mat)
    cube("Rear glass backing", (0, 1.12, 0.90), (4, 0.02, 1.95), back_mat)
    for i in range(950):
        x, y = rng.uniform(-2, 2), rng.uniform(-0.65, 1.02)
        radius = rng.uniform(0.007, 0.025)
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, radius=radius, location=(x, y, radius*0.45))
        pebble = bpy.context.object
        pebble.scale = (rng.uniform(0.8, 1.3), rng.uniform(0.7, 1.2), rng.uniform(0.4, 0.75))
        pebble.data.materials.append(floor_mat)
    for x, y, r in [(-1.63, 0.39, 0.25), (-1.27, 0.64, 0.13), (1.64, 0.68, 0.28), (1.23, 0.78, 0.15)]:
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=4, radius=r, location=(x, y, r*0.4))
        rock = bpy.context.object
        rock.scale = (1.2, 0.8, 0.73)
        rock.data.materials.append(rock_mat)
        for vertex in rock.data.vertices:
            vertex.co *= 1 + rng.uniform(-0.035, 0.035)
        for polygon in rock.data.polygons:
            polygon.use_smooth = True

    water = bpy.data.materials.new("Water volume - absorption and scattering")
    water.use_nodes = True
    nodes, links = water.node_tree.nodes, water.node_tree.links
    nodes.clear()
    outlet = nodes.new("ShaderNodeOutputMaterial")
    scatter = nodes.new("ShaderNodeVolumeScatter")
    scatter.inputs["Color"].default_value = (0.16, 0.34, 0.38, 1)
    scatter.inputs["Density"].default_value = 0.09
    scatter.inputs["Anisotropy"].default_value = 0.28
    absorption = nodes.new("ShaderNodeVolumeAbsorption")
    absorption.inputs["Color"].default_value = (0.07, 0.38, 0.49, 1)
    absorption.inputs["Density"].default_value = 0.13
    add = nodes.new("ShaderNodeAddShader")
    links.new(scatter.outputs[0], add.inputs[0])
    links.new(absorption.outputs[0], add.inputs[1])
    links.new(add.outputs[0], outlet.inputs["Volume"])
    cube("Water", (0, 0.21, 0.88), (4, 1.75, 1.8), water)
    # Front glass is a thin solid, rather than a transparency overlay.
    cube("Front glass", (0, -0.69, 0.9), (4, 0.012, 1.9), glass)
    surface_mat = material("Water surface", (0.90, 0.98, 1), 0.035, transmission=1)
    surface_mat.node_tree.nodes["Principled BSDF"].inputs["IOR"].default_value = 1.333
    cube("Waterline", (0, 0.20, 1.79), (4, 1.78, 0.004), surface_mat)

    def area(name, location, target, power, size, color):
        data = bpy.data.lights.new(name, "AREA")
        data.energy, data.size, data.color = power, size, color
        data.shape = "RECTANGLE"
        data.size_y = 0.06
        obj = bpy.data.objects.new(name, data)
        scene.collection.objects.link(obj)
        obj.location = location
        obj.rotation_euler = (Vector(target)-obj.location).to_track_quat("-Z", "Y").to_euler()
        return obj

    area("Overhead softbox", (0.45, -0.15, 2.35), (0.2, 0.1, 0.4), 190, 1.3, (0.76, 0.86, 1.0))
    area("Side fill", (-1.7, -0.3, 1.4), (0.2, 0.1, 0.7), 35, 0.9, (0.39, 0.66, 0.80))
    area("Rim strip", (1.0, 0.9, 1.8), (0.2, 0.1, 0.7), 95, 0.55, (0.56, 0.81, 0.90))
    camera_data = bpy.data.cameras.new("Aquarium camera")
    camera = bpy.data.objects.new("Aquarium camera", camera_data)
    scene.collection.objects.link(camera)
    camera.location = (0, -4.1, 1.02)
    camera.rotation_euler = (Vector((0, 0.2, 0.86))-camera.location).to_track_quat("-Z", "Y").to_euler()
    camera_data.lens = 47
    scene.camera = camera
    bpy.ops.wm.save_as_mainfile(filepath=str(work/"aquarium.blend"), compress=True)
    metadata = {
        "generator": f"Blender {bpy.app.version_string}", "asset": "koi.glb",
        "units": "metres", "fish_length": 1.5, "rig_joints": len(names),
        "scale_count": rows*columns, "source": "scripts/aquarium/build.py",
        "license": "Original Vira benchmark asset; repository license applies",
        "reference": {"engine": "Cycles", "samples": scene.cycles.samples,
                      "resolution": [scene.render.resolution_x, scene.render.resolution_y]},
    }
    (output/"manifest.json").write_text(json.dumps(metadata, indent=2)+"\n", encoding="utf-8")
    if not args.no_render:
        reference = work / ("reference-draft.png" if args.draft else "reference.png")
        scene.render.filepath = str(reference)
        print("AQUARIUM: rendering reference", flush=True)
        bpy.ops.render.render(write_still=True)
        subprocess.run([encoder, "-q", "94", "-m", "6", str(reference),
                        "-o", str(output / "reference.webp")],
                       check=True, capture_output=True, timeout=120)
    print("AQUARIUM: complete", flush=True)


if __name__ == "__main__":
    build()
