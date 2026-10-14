"""Author and render the aquarium benchmark with Blender (no paid assets).

Run: blender --background --factory-startup --python scripts/aquarium/build.py
The glTF contains one rigged, metre-scaled koi. Cycles renders the same asset.
Optional arguments after --: --draft, --no-render, --cpu, --samples N, --output DIR.
Rendering also needs cwebp to encode the browser's WebP comparison asset.
"""

import argparse
import hashlib
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
    from mathutils import Vector,Quaternion,Matrix
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from anatomy import author_anatomy,spine_height,vertebral_positions
    from face import author_face

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
    skin_image = bpy.data.images.load(str(root / "static/aquarium/side-projection.png"))
    texture = ivory.node_tree.nodes.new("ShaderNodeTexImage")
    texture.image = skin_image
    skin_node = ivory.node_tree.nodes.get("Principled BSDF")
    ivory.node_tree.links.new(texture.outputs["Color"], skin_node.inputs["Base Color"])
    skin_node.inputs["Subsurface Weight"].default_value = 0.08
    skin_node.inputs["Coat Weight"].default_value = 0.26
    skin_node.inputs["Roughness"].default_value = 0.38
    skin_node.inputs["IOR"].default_value = 1.04
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
    fin_texture = fin.node_tree.nodes.new("ShaderNodeTexImage")
    fin_texture.image = skin_image
    fin_node = fin.node_tree.nodes.get("Principled BSDF")
    fin.node_tree.links.new(fin_texture.outputs["Color"], fin_node.inputs["Base Color"])
    fin.node_tree.links.new(fin_texture.outputs["Alpha"], fin_node.inputs["Alpha"])
    fin_ray = material("Fin rays", (0.48, 0.55, 0.43), 0.36, transmission=0.25)
    fin_ray.node_tree.nodes["Principled BSDF"].inputs["Alpha"].default_value = 0.25
    lip = material("Soft lips and barbels", (0.63, 0.58, 0.40), 0.28)
    mouth_inside = material("Mouth opening", (0.055, 0.018, 0.017), 0.4)
    iris = material("Golden iris", (0.29, 0.23, 0.095), 0.23, metal=0.08)
    orbital = material("Orbital tissue", (0.61, 0.64, 0.52), 0.36)
    pupil = material("Black pupil", (0.003, 0.009, 0.012), 0.11)
    cornea = material("Corneal highlight", (0.28, 0.40, 0.38), 0.045, transmission=0.9)
    cornea_node=cornea.node_tree.nodes.get("Principled BSDF")
    cornea_node.inputs["Base Color"].default_value=(1,1,1,1)
    cornea_node.inputs["IOR"].default_value=1.035
    cornea_node.inputs["Transmission Weight"].default_value=1
    cornea_node.inputs["Coat Weight"].default_value=.02

    # Nose at negative X; the body narrows to a real caudal peduncle.
    # (X, lateral half-width, dorsal height, ventral depth, centre height).
    # Calibrated body silhouette of the original photographic projection.
    # Fin contours are excluded: they are separately articulated surfaces.
    projection_scale = (2144-30)/1.5
    projection_axis = 374
    silhouette = [(30,386,456), (100,336,489), (200,260,516), (300,215,542),
                  (400,173,560), (500,144,574), (600,124,575), (700,114,576),
                  (800,105,578), (900,110,579), (1000,118,577), (1100,135,560),
                  (1200,160,545), (1300,184,536), (1400,210,518),
                  (1500,231,498), (1600,262,476), (1700,278,465), (1722,281,464)]
    profile = []
    for px, top, bottom in silhouette:
        x = -0.66+(px-30)/projection_scale
        high, low = (projection_axis-top)/projection_scale, (projection_axis-bottom)/projection_scale
        center, radius = (high+low)/2, (high-low)/2
        width = radius*(0.86 if x < 0.2 else 0.86-0.31*min(1,(x-0.2)/0.34))
        profile.append((x,width,radius,radius,center))
    # The last alpha silhouette includes the caudal membrane, not flesh.
    # Taper the actual peduncle into the fin's thin root instead of leaving
    # a flat cylindrical end where a photographic fin begins.
    profile[-3:] = [(0.454,.026,.048,.048,.004),
                    (0.525,.014,.028,.028,.002),
                    (0.54,.009,.024,.024,.002)]
    profile[0]=(-.66,.0245,.025,.025,-.03335)
    profile.append((.58,.0008,.010,.010,.002))

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
        point=Vector((x, (width + offset) * c, center + (radius + offset) * s))
        if x<-.43:
            orbit=math.exp(-((x+.534)**2+(point.z-.0035)**2)/.018**2)
            point.y-=math.copysign(.0032*orbit,point.y)
            # Soft cheek volume and minute physical dermal relief, limited to
            # the scale-free head. It is fixed on the animal, never time-noise.
            point.y+=math.copysign(.0012*math.exp(-((x+.570)/.038)**2)*math.exp(-((point.z+.041)/.035)**2),point.y)
            pore=.000035*math.sin(x*1700+angle*37)*math.sin(x*1100-angle*71)
            point.y+=pore*c;point.z+=pore*s
        return point

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
    rings, sides = 240, 144
    for i in range(rings + 1):
        x = -0.66 + 1.24*i/rings
        for j in range(sides):
            vertices.append(surface(x, math.tau*j/sides))
    for i in range(rings):
        x = -0.66 + 1.24*(i+0.5)/rings
        for j in range(sides):
            a, b = i*sides+j, i*sides+(j+1)%sides
            faces.append((a, b, b+sides, a+sides))
            mat_indices.append(int(pigment(x, math.tau*(j+0.5)/sides)))
    # The rostral ring stays open: the modeled lips and buccal cavity occupy it.
    faces += [tuple(rings*sides+j for j in range(sides))]
    mat_indices += [0]
    body = mesh("Koi skin", vertices, faces, [ivory])

    # RGB in transparent cutout margins is undefined. Opaque 3D tissues
    # reproject boundary samples into real dermis; fin membranes retain alpha.
    from array import array
    source_width,source_height=skin_image.size
    alpha_pixels=array("f",skin_image.pixels)[3::4]
    uv_cache={}
    uv_repairs=0

    def opaque_uv(u,v):
        nonlocal uv_repairs
        x=max(1,min(source_width-2,int(u*source_width)))
        y=max(1,min(source_height-2,int(v*source_height)))
        key=(x,y)
        if key in uv_cache:
            return uv_cache[key]
        def valid(px,py):
            if not 1<=px<source_width-1 or not 1<=py<source_height-1:
                return False
            return min(alpha_pixels[(py+dy)*source_width+px+dx] for dx in [-1,0,1] for dy in [-1,0,1])>.94
        if valid(x,y):
            return u,v
        for radius in [1,2,3,4,6,8,12,16,24,32,48,64,96]:
            for dx,dy in [(0,-radius),(radius,0),(-radius,0),(0,radius),
                          (radius,-radius),(-radius,-radius),(radius,radius),(-radius,radius)]:
                px,py=x+dx,y+dy
                if valid(px,py):
                    result=((px+.5)/source_width,(py+.5)/source_height)
                    uv_cache[key]=result;uv_repairs+=1;return result
        raise RuntimeError(f"Opaque skin projection has no valid dermis near pixel {x},{y}")

    def skin_uv(obj):
        uv = obj.data.uv_layers.new(name="Photographic side projection")
        for loop in obj.data.loops:
            point = obj.matrix_world @ obj.data.vertices[loop.vertex_index].co
            px,pz=point.x,point.z
            if obj.name.startswith(("Koi skin","Rostral lip attachment")) and px<-.626:
                # A side cutout's leading edge is transparent. The 3D rostral
                # pole needs dermis from inside its silhouette, not black RGB
                # from the transparent border of that photograph-like source.
                px=-.626+(px+.626)*.12
            if obj.name.startswith(("Koi skin","Orbital dermal")):
                dx,dz=px+.534,pz-.0035
                radius=math.hypot(dx,dz)
                if radius<.037:
                    # The head projection supplies dermis only. The separate
                    # modeled eye must not sit above a second printed eye.
                    a=math.atan2(dz,dx)
                    px=-.534+.037*math.cos(a);pz=.0035+.037*math.sin(a)
            u=(30+(px+0.66)*projection_scale)/source_width
            v=1-(projection_axis-pz*projection_scale)/source_height
            uv.data[loop.index].uv=(u,v) if "membrane" in obj.name.lower() else opaque_uv(u,v)
    skin_uv(body)

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
    scales = mesh("Overlapping scales", vertices, faces, [ivory])
    skin_uv(scales)

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

    author_face(mesh,curve,sphere,material,surface,skin_uv,ivory,cornea,pupil)

    def fin_mesh(name, base, boundary, mat=fin, base_end=None):
        # A fan has many segmented rays and a curved, scalloped membrane.
        base = Vector(base)
        rays = 42 if name.startswith("Dorsal") else 14 if name.startswith("Anal") else 38 if name.startswith("Caudal") else 32 if name.startswith("Pectoral") else 16
        rings = 8
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
                anchor = base.lerp(Vector(base_end), i/rays) if base_end else base
                if name.startswith("Dorsal"):
                    anchor.z=surface(anchor.x,math.pi/2).z+.0005
                point = anchor.lerp(edge, s)
                point.y += math.sin(s*math.pi)*0.005*math.sin(i/rays*math.pi)
                # Fine corrugation between the branched rays of the membrane.
                point.y += math.sin(i/rays*math.pi*30)*s*s*0.001
                vertices.append(point)
        for i in range(rays):
            for j in range(rings):
                a = i*(rings+1)+j
                faces.append((a, a+rings+1, a+rings+2, a+1))
        obj = mesh(name, vertices, faces, [mat])
        obj.data.materials[0].use_nodes = True
        skin_uv(obj)
        for i in range(1, rays, 2):
            curve(name+" ray", [vertices[i*(rings+1)+j] for j in [0, 2, 5, 8]], 0.00032, fin_ray)
            for offset in [-0.35, 0.35]:
                tip = vertices[i*(rings+1)+8].lerp(vertices[(i+(1 if offset > 0 else -1))*(rings+1)+8], abs(offset))
                curve(name+" ray branch", [vertices[i*(rings+1)+5], tip], 0.00013, fin_ray)
        return obj

    fin_mesh("Caudal membrane", (0.54, 0, 0.028), [(0.59, 0, 0.051), (0.74, 0, 0.17), (0.81, 0, 0.175), (0.835, 0, 0.15), (0.77, 0, 0.055), (0.70, 0, 0.016), (0.77, 0, -0.055), (0.835, 0, -0.16), (0.81, 0, -0.19), (0.74, 0, -0.18), (0.59, 0, -0.028)], base_end=(0.54,0,-.024))
    fin_mesh("Dorsal membrane", (-0.08, 0, 0.19), [(-0.08, 0, 0.193), (-0.04, 0, 0.245), (0.0, 0, 0.254), (0.08, 0, 0.224), (0.18, 0, 0.209), (0.30, 0, 0.15), (0.43, 0, 0.091)], base_end=(0.43, 0, 0.08))
    for sign in [-1, 1]:
        fin_mesh("Pectoral membrane", surface(-.35,-.73 if sign>0 else math.pi+.73,.001), [(-0.28, sign*0.22, -0.15), (-0.20, sign*0.27, -0.208), (-0.15, sign*0.24, -0.21), (-0.12, sign*0.17, -0.14), (-0.20, sign*0.14, -0.10)])
        fin_mesh("Pelvic membrane", surface(.06,-.85 if sign>0 else math.pi+.85,.001), [(0.11, sign*0.19, -0.21), (0.18, sign*0.14, -0.23), (0.23, sign*0.085, -0.19), (0.12, sign*0.085, -0.14)])
    fin_mesh("Anal membrane", (0.27, 0, -0.109), [(0.28, 0, -0.14), (0.34, 0, -0.17), (0.41, 0, -0.14), (0.44, 0, -0.071)])

    # Keep paired fins separate, but combine each fin's branched ray geometry.
    # Hundreds of separate skinned draw calls cost more than the skin detail.
    for prefix in ["Caudal", "Dorsal", "Pectoral", "Pelvic", "Anal"]:
        pieces = [obj for obj in objects if obj.name.startswith(prefix) and "ray" in obj.name]
        if not pieces:
            continue
        bpy.ops.object.select_all(action="DESELECT")
        for obj in pieces:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = pieces[0]
        bpy.ops.object.join()
        combined = bpy.context.object
        combined.name = prefix+" branched rays"
        objects = [obj for obj in objects if obj not in pieces]+[combined]

    positions = vertebral_positions()
    author_anatomy(objects,mesh,curve,sphere,material,interpolate,surface,positions)
    # Rigid skull, 32 representative vertebral centra, and a caudal-fin handle.
    # This remains a simplified model, not a complete veterinary skeleton.
    bpy.ops.object.select_all(action="DESELECT")
    armature_data = bpy.data.armatures.new("Koi swimming rig")
    armature = bpy.data.objects.new("KoiRig", armature_data)
    scene.collection.objects.link(armature)
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="EDIT")
    names = []
    previous = None
    for i in range(len(positions)-1):
        bone = armature_data.edit_bones.new(f"spine_{i:02}")
        bone.head = (positions[i],0,0 if i==0 else spine_height(positions[i]))
        bone.tail = (positions[i+1],0,spine_height(positions[i+1]) if i<len(positions)-2 else spine_height(.54))
        if previous:
            bone.parent = previous
            bone.use_connect = True
        names.append(bone.name)
        previous = bone
    facial_specs={
        "jaw_lower": ((-.53,0,-.057),(-.67,0,-.048)),
        "jaw_upper": ((-.606,0,-.010),(-.67,0,-.028)),
        "operculum_L": ((-.50,.067,.028),(-.375,.112,.010)),
        "operculum_R": ((-.50,-.067,.028),(-.375,-.112,.010)),
        "eye_L": ((-.534,.080,.0035),(-.534,.088,.0035)),
        "eye_R": ((-.534,-.080,.0035),(-.534,-.088,.0035)),
    }
    for name,(head,tail) in facial_specs.items():
        bone=armature_data.edit_bones.new(name);bone.head=head;bone.tail=tail
        bone.parent=armature_data.edit_bones[names[0]]
    axial_names=list(names)
    names+=list(facial_specs)
    bpy.ops.object.mode_set(mode="OBJECT")
    for obj in objects:
        modifier = obj.modifiers.new("Swim deformation", "ARMATURE")
        modifier.object = armature
        obj.parent = armature
        groups = [obj.vertex_groups.new(name=name) for name in names]
        centers = [(positions[i]+positions[i+1])/2 for i in range(len(axial_names))]
        for vertex in obj.data.vertices:
            rigid = obj.get("rigid_joint")
            if rigid is not None:
                groups[rigid].add([vertex.index],1,"REPLACE")
                continue
            point=obj.matrix_world @ vertex.co
            x=point.x
            facial=obj.get("facial_joint")
            if facial:
                weight=1.0
                if obj.name.startswith(("Opercular", "Branchiostegal")):
                    weight=max(0,min(1,(x+.505)/.09))*max(0,min(1,(point.z+.18)/.035,(.06-point.z)/.035))
                if obj.name.startswith("Rostral"):
                    u=max(0,min(1,(-x-.660)/.012))
                    anchor=0
                    if facial=="jaw_lower":
                        sine=(point.z-(-.03335-.00965*u))/(.025-.0175*u)
                        root_z=-.03335+.025*sine
                        anchor=max(0,min(1,(-root_z-.041)/.024))
                    weight=anchor*(1-u)+u
                groups[names.index(facial)].add([vertex.index],weight,"REPLACE")
                groups[0].add([vertex.index],1-weight,"REPLACE")
                continue
            if obj.name=="Koi skin" and x<-.52 and point.z<-.041:
                weight=max(0,min(1,(-x-.53)/.13))*max(0,min(1,(-point.z-.041)/.024))
                groups[names.index("jaw_lower")].add([vertex.index],weight,"REPLACE")
                groups[0].add([vertex.index],1-weight,"REPLACE")
                continue
            # Paired fins articulate at a basal attachment. Their distal rays
            # must not be dragged by whatever trunk vertebra shares their X.
            if obj.name.startswith(("Pectoral", "Pelvic", "Caudal")):
                root_x = -.35 if obj.name.startswith("Pectoral") else .06 if obj.name.startswith("Pelvic") else .69
                root_joint = min(range(len(axial_names)),key=lambda i:abs(centers[i]-root_x))
                groups[root_joint].add([vertex.index],1,"REPLACE")
                continue
            if x <= -0.43:
                groups[0].add([vertex.index], 1, "REPLACE")
                continue
            nearest = sorted(range(len(axial_names)), key=lambda i: abs(centers[i]-x))[:2]
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
        if obj.name.startswith("Anatomy") or "branched rays" in obj.name:
            obj.hide_render=True
        obj.select_set(False)
    armature.location = (0.30, -0.14, 0.77)
    armature.rotation_euler.z = -0.12
    # Slight body wave, not an independent translation of the tail.
    previous_head = None
    previous_rotation = None
    previous_length = None
    free_start = positions[5]
    free_length = positions[-2]-free_start
    for i, name in enumerate(axial_names):
        bone = armature.pose.bones[name]
        s=max(0,(positions[i]-free_start)/free_length)
        phase=-s*4.8
        tangent=0 if i<=4 else math.atan(.09/free_length*(2*s*math.sin(phase)-4.8*s*s*math.cos(phase)))
        rotation=Quaternion((0,0,1),tangent) @ bone.bone.matrix_local.to_quaternion()
        head=bone.bone.head_local.copy() if i==0 else previous_head+previous_rotation @ Vector((0,previous_length,0))
        bone.matrix=Matrix.LocRotScale(head,rotation,Vector((1,1,1)))
        previous_head,previous_rotation,previous_length=head,rotation,bone.length
        bpy.context.view_layer.update()
    lateral=total=0
    for name in axial_names:
        point=armature.pose.bones[name].head
        weight=math.exp(-((point.x+.18)/.30)**2)
        lateral+=point.y*weight;total+=weight
    armature.location+=armature.rotation_euler.to_matrix() @ Vector((0,-lateral/total,0))
    floor_mat = material("Dark river gravel", (0.020, 0.027, 0.032), 0.88)
    rock_mat = material("Water worn stone", (0.026, 0.039, 0.042), 0.69)
    nodes,links=rock_mat.node_tree.nodes,rock_mat.node_tree.links
    coordinates=nodes.new("ShaderNodeTexCoord")
    mapping=nodes.new("ShaderNodeMapping");mapping.inputs["Scale"].default_value=(2,2,2)
    links.new(coordinates.outputs["Object"],mapping.inputs["Vector"])
    textures={}
    for name,file in [("color","stone-color.jpg"),("height","stone-height.jpg"),("arm","stone-arm.jpg")]:
        image=bpy.data.images.load(str(root/"static/aquarium"/file))
        if name!="color":image.colorspace_settings.name="Non-Color"
        texture=nodes.new("ShaderNodeTexImage");texture.image=image;texture.projection="BOX";texture.projection_blend=.3
        links.new(mapping.outputs["Vector"],texture.inputs["Vector"]);textures[name]=texture
    links.new(textures["color"].outputs["Color"],nodes.get("Principled BSDF").inputs["Base Color"])
    bump=nodes.new("ShaderNodeBump");bump.inputs["Distance"].default_value=.007;bump.inputs["Strength"].default_value=.75
    links.new(textures["height"].outputs["Color"],bump.inputs["Height"])
    links.new(bump.outputs["Normal"],nodes.get("Principled BSDF").inputs["Normal"])
    for mat, scale, strength in [(floor_mat, 280, 0.35)]:
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
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, radius=1)
    prototype = bpy.context.object
    stone_vertices = [v.co.copy() for v in prototype.data.vertices]
    stone_faces = [tuple(p.vertices) for p in prototype.data.polygons]
    bpy.data.objects.remove(prototype, do_unlink=True)
    vertices, faces = [], []
    for i in range(22000):
        x, y = rng.uniform(-2, 2), rng.uniform(-0.65, 1.02)
        radius = rng.uniform(0.007, 0.022)
        scale = (radius*rng.uniform(0.8, 1.3), radius*rng.uniform(0.7, 1.2), radius*rng.uniform(0.4, 0.75))
        angle = rng.random()*math.tau
        start = len(vertices)
        for v in stone_vertices:
            px, py, pz = v.x*scale[0], v.y*scale[1], v.z*scale[2]
            vertices.append((x+px*math.cos(angle)-py*math.sin(angle), y+px*math.sin(angle)+py*math.cos(angle), 0.0205+radius*0.45+pz))
        faces.extend(tuple(start+j for j in face) for face in stone_faces)
    mesh("Gravel bed", vertices, faces, [floor_mat])
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
    scatter.inputs["Density"].default_value = 0.025
    scatter.inputs["Anisotropy"].default_value = 0.28
    absorption = nodes.new("ShaderNodeVolumeAbsorption")
    absorption.inputs["Color"].default_value = (0.07, 0.38, 0.49, 1)
    absorption.inputs["Density"].default_value = 0.075
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
        data.size_y = 0.45
        obj = bpy.data.objects.new(name, data)
        scene.collection.objects.link(obj)
        obj.location = location
        obj.rotation_euler = (Vector(target)-obj.location).to_track_quat("-Z", "Y").to_euler()
        obj.visible_camera = False
        obj.visible_glossy = True
        obj.visible_transmission = True
        return obj

    area("Overhead softbox", (0.45, -0.65, 2.35), (0.2, 0.1, 0.4), 500, 1.3, (1.0, 0.93, 0.82))
    area("Side fill", (-2.2, 0.0, 1.75), (0.2, 0.1, 0.7), 100, 0.9, (0.63, 0.76, 0.84))
    area("Rim strip", (1.0, 0.9, 2.6), (0.2, 0.1, 0.7), 150, 0.55, (0.56, 0.81, 0.90))
    camera_data = bpy.data.cameras.new("Aquarium camera")
    camera = bpy.data.objects.new("Aquarium camera", camera_data)
    scene.collection.objects.link(camera)
    camera.location = (0, -4.1, 1.02)
    camera.rotation_euler = (Vector((0, 0.2, 0.86))-camera.location).to_track_quat("-Z", "Y").to_euler()
    camera_data.lens = 47
    scene.camera = camera
    bpy.ops.wm.save_as_mainfile(filepath=str(work/"aquarium.blend"), compress=True)
    geometry_hash = hashlib.sha256((output/"koi.glb").read_bytes()).hexdigest()
    old_manifest = json.loads((output/"manifest.json").read_text(encoding="utf-8")) if (output/"manifest.json").exists() else {}
    reference_metadata = old_manifest.get("reference")
    metadata = {
        "generator": f"Blender {bpy.app.version_string}", "asset": "koi.glb",
        "units": "metres", "fish_length": 1.5, "rig_joints": len(names),
        "facial_joints":list(facial_specs),
        "facial_reference":"https://xromm.org/projects/fish-feeding/",
        "anatomy": {"vertebral_centra":32,"weberian_region":4,"abdominal_region":17,"hypurals":6,
                    "reference":"https://doi.org/10.26842/binhm.7.2026.19.1.0139",
                    "scope":"Species-informed simplified axial anatomy; not a CT reconstruction"},
        "opaque_uv_repairs":uv_repairs,
        "scale_count": rows*columns, "source": "scripts/aquarium/build.py",
        "license": "Original Vira benchmark asset; repository license applies",
        "geometry_sha256": geometry_hash,
        "reference": reference_metadata,
        "reference_matches_asset": bool(reference_metadata and reference_metadata.get("geometry_sha256") == geometry_hash),
    }
    if not args.no_render:
        reference = work / ("reference-draft.png" if args.draft else "reference.png")
        scene.render.filepath = str(reference)
        print("AQUARIUM: rendering reference", flush=True)
        bpy.ops.render.render(write_still=True)
        subprocess.run([encoder, "-q", "94", "-m", "6", str(reference),
                        "-o", str(output / "reference.webp")],
                       check=True, capture_output=True, timeout=120)
        metadata["reference"] = {"engine": "Cycles", "samples": scene.cycles.samples,
                                 "resolution": [scene.render.resolution_x, scene.render.resolution_y],
                                 "geometry_sha256": geometry_hash}
        metadata["reference_matches_asset"] = True
    (output/"manifest.json").write_text(json.dumps(metadata, indent=2)+"\n", encoding="utf-8")
    print("AQUARIUM: complete", flush=True)


if __name__ == "__main__":
    build()
