"""Exercise the shipped aquarium asset and motion together, without a GPU."""
import json
import struct
from io import BytesIO

from PIL import Image

import shutil
import subprocess
import unittest
from pathlib import Path


class AquariumBenchmarkTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the research overlay harness")
    def test_research_tracks_the_rendered_rig(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run([shutil.which("node"), str(root / "tests" / "aquarium_research.mjs")],
                             cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the graphics asset harness")
    def test_authored_rig_and_swimming(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run([shutil.which("node"), str(root / "tests" / "aquarium_benchmark.mjs")],
                             cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_opaque_tissue_samples_real_skin(self):
        root = Path(__file__).resolve().parents[1]
        count = 0
        for name, alpha in opaque_tissue_samples((root / "static/aquarium/koi.glb").read_bytes()):
            self.assertGreaterEqual(alpha, 235,
                                    f"{name} projects opaque tissue onto a transparent source margin")
            count += 1
        self.assertGreater(count, 0, "the shipped skin and scales must be checked")


def opaque_tissue_samples(data):
    """Read the actual embedded PNG and exported UVs, without Blender or a GPU."""
    if data[:4] != b"glTF":
        raise ValueError("Expected a binary glTF asset")
    json_length = struct.unpack_from("<I", data, 12)[0]
    gltf = json.loads(data[20:20 + json_length])
    payload = data[28 + json_length:]
    for mesh in gltf["meshes"]:
        if not mesh["name"].startswith(("Koi skin", "Overlapping scales", "Rostral lip attachment")):
            continue
        for primitive in mesh["primitives"]:
            uv = gltf["accessors"][primitive["attributes"]["TEXCOORD_0"]]
            if uv["componentType"] != 5126 or uv["type"] != "VEC2":
                raise ValueError("Expected float UV coordinates")
            view = gltf["bufferViews"][uv["bufferView"]]
            offset = view.get("byteOffset", 0) + uv.get("byteOffset", 0)
            stride = view.get("byteStride", 8)
            material = gltf["materials"][primitive["material"]]
            texture_index = material["pbrMetallicRoughness"]["baseColorTexture"]["index"]
            image = gltf["images"][gltf["textures"][texture_index]["source"]]
            image_view = gltf["bufferViews"][image["bufferView"]]
            start = image_view.get("byteOffset", 0)
            with Image.open(BytesIO(payload[start:start + image_view["byteLength"]])) as source:
                alpha = source.convert("RGBA").getchannel("A")
            width, height = alpha.size
            for i in range(uv["count"]):
                u, v = struct.unpack_from("<ff", payload, offset + i * stride)
                x = max(0, min(width - 1, int(u * width)))
                y = max(0, min(height - 1, int(v * height)))
                yield mesh["name"], alpha.getpixel((x, y))
