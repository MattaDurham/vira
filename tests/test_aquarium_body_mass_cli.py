"""Exercise mass-authoring CLI selection against the two actual public GLBs."""
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class AquariumBodyMassCLITest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for mass authoring")
    def test_selected_asset_drives_profile_and_legacy_output_still_works(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "aquarium" / "build_body_mass.mjs"
        baseline_asset = root / "static" / "aquarium" / "koi.glb"
        refined_asset = root / "static" / "aquarium" / "refined" / "koi.glb"
        default_output = root / "static" / "aquarium" / "body-mass.json"
        before = default_output.read_bytes()
        baseline_bytes = baseline_asset.read_bytes()
        refined_bytes = refined_asset.read_bytes()
        baseline_sha = hashlib.sha256(baseline_bytes).hexdigest()
        refined_sha = hashlib.sha256(refined_bytes).hexdigest()
        self.assertNotEqual(baseline_sha, refined_sha, "the actual asset pair must differ")

        with tempfile.TemporaryDirectory(prefix="aquarium mass # ") as directory:
            temporary = Path(directory)
            # Literal reserved URL characters, Unicode and spaces must survive
            # native PATH -> file URL conversion on Windows as well as macOS.
            selected = temporary / "selected koi \u00e9#%.glb"
            selected.write_bytes(refined_bytes)

            def command(arguments):
                return subprocess.run(
                    [shutil.which("node"), str(script), *map(str, arguments)],
                    cwd=temporary, capture_output=True, text=True,
                    encoding="utf-8", timeout=60,
                )

            def generated(arguments, output):
                run = command(arguments)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertTrue(output.is_file(), "CLI must write the requested output")
                return json.loads(output.read_text(encoding="utf-8"))

            expected_run = subprocess.run(
                [
                    shutil.which("node"), "--input-type=module", "--eval",
                    "const {buildBodyMass}=await import(process.argv[1]);"
                    "console.log(JSON.stringify(await buildBodyMass({assetPath:process.argv[2]})));",
                    script.as_uri(), str(selected),
                ],
                cwd=temporary, capture_output=True, text=True,
                encoding="utf-8", timeout=60,
            )
            self.assertEqual(expected_run.returncode, 0, expected_run.stdout + expected_run.stderr)
            expected_selected = json.loads(expected_run.stdout)
            self.assertEqual(expected_selected["assetSHA256"], refined_sha)

            explicit_path = temporary / "selected mass #%.json"
            explicit = generated(
                ["--output", explicit_path.name, "--asset", selected.name], explicit_path,
            )
            self.assertEqual(explicit, expected_selected, "selected geometry drives all mass moments")
            self.assertEqual(explicit["assetSHA256"], refined_sha)

            legacy_path = temporary / "legacy mass #%.json"
            legacy = generated([legacy_path.name], legacy_path)
            self.assertEqual(legacy["assetSHA256"], baseline_sha)
            self.assertEqual(legacy, json.loads(before), "positional output keeps the default uploaded asset")
            self.assertNotEqual(
                explicit["bodies29"], legacy["bodies29"],
                "asset selection must change actual geometry-derived mass/COM/inertia, not just the hash",
            )

            mixed_path = temporary / "positional selected mass.json"
            mixed = generated([mixed_path.name, "--asset", selected.name], mixed_path)
            self.assertEqual(mixed, expected_selected)

            output_only_path = temporary / "output only.json"
            output_only = generated(["--output", output_only_path.name], output_only_path)
            self.assertEqual(output_only, legacy, "--output alone preserves the module-relative default asset")

            invalid = temporary / "invalid selected.glb"
            invalid.write_bytes(b"This is not a GLB")
            sentinel = temporary / "preserve existing output.json"
            sentinel.write_text("existing output must survive\n", encoding="utf-8")
            bad_asset = command(["--asset", invalid.name, "--output", sentinel.name])
            self.assertNotEqual(bad_asset.returncode, 0, "selected invalid geometry must be loaded and rejected")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "existing output must survive\n")

            for arguments, message in [
                (["--asset"], "Missing path for --asset"),
                (["--output"], "Missing path for --output"),
                (["--asset", "--output", "unused.json"], "Missing path for --asset"),
                (["--asset", ""], "Missing path for --asset"),
                (["--unknown"], "Unknown option: --unknown"),
                (["--asset=unused.glb"], "Unknown option: --asset=unused.glb"),
                (["first.json", "second.json"], "Only one positional output"),
                (["first.json", "--output", "second.json"], "Choose either"),
                (["--output", "second.json", "first.json"], "Choose either"),
                (["--asset", selected.name, "--asset", selected.name], "Duplicate option: --asset"),
                (["--output", "first.json", "--output", "second.json"], "Duplicate option: --output"),
            ]:
                with self.subTest(arguments=arguments):
                    failed = command(arguments)
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn(message, failed.stderr)
                    self.assertIn("Usage:", failed.stderr)
            self.assertFalse((temporary / "first.json").exists())
            self.assertFalse((temporary / "second.json").exists())
            self.assertFalse((temporary / "unused.json").exists())

        self.assertEqual(default_output.read_bytes(), before, "tests never write the real default profile")
