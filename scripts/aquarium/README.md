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
and gaps are not joined into invented motion. The fin note has a separate
causal pitch diagram: signed fin command on a fixed 8.6-degree scale, followed
by actual head and tail frame attitudes on a shared 30-degree scale. It removes
each joint's neutral arch before reading world pitch. Its arrows show authored
trim direction, not measured fluid forces; pose-only studies hide this diagram.
The Pitch/Roll/Steering switch also exposes differential fin trim and actual
material roll on a fixed 7.4-degree body scale. The roll measurement removes heading and
pitch geometrically, rather than using an Euler component or axial-offset
estimate. Steering plots the four issued left/right pectoral paddle and
fore-aft angles, on fixed scales of +/-0.15 radians (8.6 degrees) and +/-0.08
radians (4.6 degrees). Left is solid and right is dotted; values outside the
scales are clipped. Its mode label identifies the active pectoral task or
swimming context, not a combined stage for all four fins. These are model
commands, not measured fish kinematics or fluid forces. Histories remain live
while switching; pausing invents no new samples.
Six research notes distinguish observations, implementation and the limits of each
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
| Motion | Frozen reference pose | 34 axial and 6 facial controls, lateral/vertical curvature and axial torsion, a travelling trunk wave, burst-and-coast propulsion, flexing paired, median and caudal fins, and articulated respiratory jaw/gill motion |
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
`aquarium_pitch.mjs` joins signed fin commands to actual membrane incidence,
integrated pitch acceleration and skull/trunk/tail onset. Opposite commands,
quiet glides and three repeated seeded climb/descent routes exercise the same
deformed rays, tissue, face and caudal field used in the renderer. Disconnecting
the fin presentation or moment, or restoring an independent head servo, fails
these guards. The onset thresholds validate this authored sequence, not koi
reaction times.
`aquarium_roll.mjs` checks the shared differential-fin moment against actual
skull acceleration, axial material-frame onset and release, quiet settling,
and combined pitch/turn clearance. Mutations disconnecting the foil or moment,
restoring the former bank servo, and injecting yaw directly into tail torsion
are rejected.
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
Forward effort follows the same posterior activation that reaches the visible
tail, combined with the stroke-dependent carrier rate. Asking for a coast
lets that finishing stroke release its force gradually. A newly chosen cruise
starts its own propulsive bout rather than inheriting an almost-finished
inspection stroke. These are authored causal controls, not measured muscle forces.
`tests/aquarium_fins.mjs` checks the actual paired membranes and ray geometry
through a shared field: fixed attachments, close ray-to-tissue alignment,
continuous phases, local volume preservation and unfolded surface triangles.
`tests/aquarium_median.mjs` checks dorsal and anal attachment lines, material
segment and actual tube-centerline lengths after skinning, shaft/branch
junctions, ray-to-tissue alignment, smooth seams and rendered normal directions.
Fixed-body comparisons isolate signed intrinsic fin movement from spine
movement. Turn loading, glide release and seeded combined navigation use the
same production fields. The deterministic study includes both median control
histories at every physics tick.
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
[Clark and Tytell's bluegill experiments](https://doi.org/10.7717/peerj.21161)
use a moving flow-through chamber to elicit 180-degree turns at controlled
rates and a constrained radius. Five fish each contributed at least 15 slow
and 15 fast turns. All used pectoral fins; slower turns involved more strokes,
while faster turns had higher mean stroke frequency. Inside and outside
pectoral amplitudes did not differ significantly in slow turns (P=0.378),
whereas the inside amplitude was greater in fast turns (P<0.001). A universal
rule that the inside fin is always larger is therefore unsupported. These
apparatus-elicited turns do not calibrate spontaneous koi corrections,
continuous yaw reversals or return-to-glide timing.
The original [slow](https://doi.org/10.7717/peerj.21161/supp-1) and
[fast](https://doi.org/10.7717/peerj.21161/supp-2) movies present views from below
(top) and the side (bottom). The slow movie was inspected at normal and half
speed through its 6.933333-second end, including media times 1.368716,
3.645563, 4.153059 and 5.629192 seconds. The body starts comparatively
straight; the head turns laterally as the tail follows into a C-shaped bend,
the fins present differently during the turn, and the body unbends as it
moves out. These are qualitative observations, not measured stroke chronology,
fluid forces or control coefficients.
[Howe and Astley](https://doi.org/10.1002/jez.2398) measured
localized curvature pulses in giant danios; head-led reorientation does not
imply that curvature must originate at the nose.

The original [freely swimming carp video S1](https://figshare.com/articles/media/28127789?file=51456827)
was reviewed at normal speed through its 50.433333-second end, including media
times 2.870833, 12.153788 and 34.827844 seconds. At the first sample the fish
cruises along the pool edge with extended pectorals; at the second its posterior
is curved while the anterior stays relatively straight and the near pectoral
presents a broad surface; at the third the tail is displaced as the fish
crosses open water with paired fins visible. These are qualitative viewing
notes. The source caption identifies a carp carrying a cranial 3D gyrosensor;
the oblique overhead view, reflections and sensor prevent reliable local-roll
or median-ray timing estimates. No control coefficients were derived from
this clip.

The [goldfish foraging video S2](https://ndownloader.figshare.com/files/51456824)
was reviewed in its original player, including media times 3.52, 6.99 and
11.37 seconds. Repeated nose-down dips interrupt slower recovery while the fish
works along the feeding grate. The experiment constrains lateral turns with
a narrow slot and tracks two head landmarks from one side; it does not measure
unrestricted three-dimensional swimming or establish fin-force timing.
Nor is a head-first sequence universal: [Wilga and Lauder](https://doi.org/10.1242/jeb.202.18.2413)
connect pectoral deformation with the initiation of rising and sinking in
juvenile sturgeon. [Ehrlich and Schoppik](https://doi.org/10.7554/eLife.45839)
show larval zebrafish combining body rotation with fin-generated lift.
[Ting and Yang](https://doi.org/10.1242/jeb.020263) measured opposing pectoral
and caudal pitching moments during forward sinking in parrot cichlids. Those
species and experimental conditions inform the causal questions, not adult
koi timing, buoyancy, fin-incidence signs or force magnitudes.

A depth decision now changes signed basal fin trim before integrating head
rotation. The same returned pectoral and pelvic incidences drive a
neutral-relative authored pitching moment, with angular damping and bounded
rate and attitude. Basal span rotation preserves the fin attachment while
remaining separate from distal cupping. Neutral rig normals are not a zero-lift
calibration: no absolute lift coefficient, solved wake or koi EMG is inferred.
The body still climbs or dives through pitched propulsion, not a height tween.

Roll uses a separate signed differential incidence on the same paired fins.
Common pitch trim and differential roll trim form separate neutral-relative
authored moments. Angular damping carries bank toward its existing restrained
turn preference, and actual bank changes propagate through the axial history.
This replaces the direct yaw-rate twist that could roll the posterior before
the head bank appeared; a held, quiet attitude no longer carries a permanent
counter-twist. Bank sign, gains and response times remain authored, not an
adult-koi banking law. A glide on a curved path can retain a bank preference;
ending a propulsive stroke does not forcibly level the fish.

[Standen and Lauder's bluegill study](https://doi.org/10.1242/jeb.01706)
records soft dorsal and anal fin shape during steady swimming and slow turns.
Figure 5 (p. 2758) shows both trailing edges displaced to the same side,
with modest curvature of individual rays despite a strongly bent fin surface.
The authors infer varying basal ray rotation as a separate control from ray
curvature; they did not measure the muscle activation needed to confirm that
mechanism. The stiff anterior spines did not oscillate laterally. Results and
Figure 6 (pp. 2758-2759) report smaller fin areas at faster steady speeds,
contradicting the abstract's statement that area increases; turn areas remain
comparable to those at low speeds. These observations establish neither a
rule that median fins collapse during gliding nor adult-koi control values.
The proposed roll torque balance remains a hypothesis.

[Chadwell and Ashley-Ross's bluegill morphology study](https://doi.org/10.1002/jmor.11031)
finds stiffer anterior soft rays and more flexible posterior rays with greater
range of motion. Cyprinid supports need their own anatomical distinction:
[Li et al.'s developing wild-type goldfish](https://doi.org/10.1002/dvdy.24340),
Figures 31-32, have serrated anterior segmented spine-like rays and posterior
rays that branch distally. These developmental specimens do not supply adult
koi stiffness or movement measurements, and their reinforced leading rays
should not be described as bluegill-style unsegmented spines.

`aquarium-median.js` adds restrained dorsal and anal inclination and delayed
distal curvature. It first skins the original material paths, retains every
posed basal attachment, and integrates unit tangents at the original segment
lengths. One posed tissue atlas carries the membrane and embedded ray tubes.
Positions and surface normals return through each vertex's exact inverse
weighted skin transform, so the renderer's subsequent skinning reproduces
that surface. The long dorsal attachment remains a line across several
vertebrae. Quiet glides release active bias toward neutral deployment; this
shape field supplies no extra swimming force. Gains and response times are
authored, with comparatively quiet anterior support and more posterior flex.
The [original posterior bluegill video](https://www.youtube.com/watch?v=l7gtX7QIlLU)
was reviewed at quarter speed, including media times 3.03, 4.94 and 5.47 seconds.
Paired fins change from broad sheets to narrow edge presentations while the
body stays comparatively quiet. This qualitative view does not resolve fluid
forces or calibrate the roll controller.

[Drucker and Lauder (2001)](https://doi.org/10.1242/jeb.204.3.431) measure
distinct pectoral stroke timing and wake-force directions during bluegill
turns: the stimulus-near fin directs momentum laterally to rotate away, while
the other directs it posteriorly to translate. In
[Standen's trout study (2008)](https://doi.org/10.1242/jeb.018572), spontaneous
foraging turns at 0.21-0.84 body lengths per second have no regular pelvic fin
cycle. Heading change precedes inside-fin abduction; outside-fin abduction
follows as heading change reduces, sometimes followed by another inside-fin
correction. As the body straightens, both fins weakly adduct and pronate but
remain fairly extended; settling does not imply that every fin folds flat.
The proposed trimming forces are inferred from kinematics, not measured in
that study. These species and conditions inform separate steering
and settling actions, not adult-koi gains, continuous direction-reversal timing
or quiet-glide relaxation constants.

Live paired-fin control issues finite steering and braking strokes with a
loaded excursion and weaker recovery. Ordinary bilateral pectoral steering
uses the same strength on both sides, with different onsets and fore-aft
presentation. Direction changes blend from the still-evolving outgoing
trajectory; later requests queue, and releasing a direction cancels stale
queued steering. Braking retains deployment without continuously repeating a
paddle, and rearms after flow-weighted brake demand releases. Explicit hover
context permits sparse, finite balance strokes after a quiet interval;
neutral gliding does not acquire that activity. Pelvic inside/outside actions
respond to actual heading rate and its subsequent reduction, rather than a
fixed offset from a pectoral carrier. All thresholds, timings and gains are
authored; this repertoire neither reconstructs a koi motor program nor solves
its fluid forces. Signed pitch and roll incidence retain their existing
neutral-relative moment coupling.

Merged ray geometry uses each membrane's basal pivot and the corresponding
side's issued paddle and fore-aft angles. The fins still open for braking and
trail with forward flow.
A shared bind-space field curves each membrane and its embedded rays together.
Loaded surfaces cup, the distal sheet feathers through recovery, and span and
ray position introduce a short compliance delay. Static glides acquire no
extra decorative oscillation. Four bounded controllers retain actual observed
paddle commands for the delayed response; a new control demand is not copied
into an invented earlier carrier phase. Observation occurs after every
completed physics substep, at intervals no larger than 1/120 second, in free
swimming, ascent/descent and the deterministic turn replay. Render cadence
therefore does not discard intermediate issued commands. Pauses preserve the
response, and a reset discards future command history. Frozen fixtures retain
an explicitly stateless authored-cycle function. Smooth ray normals are
reconciled only across the original smooth UV seams after deformation; intentional creases, skin
influences and topology stay separate.
The unsigned fin-demand map uses a compact C2 transition through neutral, with
an authored width of 0.04 in normalized demand, or radians per second for the
yaw-rate contribution to the retained inspection carrier. Managed tissue uses
a separate 0.003-radian C2 positive-part engagement band for loading and
recovery, applied after sampling observed paddle history. This does not make
the whole controller C2: history interpolation remains linear, and existing
speed clamps and other branches retain their own joins. `finPhaseRate` remains
shared by carrier integration and stateless delayed-phase evaluation.
The held `Fine fin corrections` study varies small steering and signed pitch
trim while the body stays fixed. Its carrier is an authored fixed clock;
the `Oblique front` camera exposes both fin surfaces for inspection. The
ordinary swimming studies move the body and use the finite paired-fin task
controller described above.
These bounded deformations approximate the modes
described in [Wang et al.'s koi study](https://doi.org/10.1016/S1672-6529(13)60217-6)
and [active ray-curvature mechanics](https://doi.org/10.1098/rsif.2006.0181);
they do not solve the two sliding ray halves or recover measured tissue forces.
Wang's direct and mirror views reconstruct one observed pectoral fin, not
simultaneous left-right coordination. Hovering and retreating use different
expansion and contraction sequences, with delays across rays. Continuous fin
activity in those tasks does not establish continuous paddling during a
cruise. Cruising and turning are identified as future work, so the study does
not establish bilateral turn timing, reversal hand-offs or glide release.
Comparisons include the Lauder Laboratory's original
[front-view bluegill footage](https://www.youtube.com/watch?v=36Of7uj9naA),
reviewed at quarter speed at media times 8.528094, 11.248222, 15.241736 and
15.550365 seconds. The body heading stays comparatively steady while one fin
presents a broad fan and the other is nearly edge-on. The clip is embedded on
the [author's source page](https://sites.harvard.edu/glauder/research-videos/bluegill-pectoral-fin-locomotion/);
its task, swimming speed and full three-dimensional kinematics were not
established, and no quantitative timing was derived from this viewing.
Bluegill labriform swimming also differs from carp. The
[trout fin-control experiments](https://doi.org/10.1242/jeb.246275)
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
The fan uses its material ray paths so narrowing comes from rotation and bending
rather than shortening its support rays. A shared deformation keeps rays and
membrane together; actual segment lengths and surface folds are checked.
Each sampled ray rotates around its own basal attachment, then integrates
three-dimensional tangent changes at its rest segment lengths. The exported
membrane supplies a material atlas shared with the primary shafts and their
branches. Their actual tube centerlines are checked separately from membrane
paths. The authoring boundary follows distal ray tips; proximal edge margins
no longer create crossing supports near the base. The rest of the fish's
geometry, skin influences, facial detail and skeletal transforms are preserved.
Fixed-length integration is a dominant kinematic constraint, not a claim that
biological rays cannot stretch. [Das et al.'s rainbow-trout experiments](https://doi.org/10.1016/j.actbio.2023.06.029)
retain finite axial elasticity; this field does not solve their nonlinear
hemitrich and collagen mechanics or use their values to calibrate koi.
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
