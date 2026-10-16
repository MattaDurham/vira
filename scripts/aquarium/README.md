# Aquarium benchmark

An isolated quality benchmark, available at `/aquarium-benchmark.html`
on Vira. It does not change the selected desktop background or Design Studio.

One original rigged koi is shared between an interactive WebGPU aquarium and
a Blender Cycles reference. The controls compare the two renderers, restart the
swim, isolate body motion, inspect the mesh and deformation joints, and adjust
illumination. A restart resumes motion rather than silently freezing the fish.
Move the pointer to invite a loose approach; a close tap produces a brief escape.
Attention has a short delay and fades when the pointer remains still, so the
animal can resume its own activity instead of chasing indefinitely.
Drag, scroll or use the focused canvas's arrow keys to inspect the tank.

The optional **Research layer** follows the rendered skeleton with projected
labels for head-led steering, the travelling muscle envelope, fin control,
anatomy, water depth and authored surfaces. Its live diagram displays the
authored axial pose offsets and activation envelope, not recorded EMG or a
reconstructed centerline. A four-second history traces trunk and tail offsets
separately in lateral bending, vertical bending and twist, on fixed degree
scales. Pausing preserves the observed samples; restarting clears the trail,
and gaps are not joined into invented motion. Six
research notes distinguish observations, implementation and the limits of each
comparison; linked papers and original footage retain their species context.
**How this was built** connects observation, geometry, controls and lighting,
with a complete source index and links back into the live notes. While the
notes are open, a smoothed camera focus follows the fish so it stays beside
the reading panel; projection, reflections and annotation anchors share that
camera. Closing the layer restores the normal aquarium view. Append
`?research=1` or `?research=story` to open either view directly. Annotations
project actual world-space rig positions through the current camera, update at
up to 20 Hz only while visible, and stop over the offline reference.
The research camera reserves space beside or below the animal for its notes;
the fish, reflections and annotation points all share that off-axis projection.

## Authoring

Install Blender and the WebP command-line encoder. On macOS:

```sh
brew install --cask blender
brew install webp
```

From the feature worktree:

```sh
blender --background --factory-startup --python-exit-code 1 --python scripts/aquarium/build.py -- --samples 64
node tests/aquarium_benchmark.mjs
```

The build writes `static/aquarium/koi.glb`, `manifest.json` and `reference.webp`.
Its editable Blender scene and full PNG reference live in `.aquarium-build/`,
which is ignored by git. `--draft` renders 1280 x 720; a normal render is
3840 x 2160. `--cpu` disables GPU selection, `--no-render` only exports the
model, and `--output DIR` changes the exported asset directory. Metal is used
on macOS when available; other systems attempt CUDA and otherwise use CPU.
Blender and cwebp are authoring tools; the Vira server does not require them.

All geometry is authored by this repository's Blender script. The skin source
`static/aquarium/side-projection.png` was created with the built-in imagegen
tool and calibrated onto the volumetric body. The current `skin-dermis.png`
removes its printed eye, nostril and gill markings so real facial surfaces do
not duplicate a photograph or stretch adjacent pigmentation into a radial halo.
The original is preserved; both edit prompts are in `skin-dermis-prompt.txt`.
Fins have separate generated
root-to-tip membrane textures, with translucent tissue and fibres aligned to
their modeled branched rays; they do not reuse the body photograph. The skin's
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
| Water | Separate closed dielectric boundary and absorption/scattering volume, with path-traced refraction | Beer-Lambert extinction and camera-ray single scattering, with a moving planar reflection, exact dielectric Fresnel lookup and underwater total internal reflection |
| Glass | Solid thin pane, path-traced refraction | Thin reflective pane approximation at near-normal incidence |
| Fins | Separate translucent membrane maps; fine ray tubes omitted from the still | Same membrane UVs plus visible ray tubes, alpha-blended tissue and approximate subsurface backlighting |
| Resolution | 4K still, sample count recorded in manifest | Native pixels up to 16,777,216 total pixels and device ratio 2 |
| Timing | Offline render | Rolling browser frame cadence; milliseconds are frame intervals, not GPU timestamps |

