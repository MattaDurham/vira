# Aquarium benchmark

An isolated quality benchmark, available at `/aquarium-benchmark.html`
on Vira. It does not change the selected desktop background or Design Studio.

One original rigged koi is shared between an interactive WebGPU aquarium and
a Blender Cycles reference. The controls compare the two renderers, restart the
swim, isolate body motion, inspect the mesh and deformation joints, and adjust
illumination. A restart resumes motion rather than silently freezing the fish.
Move the pointer to invite a loose approach; a close tap produces a brief escape.
Drag, scroll or use the focused canvas's arrow keys to inspect the tank.

## Authoring

Install Blender and the WebP command-line encoder. On macOS:

```sh
brew install --cask blender
brew install webp
```

From the feature worktree:

```sh
blender --background --factory-startup --python scripts/aquarium/build.py -- --samples 64
node tests/aquarium_benchmark.mjs
```

The build writes `static/aquarium/koi.glb`, `manifest.json` and `reference.webp`.
Its editable Blender scene and full PNG reference live in `.aquarium-build/`,
which is ignored by git. `--draft` renders 1280 x 720; a normal render is
3840 x 2160. `--cpu` disables GPU selection, `--no-render` only exports the
model, and `--output DIR` changes the exported asset directory. Metal is used
on macOS when available; other systems attempt CUDA and otherwise use CPU.
Blender and cwebp are authoring tools; the Vira server does not require them.

All geometry is authored by this repository's Blender script. The skin and fin source
`static/aquarium/side-projection.png` was created with the built-in imagegen
tool and calibrated onto volumetric body and articulated fin meshes. Its
production prompt is preserved in `projection-prompt.txt`. The earlier atlas
and prompt are retained as ignored authoring alternatives in `.aquarium-build/`. These original assets
use the repository's MIT license. Stone color, height and packed material
maps are Poly Haven's Dark Rock by Amal Kumar, licensed CC0; source URLs and
checksums are in `static/aquarium/material-sources.json`. No purchased model, remote API,
generated video, credentials or network dependency is needed to display it.
Three.js and its loader utilities are vendored with provenance in
`static/vendor/README.md`.

## What the comparison measures

| | Cycles reference | Live aquarium |
|---|---|---|
| Fish | Shared GLB geometry, 1,330 scales, layered eyes and fin rays | Same mesh and skin weights |
| Motion | Frozen reference pose | 34 axial and 6 facial controls, lateral/vertical curvature and axial torsion, a travelling trunk wave, burst-and-coast propulsion, flexing paired fins and articulated respiratory jaw/gill motion |
| Water | Absorption and scattering in a bounded volume | Beer-Lambert extinction plus camera-ray single scattering through the bounded water volume; specular dielectric surface approximation |
| Glass | Solid thin pane, path-traced refraction | Thin reflective pane approximation at near-normal incidence |
| Fins | Translucent Cycles material | Alpha-blended tissue, branched ray geometry and approximate subsurface backlighting; front glass no longer excludes them |
| Resolution | 4K still, sample count recorded in manifest | Native pixels up to 16,777,216 total pixels and device ratio 2 |
| Timing | Offline render | Rolling browser frame cadence; milliseconds are frame intervals, not GPU timestamps |

The axial study includes a representative 32-centrum carp column, cranial and
opercular plates, abdominal ribs, neural/haemal arches, a six-hypural tail
complex and fin-support joints. Centra and support rods use rigid skin weights;
the notochord bridges remain flexible. These are species-informed simplified
structures, not a complete CT reconstruction. The four anterior Weberian
segments are held rigid, and the remaining joints follow an arched rest spine. Motion approximates propulsion and resistance rather than solving
fluid dynamics. Depth moves the fish through the tank; it never changes scale.
WebGPURenderer uses WebGL 2 if WebGPU is unavailable, and reports the backend.
Append `?renderer=webgl` to force that backend for compatibility testing.
A startup failure stays visible as a named error.

This remains a work in progress toward photographic quality. Skin detail,
gill geometry, fin rays and continuous body deformation have been rebuilt.
The facial feature locations, body contour and fin attachments are calibrated
to the photographic source rather than a generic ellipsoid.
The live renderer uses rectangular area lighting, approximate subsurface
scattering, micro-surface variation and a dense 22,000-pebble substrate.
The offline and live optical models still differ; neither visual quality nor
biological accuracy follows from the test suite passing.

## Verification

