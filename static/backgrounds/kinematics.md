# Koi swimming: anatomy and animation model

The animated pond uses a reduced model informed by carp research, with coefficients
tuned visually for a desktop background. It is not a calibrated animal simulation
or a full fluid solver. Fish lengths and water-plane velocities are normalized;
vertical depth uses metres in a nominal six-metre-wide pond.

## Sources and what they informed

- [Common carp vertebral anatomy, micro-CT and elemental microanalysis](https://doi.org/10.1007/s00435%2D024%2D00683%2D2)
  ([accessible manuscript](https://doi.org/10.21203/rs.3.rs-4442332/v1)) describes
  distinct abdominal, transition and caudal regions, intervertebral connections,
  and the terminal caudal skeleton. This supports a connected, regionally varied
  rig rather than deforming the fish as a uniform sheet. Our 24 numerical segments
  are a resolution choice, not a claim about the fish's vertebral count.
- [Body bending during fast-starts in fish can be explained in terms of muscle
  torque and hydrodynamic resistance](https://pubmed.ncbi.nlm.nih.gov/10021321/)
  measures common carp spine curvature and muscle activity. Bending results from
  muscle torque acting against fish inertia and the added mass of water. This
  informed damped joint springs and a delay between muscle effort and body motion.
- [Kinematics and muscle dynamics of C- and S-starts of carp](https://pubmed.ncbi.nlm.nih.gov/9914147/)
  connects strong tail bending to thrust and direction changes. This informed a
  preparatory bend followed by a counterstroke during an accelerating turn.
- [Flow Physics of Routine Turns of Koi Carp](https://www.jstage.jst.go.jp/article/jbse/4/1/4_1_67/_article)
  compares measured and simulated single-beat and cruising turns. This informed
  both transient turns and sustained steering curvature.
- [Undulatory fish swimming: from muscles to flow](https://research.wur.nl/en/publications/undulatory-fish-swimming-from-muscles-to-flow/)
  reviews segmented axial muscles, body undulation, thrust and wakes. This informed
  a wave traveling toward the tail and propulsion coupled to tail strokes.

## Implementation

The skull stays rigid; joint mobility increases into the trunk and tail. Angular
springs and damping follow a traveling preferred-curvature wave and a separate
steering bend. Segment lengths remain fixed. Skin triangles span cross sections
normal to the connected backbone, preserving a continuous curved silhouette and
rotating the photographed scales through bends. Both pectoral regions move
independently, and roll varies along the body.

The normal renderer uploads the sprite images once and updates a small skin
vertex buffer on the GPU. Fish and their shadows render into a transparent target
before water refraction and photographic reflections. If graphics are unavailable,
a named status explains the loss of water effects and short rotated raster columns
keep the fish moving over the photograph.

Muscle effort rises before forward speed. Caudal angular motion modulates thrust;
velocity persists between strokes and decays under forward drag. Stronger lateral
drag resists sliding sideways when heading changes. Steering has angular inertia.
Larger fish have more gradual changes in velocity and heading, using a simplified
area-to-volume scaling for effective inertia; their stroke cadence is also slower.
Springs run with bounded 120 Hz substeps so a slow render frame cannot explode the
rig. Pause and hidden tabs retain the pose and momentum without advancing time.

The joint stiffness, drag ratios, stroke cadence, fin movement and burst timings
are animation choices, not measured koi parameters. Pectoral movement and roll are
visual approximations; buoyancy and full three-dimensional fluid forces are not solved.

## Camera and underwater light

The plate, fish, feeding points and ripple rings share one oblique pinhole camera
with a visually chosen 50-degree elevation. Connected skin cross sections have a
raised dorsal profile and rotate in three dimensions when banking or pitching.
Depth changes projected position and scale. Vertical velocity responds gradually
to a chosen depth, with damping and bounded ascent/descent speed; the fish pitch
into the movement. Feeding requires reaching the surface layer first.

[PBRT's dielectric reflection and transmission model](https://www.pbr-book.org/3ed-2018/Reflection_Models/Specular_Reflection_and_Transmission)
informs Snell-law apparent depth using water's refractive index of 1.333 and a
Fresnel surface reflection approximation. [PBRT's transmittance model](https://www.pbr-book.org/4ed/Volume_Scattering/Transmittance)
informs exponential contrast and color attenuation with optical path length.
Deeper fish progressively soften, lose warm colors and recede beneath the green
medium and photographic reflections. Blur samples use premultiplied sprite color
so transparent edges do not develop dark halos. Camera elevation, apparent-depth
reference ray, pond extinction coefficients and reflected-radiance gain are tuned
approximations: this does not reconstruct an environment map or ray-trace the
photograph. Water distortion is masked to the open pond rather than shifting its
stone banks. The fallback preserves the same projected skin and depth cues with
simplified raster tinting.

## Classic pond settings

The garden and courtyard photographs are generated background plates, not images
of specific gardens. Their design is informed by the broad pond, stone banks and
garden planting shown in the official [Portland Strolling Pond Garden](https://japanesegarden.org/garden-spaces/strolling-pond-garden/)
and [Seattle Japanese Garden's stroll-garden design](https://www.seattlejapanesegarden.org/blog/2019/7/22/the-seattle-japanese-garden-designed-in-the-stroll-garden-style).
Portland's [koi article](https://japanesegarden.org/2024/05/23/koi/) identifies
its lower pond as the koi habitat. The design interpretation here is quiet, deep
open water with planted, intentional banks rather than a visible river-stone bed.
The generated plates contain no fish; all fish are animated beneath the reflected
surface. Exact generation prompts are recorded in `provenance.json`.
