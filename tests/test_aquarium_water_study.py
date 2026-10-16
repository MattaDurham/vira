"""Check recorded water data and frozen kicks without a full animal simulation."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for recorded water data")
class AquariumWaterStudyDataTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "Git is needed for checkout portability")
    def test_byte_hashed_assets_survive_windows_style_git_checkout(self):
        root = Path(__file__).resolve().parents[1]
        recorded = Path("static/aquarium/water-study")
        manifest = json.loads((root / recorded / "manifest.json").read_text(encoding="utf-8"))
        inputs = json.loads((root / "scripts/aquarium/water-study/inputs.json").read_text(encoding="utf-8"))
        pinned = {
            recorded / run[kind]["url"]: (run[kind]["sha256"], run[kind]["bytes"])
            for run in manifest["runs"] for kind in ["poses", "metadata"]
        }
        mass = Path("static/aquarium/body-mass.json")
        pinned[mass] = (inputs["provenance"]["massSHA256"], None)
        # These source files are raw-byte provenance inputs to generate/pack.
        # The fixture exercises their real checkout filters, not attribute text.
        provenance = [
            Path("scripts/aquarium/water-study/inputs.json"),
            *sorted(Path("scripts/aquarium/water-study") / p.name
                    for p in (root / "scripts/aquarium/water-study").glob("*.mjs")),
            *map(Path, [
                "static/aquarium-articulated.js", "static/aquarium-axial-frame.js",
                "static/aquarium-spine.js", "static/aquarium-turn.js",
                "static/vendor/three.webgpu.js", "static/vendor/three.core.js",
                "static/vendor/GLTFLoader.js", "static/vendor/BufferGeometryUtils.js",
                "static/vendor/rapier.mjs",
            ]),
        ]
        paths = sorted(set(pinned) | set(provenance) | {recorded / "manifest.json"})
        env = os.environ.copy()
        for key in ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"]:
            env.pop(key, None)
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM="1")
        with tempfile.TemporaryDirectory(prefix="vira-aquarium-checkout-") as directory:
            checkout = Path(directory)
            git = [shutil.which("git"), "-c", "core.autocrlf=true", "-c", "core.eol=crlf",
                   "-c", "core.safecrlf=false", "-c", f"core.attributesFile={os.devnull}",
                   "-C", str(checkout)]

            def run_git(*arguments):
                result = subprocess.run(git + list(arguments), capture_output=True, text=True,
                                        encoding="utf-8", env=env, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            run_git("init", "--quiet")
            (checkout / ".gitattributes").write_bytes((root / ".gitattributes").read_bytes())
            expected = {}
            for path in paths:
                raw = (root / path).read_bytes()
                expected[path] = raw if path.suffix == ".f64" else raw.replace(b"\r\n", b"\n")
                target = checkout / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(expected[path])
            control = checkout / "checkout-control.txt"
            control.write_bytes(b"ordinary text\nsecond line\n")
            run_git("add", "--", ".gitattributes", control.name, *(p.as_posix() for p in paths))
            for path in paths:
                (checkout / path).unlink()
            control.unlink()
            run_git("checkout-index", "--all", "--force")
            self.assertEqual(control.read_bytes(), b"ordinary text\r\nsecond line\r\n",
                             "the fixture must actually exercise CRLF checkout conversion")
            for path in paths:
                raw = (checkout / path).read_bytes()
                self.assertEqual(raw, expected[path], f"Git changed byte-hashed aquarium input: {path}")
                if path in pinned:
                    checksum, size = pinned[path]
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), checksum, str(path))
                    if size is not None:
                        self.assertEqual(len(raw), size, str(path))
            # Feed the actual checked-out resources into the public integrity
            # loader; no byte normalization or replacement verifier is used.
            script = r"""
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {webcrypto} from 'node:crypto';
globalThis.crypto??=webcrypto;
const {loadWaterStudy}=await import(process.argv[1]),base=new URL(process.argv[2]);
const fetcher=async url=>{
  assert.ok(new URL(url).href.startsWith(base.href));
  const bytes=await readFile(url);
  return {ok:true,json:async()=>JSON.parse(bytes.toString('utf8')),
    arrayBuffer:async()=>bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength)};
};
const {runs}=await loadWaterStudy(base,{fetch:fetcher});
for(const descriptor of runs)assert.equal((await descriptor.load()).id,descriptor.id);
console.log(JSON.stringify({runs:runs.length}));
"""
            result = subprocess.run(
                [shutil.which("node"), "--input-type=module", "-e", script,
                 (root / "static/aquarium-water-study-data.js").as_uri(),
                 (checkout / recorded).as_uri() + "/"],
                cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["runs"], 3)

    def test_exact_recorded_data_integrity_units_and_actual_render_join(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_water_study_data.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_independent_frozen_kick_solver_and_physical_mutations(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_water_solver_oracle.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