The axial study includes a representative 32-centrum carp column, cranial and
opercular plates, abdominal ribs, neural/haemal arches, a six-hypural tail
complex and fin-support joints. Centra and support rods use rigid skin weights;
the notochord bridges remain flexible. These are species-informed simplified
structures, not a complete CT reconstruction. The four anterior Weberian
segments are held rigid, and the remaining joints follow an arched rest spine. Motion approximates propulsion and resistance rather than solving
fluid dynamics. Depth moves the fish through the tank; it never changes scale.
The water volume is 4 m wide, 2.5 m front to back and 1.77 m high. Shared live
bounds in `aquarium-space.js` keep navigation, collision and optics together;
the exported reference manifest records the matching authoring bounds.
The extra turning room permits broad arcs instead of forcing the large fish
to pivot slowly inside a nearly body-length enclosure.
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
bounded swimming. Three repeatable behavior runs exercise cruising, inspection,
quiet pauses, heading stability during relaxed glides and pointer habituation.
Identical elapsed-time runs at 15, 30, 60 and 120 fps check the 120 Hz physics
substeps. Six seeded runs reject accidental low-speed cruising that lasts six
seconds; new cruise choices favour travel along the tank's long axis.
Actual-bone onset checks require skull, shoulder, trunk and tail to turn in
that order in both yaw and pitch, without impulses across heading wrap. A
travelling drive envelope allows the tail to settle during a coast. Paired-fin
checks cover quiet gliding, sparse hover bouts, braking, turn/depth corrections
and separate pelvic timing. These tests reject regression variants; their
passing does not establish measured biological timing.
Turning torque builds and releases with the manoeuvre envelope, rather than
switching between a full turn and fin trim at each bout boundary. Gesture
regressions reject abrupt onset and release during broad and small turns.
Unsigned turn activity follows the finite bend through the same regional
response. A smooth local blend lets the ordinary swim wave yield during
loading and return during release, so an opposing carrier phase cannot erase
a loaded turn. The recruitment threshold is authored, not a measured koi
muscle parameter. Mirrored, phase-varied actual-skin checks distinguish loaded
strokes from nascent loading and anterior recovery. Pure twist follows each
vertebra's arched rest tangent without adding unintended centerline bending.
`tests/aquarium_fins.mjs` checks the actual paired membranes and ray geometry
through a shared field: fixed attachments, close ray-to-tissue alignment,
continuous phases, local volume preservation and unfolded surface triangles.
`tests/aquarium_turn_bend.mjs` reads the actual head, trunk and peduncle to
check separate loading, peak and return times, quiet settling and small-turn
restraint. It also exercises flicks and two minutes of deformed swimming for
each of six deterministic seeds. These are regression bounds for the authored
fish, not fitted biological reaction times or curvature measurements.
A joined navigation/asset check tests a minute of routine swimming against the
actual articulated body and fins, catching visible collision corrections at
the glass. Its unittest wrapper runs in CI without Blender or a GPU.

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

