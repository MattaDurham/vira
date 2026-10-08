# Koi swimming: anatomy and animation model

The animated pond uses a reduced model informed by carp research, with coefficients
tuned visually for a desktop background. It is not a calibrated animal simulation
or a full fluid solver. Fish lengths and water-plane velocities are normalized;
the rendered world uses metres in a six-metre-wide pond with a 1.8-metre basin.

## Sources and what they informed

- [Common carp vertebral anatomy, micro-CT and elemental microanalysis](https://doi.org/10.1007/s00435%2D024%2D00683%2D2)
  ([accessible manuscript](https://doi.org/10.21203/rs.3.rs-4442332/v1)) describes
  distinct abdominal, transition and caudal regions, intervertebral connections,
  and the terminal caudal skeleton. This supports a connected, regionally varied
  rig rather than deforming the fish as a uniform sheet. The representative rig uses 34 centra: 11 abdominal, 6 transition and 17 caudal;
  the paper reports variation between specimens. Bone and compliant connective
  tissue are distinct. This is a procedural interpretation, not a micro-CT replica.
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

The normal renderer uses locally bundled Three.js. Each fish has a solid bending
hull, a rigid head, paired pectoral and pelvic fins, and dorsal, anal and vertical
caudal fins. A procedural skeletal view follows the same backbone: biconcave centra,
neural and caudal hemal supports, trunk ribs and terminal caudal supports. Bone is
ivory; intervertebral connective tissue is cyan. Bone proportions, skull outline,
ribs and terminal supports are simplified, and not every cranial or intermuscular
bone is represented. We do not use mammalian ball joints or label the intervertebral
tissue as generic cartilage. The model's joint springs represent compliance; no
measured cartilage constitutive model or calibrated bone material properties are used.

Muscle effort rises before forward speed. Caudal angular motion modulates thrust;
velocity persists between strokes and decays under forward drag. Stronger lateral
drag resists sliding sideways when heading changes. Steering has angular inertia.
Larger fish have more gradual changes in velocity and heading, using a simplified
area-to-volume scaling for effective inertia; their stroke cadence is also slower.
Springs run with bounded 120 Hz substeps so a slow render frame cannot explode the
rig. Pause and hidden tabs retain the pose and momentum without advancing time.

Depth commands first change pitch through angular damping. Tail thrust acts along
the pitched body, with vertical drag and nominal neutral buoyancy; there is no
independent depth spring lifting a stationary fish. Deep fish keep swimming in an
ascending approach near food rather than stopping directly beneath it. A
conservative hull envelope keeps the entire bending body above the sloped floor.
The maximum tuned burst speed is 0.9 m/s. Fin movements, effective mass, joint
stiffness, drag and burst timing remain animation choices, not measured koi values.
Full three-dimensional fluid flow, detailed buoyancy regulation and fin lift are
not solved.

## Camera and underwater light

The basin, fish, food and surface occupy the same metre-scaled world and perspective
camera. The water is at y=0, with the deepest floor at y=-1.8; fish depth changes
physical position. Look around moves this same camera, with bounded orbit and zoom.
Natural and cyberpunk looks change materials without rebuilding the world or
resetting the fish. The anatomy view exposes the same moving rig.

[PBRT's dielectric reflection and transmission model](https://www.pbr-book.org/3ed-2018/Reflection_Models/Specular_Reflection_and_Transmission)
informs the air/water exit-point calculation using Snell's law and refractive index
1.333. Submerged vertices are projected through that exit point, preserving depth
ordering and avoiding single-view ray-marching trails. An offscreen pass carries
actual underwater geometry; a mirrored camera supplies bank and sky reflections.
The surface combines the two with a Fresnel approximation and small wave normals.
[PBRT's transmittance model](https://www.pbr-book.org/4ed/Volume_Scattering/Transmittance)
informs exponential color attenuation with underwater optical path. Deeper fish
lose warm light and contrast, and their image softens beneath the reflected surface.
Turbidity, caustics, wave amplitude and sky illumination are visually tuned.
Refraction assumes a locally flat interface; ripple slopes add a small distortion,
not a full wave-surface ray trace. The garden's procedural materials and lighting
are a realistic rendering direction, not photographic reconstruction.

If the 3D renderer cannot start, the picker explicitly names its photographic
approximation. That older renderer retains an articulated photo skin and fitted
oblique camera. Graphics-context loss names the failure and retains a static pond;
choosing the pond again retries. Motion freezes for pause, reduced motion and hidden
tabs. Render resolution and shadow maps are bounded because the scene runs behind
working desktop windows.

## Classic pond settings

The rendered garden and courtyard use an enclosed basin, stone banks, planting
and a lantern or deck. Their design is informed by the broad pond, stone banks and
garden planting shown in the official [Portland Strolling Pond Garden](https://japanesegarden.org/garden-spaces/strolling-pond-garden/)
and [Seattle Japanese Garden's stroll-garden design](https://www.seattlejapanesegarden.org/blog/2019/7/22/the-seattle-japanese-garden-designed-in-the-stroll-garden-style).
Portland's [koi article](https://japanesegarden.org/2024/05/23/koi/) identifies
its lower pond as the koi habitat. The design interpretation here is quiet, deep
open water with planted, intentional banks rather than a visible river-stone bed.
The older generated plates remain as thumbnails and fallbacks and depict no specific
garden. Their prompts and the dorsal fish textures are recorded in `provenance.json`.
Living Garden and Neon Pond skin manifests include the scene, setting and material
look; applying either carries that preset through the normal synced UI preference.