`tests/aquarium_benchmark.mjs` loads the shipped GLB through the real loader,
checks its skin weights, UVs and scale normals, and exercises the production pose
function against actual skinned vertices to catch a tail that rolls instead of bending. It also verifies
pitched climbing, pointer curiosity, startle acceleration and five minutes of
bounded swimming. Its unittest wrapper runs in CI without Blender or a GPU.

Browser checks cover the reference toggle, pause/resume, mesh inspection,
camera controls, desktop rendering and a fresh mobile-width load. The displayed
performance is measured locally and should be reassessed on target hardware;
the browser's 60 Hz cadence is not a promise of spare GPU capacity.

## Local engineering preview

Serve the `static/` directory over HTTP; opening the HTML with `file://` cannot
load its ES modules and GLB reliably. A static preview needs no Vira server
restart and does not use owner data:

```sh
python3 scripts/aquarium/preview.py --port 8399
```

Open `http://localhost:8399/aquarium-benchmark.html`. The full owner-review
instance follows the repository's separate Try it first workflow.

## Motion reference

The growing body wave and propulsive bouts are informed by the experimental
midline traces in Yang et al. (2010), [A study on flow physics of burst-and-coast
swimming of koi carp](https://doi.org/10.5226/jabmech.1.30). The simulation
uses a quadratic lateral midline differentiated into a tangent field, then
applied as absolute body-space orientations,
so adding joints does not multiply the bend. It approximates fish-water
interaction; it does not run the paper's Navier-Stokes solver.

The Record 8 seconds (1080p) control copies real live frames at up to
1920 pixels wide into a 30 fps recording canvas. It adds no interpolated frames. With the
engineering preview server, its WebM is saved to
`.aquarium-build/recordings/latest.webm` and available through Review recording.
On an ordinary static server it offers a browser download. No microphone,
screen contents outside the canvas, owner data or external service is used.

Whole-body confinement is checked against sampled skinned trunk surfaces and
all fin membrane vertices, rather than limiting only the root position.
Suspended particles and swimmer drag share a weak divergence-free circulation.

The skeletal proportions are informed by Al Fatle and Jawad (2026),
[Osteological features of the axial skeleton](https://doi.org/10.26842/binhm.7.2026.19.1.0139),
especially plates 2-4 and figure 1. Counts vary among specimens; this authoring
example uses 32 centra and 17 abdominal elements. Fin-ray morphology follows
[FAO's common carp description](https://www.fao.org/fi/static-media/FI/CDrom/aquaculture/I1129m/file/en/en_commoncarp.htm).

The face is authored in `face.py`: a genuinely open oral vestibule, fleshy
labial folds, paired recessed olfactory canals and valves, tapered sensory
barbels, a curved radial iris mesh and shallow corneal domes. Separate lower
jaw, upper jaw, paired opercular and ocular controls move the exported tissue and
corresponding schematic bones. Respiratory sequencing is an artistic
approximation informed by [Gidmark et al.'s common-carp XROMM study](https://xromm.org/projects/fish-feeding/),
not a reconstructed measured trial or a full jaw linkage simulation.

The study selector isolates lateral bending, vertical bending and axial twist.
The head stays level in the isolated vertical/twist studies; those two modes
amplify the deformation for inspection. Normal swimming uses modest vertical
compliance and bank-related trunk torsion, keeping carp propulsion primarily
lateral. Face close-up tracks the held head so respiratory motion can be
judged in moving footage rather than from a texture or a still.

Front, three-quarter and above-water views expose the same articulated
geometry. Extinction clips the camera-to-fragment segment against all six
water-volume bounds, rather than assuming every view enters the front pane.
The live surface adds dielectric reflection without a screen-space
transmission pass that would omit alpha-blended fins. Refraction and energy
conservation at the surface remain approximations requiring further work.

The Swimming ascent / descent study uses the same forward-thrust and water-drag
controller as normal wandering. It changes a goal, rather than translating
height directly. Turns, coasts and boundary reactions remain active. Routine
swimming bouts vary in duration, and small eye rotations compensate turning
and banking without adding eyelids to a fish.

Opaque body, scale and facial surfaces avoid transparent margins in the
photographic projection. The authoring script records reprojected UV samples;
the asset regression test decodes the embedded PNG and checks the exported UVs.
This prevents transparent-border RGB from becoming dark patches on real 3D
skin. Membranes retain their separate translucent material and alpha contour.
