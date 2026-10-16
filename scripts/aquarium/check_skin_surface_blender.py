"""Manual real-export probe: blender --background --factory-startup
--python-exit-code 1 --python scripts/aquarium/check_skin_surface_blender.py

Writes a tiny GLB and two maps to the ignored authoring directory. Ordinary
unit tests exercise numerical seams without requiring Blender in CI.
"""
import json
import math
import struct
import sys
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.aquarium.skin_surface import PBR_DEFAULTS, SkinSurface
from scripts.aquarium.face import author_face, rostral_profile


def check_barbel_registration():
    """Exercise the actual author_face callback on all constructed vertices."""
    from mathutils import Vector
    captures = []
    def material(name, color, rough=.3, metal=0, transmission=0):
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        return mat
    def mesh(name, vertices, faces, materials, indices=None):
        data = bpy.data.meshes.new(name)
        data.from_pydata(vertices, [], faces)
        data.update()
        obj = bpy.data.objects.new(name, data)
        bpy.context.collection.objects.link(obj)
        for mat in materials:
            data.materials.append(mat)
        if "sensory barbel" in name:
            captures.append([v.co.copy() for v in data.vertices])
        return obj
    def curve(name, points, radius, mat):
        return mesh(name, points, [], [mat])
    def sphere(name, location, dimensions, mat, segments=32):
        return mesh(name, [location], [], [mat])
    def surface(x, theta, offset=0):
        return Vector((x, (.12+offset)*math.cos(theta), (.15+offset)*math.sin(theta)))
    ivory = material("Registration dermis", (.8, .8, .8))
    author_face(mesh, curve, sphere, material, surface, lambda obj: None, ivory, ivory, ivory)
    originals = captures[:]
    captures.clear()
    delta = Vector((.001, .02, -.002))
    calls = []
    def register(origin):
        calls.append(origin.copy())
        return origin+delta
    author_face(mesh, curve, sphere, material, surface, lambda obj: None, ivory, ivory, ivory,
                register_barbel_root=register)
    assert len(calls) == len(originals) == len(captures) == 4
    error = max((b-a-delta).length for old, new in zip(originals, captures) for a, b in zip(old, new))
    assert error < 1e-7
    return {"callbacks": len(calls), "maximum_rigid_translation_error": error}


def main():
    output = ROOT / ".aquarium-build" / "skin-surface-export-probe"
    output.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    skin = SkinSurface(lambda x: (.12, .15, .13, 0), size=(64, 32))
    material, metadata = skin.build_material(output)
    vertices = [(x, .12*math.cos(theta), .13*math.sin(theta))
                for x, theta in [(-.3, -math.pi/2-.02), (-.3, -math.pi/2+.02),
                                 (-.1, -math.pi/2+.02), (-.1, -math.pi/2-.02)]]
    mesh = bpy.data.meshes.new("Ventral seam probe")
    mesh.from_pydata(vertices, [], [(0, 1, 2, 3)])
    obj = bpy.data.objects.new("Ventral seam probe", mesh)
    bpy.context.collection.objects.link(obj)
    mesh.materials.append(material)
    uv = skin.assign_uv(obj)
    assert uv["seam_faces"] == 1 and uv["maximum_face_v_span"] < .02
    collar_vertices = []
    for x, theta in [(-.671, math.pi/2-.025), (-.671, math.pi/2+.025),
                     (-.6715, math.pi/2+.025), (-.6715, math.pi/2-.025)]:
        width, radius, _, center = rostral_profile(x)
        collar_vertices.append((x, width*math.cos(theta), center+radius*math.sin(theta)))
    collar_mesh = bpy.data.meshes.new("Actual oral collar chart")
    collar_mesh.from_pydata(collar_vertices, [], [(0, 1, 2, 3)])
    collar = bpy.data.objects.new("Rostral material chart probe", collar_mesh)
    bpy.context.collection.objects.link(collar)
    collar["material_chart"] = "rostral"
    collar_mesh.materials.append(material)
    collar_uv = skin.assign_uv(collar)
    assert collar_uv["maximum_face_v_span"] < .01
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    collar.select_set(True)
    bpy.context.view_layer.objects.active = obj
    path = output / "probe.glb"
    bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB", use_selection=True, export_extras=False)
    raw = path.read_bytes()
    length = struct.unpack_from("<I", raw, 12)[0]
    doc = json.loads(raw[20:20+length])
    exported = doc["materials"][0]
    extensions = exported.get("extensions", {})
    assert abs(extensions["KHR_materials_ior"]["ior"]-PBR_DEFAULTS["ior"]) < 1e-6
    assert abs(extensions["KHR_materials_specular"]["specularFactor"]-PBR_DEFAULTS["specular"]) < 1e-6
    coat = extensions["KHR_materials_clearcoat"]
    assert abs(coat["clearcoatFactor"]-PBR_DEFAULTS["coat"]) < 1e-6
    assert abs(coat["clearcoatRoughnessFactor"]-PBR_DEFAULTS["coat_roughness"]) < 1e-6
    assert exported.get("alphaMode", "OPAQUE") == "OPAQUE"
    pbr = exported["pbrMetallicRoughness"]
    assert pbr["baseColorTexture"]["index"] != pbr["metallicRoughnessTexture"]["index"]
    assert all(im.get("mimeType") == "image/png" and "bufferView" in im and "uri" not in im for im in doc["images"])
    assert all(s.get("wrapT", 10497) == 10497 and s["minFilter"] in (9984, 9985, 9986, 9987) for s in doc["samplers"])
    assert all(not Path(metadata[key]).is_absolute() for key in ("albedo", "roughness"))
    barbel = check_barbel_registration()
    print("SKIN_EXPORT_PROBE " + json.dumps({"result": "passed", "uv": uv, "collar_uv": collar_uv, "barbel": barbel, "material": exported,
                                              "metadata": metadata}))


if __name__ == "__main__":
    main()