The recording control offers 8 or 30 seconds and copies real live frames at up to
1920 x 1080 into a 30 fps recording canvas, preserving the view's aspect ratio.
It adds no interpolated frames. During capture the renderer uses the export's
pixel extent, rather than drawing a native 7M-pixel image only to downsample it.
Native resolution returns as soon as recording stops or fails. Resizing the
window during capture letterboxes the real frames into a fixed-size video.
With the
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
corresponding schematic bones. Jaw protrusion and the feeding mechanism are
informed by [Gidmark et al.'s common-carp XROMM study](https://xromm.org/projects/fish-feeding/).
The respiratory sequence and timing are authored; the model does not
reconstruct a measured breathing trial or a full jaw linkage simulation.
The opercular seal is informed by [Ballintijn's carp valve study](https://pubmed.ncbi.nlm.nih.gov/4001607/),
which does not supply this animation's timing.

The two narial apertures on each side are cut through the actual head mesh,
with recessed cups, a raised anterior inlet and a dividing crest. The slightly
larger, elongated posterior opening follows the adult common-carp description
in [Pashchenko and Kasumyan (2017)](https://doi.org/10.1134/S0032945217010088).
The inset iris has its own continuous polar pigment atlas, rather than body
projection UVs. Its subdued pale reflectance approximates a guanine reflector;
[Gur et al. (2018)](https://doi.org/10.1002/advs.201800338) measured that mechanism
in zebrafish, not a carp reconstruction. Real exported-surface ray tests check
that skin does not fill the narial openings and that eyes sit within six
millimetres of the surrounding head.

The opercular cover replaces an opening in the actual head skin. Its closed
thin shell and integrated posterior edge move over a recessed, skull-bound
chamber; the ventral seal blends between stationary and moving tissue. Attached
and dorsal/ventral margins stay flush with the curved head envelope.

The study selector isolates lateral bending, vertical bending and axial twist.
**Swimming turn** replays the real navigation and propulsion model with a
resettable random stream and fixed 120 Hz steps. The destination reverses
after two seconds; Restart swim and Record replay the same course while
preserving the chosen camera. Cursor attraction and taps are ignored during
this comparison. It animates no prescribed body poses; fin history is sampled
at each physics tick so display cadence does not change the replay.
Paired fin control holds the trunk while exposing cupping and feathered
recovery under an authored braking load. Head-on study follows the same fish
from in front; it can be combined with the side and quarter views to judge
surface shape rather than just a side silhouette.
The head stays level in the isolated vertical/twist studies; those two modes
amplify the deformation for inspection. Normal swimming uses modest vertical
compliance and bank-related trunk torsion, keeping carp propulsion primarily
lateral. Face close-up tracks the held head so respiratory motion can be
judged in moving footage rather than from a texture or a still.

Front, three-quarter and above-water views expose the same articulated
geometry. Extinction clips the camera-to-fragment segment against all six
water-volume bounds, rather than assuming every view enters the front pane.
The live surface blends the mirrored scene using the air/water Fresnel
coefficients, including total internal reflection when observed from below.
Reflection uses a quarter of the main framebuffer dimensions and omits scale relief
and individual pebbles, retaining the articulated fish silhouette, membrane
tissue and large stones. Native detail is retained in the main view.
The reflection renders separately before the main scene, is skipped when the
surface is outside the view, and reuses the preceding main-view shadow maps.
It refreshes every third live frame; camera changes force an immediate refresh.
Main-view swimming and water normals continue to update each frame.
The reflected and direct paths share extinction coefficients. The lights sit
below the water surface in both renderers. Live surface refraction remains
an approximation requiring further work; the offline reference traces the
separate glass, water boundary and participating medium.

The Swimming ascent / descent study uses the same forward-thrust and water-drag
controller as normal wandering. It changes a goal, rather than translating
height directly. Turns, coasts and boundary reactions remain active. Routine
swimming bouts vary in duration, and small eye rotations compensate turning
and banking without adding eyelids to a fish.

Routine choices maintain a course for several seconds, alternate travel with
slower inspection and occasional hovering, and retain a depth preference longer
than a single stroke. This is an authored behavior model, not a fitted ethogram.
The observational references are original [pond footage](https://www.youtube.com/watch?v=5XJteeTP5SA)
and [underwater koi footage](https://www.youtube.com/watch?v=zhLm3EsDDqc).
[Yang et al. (2009), Flow Physics of Routine Turns of Koi Carp](https://doi.org/10.1299/jbse.4.67)
describes a single-sided stroke, return toward straight, and a subsequent coast.
The controller approximates that separation through short turning bouts. A
bounded signed bend loads during the first part of a bout, then releases
through successive trunk regions. Small course corrections recruit very little
of this bend; routine return turns recruit more. The skull remains rigid and
the existing orientation lag remains separate from that finite stroke.
The tail relaxes and the course holds during the glide. Its rates and forces
are tuned for this large authored fish; they are not copied from the paper's
small experimental specimen or derived by solving its fluid equations.
The original [slow](https://doi.org/10.7717/peerj.21161/supp-1) and
[fast](https://doi.org/10.7717/peerj.21161/supp-2) bluegill videos show separate
head rotation and tail follow-through from bottom and side views. Those
apparatus-elicited 180-degree turns are comparative evidence, not spontaneous
koi calibration. [Howe and Astley](https://doi.org/10.1002/jez.2398) measured
localized curvature pulses in giant danios; head-led reorientation does not
imply that curvature must originate at the nose.

Paired fins paddle independently at low speed, open for braking and trail during
a propulsive burst. Their phase is independent of the tail; merged ray geometry
uses each membrane's basal pivot and the corresponding side's paddle phase.
A shared bind-space field curves each membrane and its embedded rays together.
Loaded surfaces cup, the distal sheet feathers through recovery, and span and
ray position introduce a short compliance delay. Static glides acquire no
extra decorative oscillation. Four bounded controllers retain actual observed
paddle commands for the delayed response; a new control demand is not copied
into an invented earlier carrier phase. Pauses preserve the response, and a
reset discards future command history. Frozen fixtures retain an explicitly
stateless authored-cycle function. Smooth ray normals are reconciled only across
the original smooth UV seams after deformation; intentional creases, skin
influences and topology stay separate. These bounded deformations approximate the modes
described in [Wang et al.'s koi study](https://doi.org/10.1016/S1672-6529(13)60217-6)
and [active ray-curvature mechanics](https://doi.org/10.1098/rsif.2006.0181);
they do not solve the two sliding ray halves or recover measured tissue forces.
Comparisons include the Lauder Laboratory's original
[front-view bluegill footage](https://www.youtube.com/watch?v=36Of7uj9naA),
whose labriform swimming differs from carp, and
[trout fin-control experiments](https://doi.org/10.1242/jeb.246275), which
distinguish actively held braking surfaces from passive motion in a wake.
The laboratory's [prey-capture and braking clip](https://www.youtube.com/watch?v=yauU364_Gz8)
was also inspected at quarter speed: after capture the head translates little
while the near pectoral changes from broad and oblique to a narrow edge.
That is qualitative bluegill context, not a koi timing measurement.
The author's [trout free-stream clip](https://www.youtube.com/watch?v=YI4lr8Z70VE)
was inspected at quarter speed too. Its dorsal projection shows the tail edge
counterposed to the bending peduncle while the head stays comparatively steady;
that view supports local feathering, but cannot establish independent lobe motion.

The caudal fan now has its own shared membrane-and-ray field in
`aquarium-caudal.js`, in addition to the articulated tail attachment. The entire
basal line stays fixed. During a moving glide the fan narrows gently; braking
opens it and curves the dorsal and ventral lobes in opposite lateral directions
around a relatively quiet fork. Power adds restrained distal compliance and
cupping. A critically damped controller retains observed control changes;
paused frames are idempotent, and reset or missing history starts a new response.
The field has a positive triangular Jacobian, with no folding or detached rays.
[Flammang and Lauder's bluegill measurements](https://doi.org/10.1242/jeb.021360)
motivate these distinct modes; [Esposito et al.](https://doi.org/10.1242/jeb.062711)
compare live tail kinematics with a robotic fan. The amplitudes and response
times here are authored koi animation controls, not measured koi anatomy or
fluid forces. The tail field adds no extra propulsion force. The research layer
traces the actual deformed, skinned distal membrane edge through the live camera.
The Tail-on study camera follows the fish from behind to inspect this field
without replacing the active swimming controls with a prescribed fan pose.

Loaded turning regions suppress the ordinary cruise carrier. Root propulsion
uses the same posterior recruitment blend: its phase modulation yields to the
authored mean effort while the tail loads, then returns as that region releases.
This removes phase-dependent acceleration from a cruise wave that is no longer
visible. It remains reduced-order propulsion, not a force reconstruction from
tail shape or a solved wake.

The live renderer also hands unresolved ray-tube contrast to the filtered fin
map. A conservative whole-set fade uses the 0.26 mm branch diameter, actual
drawing-buffer height, camera projection and fish-root camera depth, fading
between 0.5 and 1.5 pixels. These thresholds are authored contrast LOD, not
exact pixel coverage or a tissue measurement. Geometry remains intact, and
skeleton inspection replaces the faded material. The reflection shares the
main camera's detail level. `?ray-detail=full` retains full ray contrast for
visual comparison. [Sampling theory](https://pbr-book.org/4ed/Sampling_and_Reconstruction/Sampling_Theory)
explains the aliasing problem; it does not prescribe these thresholds.
A body-envelope
forecast reserves room for tail sweep and constrains turning torque before a
pane is reached. Small paired-fin forces can centre the fish or briefly scull
backward when a turn is tight; these are horizontal forces integrated against
water drag, not position offsets. The actual skinned-surface collision guard
remains the final fallback. Vertical acceleration still comes from pitched
forward swimming, with neutral buoyancy and no height tween.

Opaque body, scale and facial surfaces avoid transparent margins in the
photographic projection. The authoring script records reprojected UV samples;
the asset regression test decodes the embedded PNG and checks the exported UVs.
This prevents transparent-border RGB from becoming dark patches on real 3D
skin. Membranes retain their separate translucent material and alpha contour.
