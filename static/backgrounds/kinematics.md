# Koi swimming: anatomy and animation model

The animated pond uses a reduced model informed by carp research, with coefficients
tuned visually for a desktop background. It is not a calibrated animal simulation
or a full fluid solver. Fish lengths and water-plane velocities are normalized;
the rendered world uses metres in a six-metre swimming area beneath open water with a 1.8-metre floor.

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
independent depth spring lifting a stationary fish. Swimmers pitch and propel themselves toward their chosen depth. A conservative
hull envelope keeps the entire bending body above the flat floor.
The maximum tuned burst speed is 0.9 m/s. Fin movements, effective mass, joint
stiffness, drag and burst timing remain animation choices, not measured koi values.
Full three-dimensional fluid flow, detailed buoyancy regulation and fin lift are
not solved.

## Camera and underwater light

The floor, fish and surface occupy the same metre-scaled world and perspective
camera. The water is at y=0, with the deepest floor at y=-1.8; fish depth changes
physical position. Look around moves this same camera, with bounded orbit and zoom.
Natural and cyberpunk looks change materials without rebuilding the world or
resetting the fish. The anatomy view exposes the same moving rig.

[PBRT's dielectric reflection and transmission model](https://www.pbr-book.org/3ed-2018/Reflection_Models/Specular_Reflection_and_Transmission)
informs the air/water exit-point calculation using Snell's law and refractive index
1.333. Submerged vertices are projected through that exit point, preserving depth
ordering and avoiding single-view ray-marching trails. An offscreen pass carries
actual underwater geometry; a procedural blue night sky supplies quiet reflections without a visible horizon.
The surface combines the two with a Fresnel approximation and small wave normals.
[PBRT's transmittance model](https://www.pbr-book.org/4ed/Volume_Scattering/Transmittance)
informs exponential color attenuation with underwater optical path. Deeper fish
lose warm light and contrast, and their image softens beneath the reflected surface.
Turbidity, caustics, wave amplitude and sky illumination are visually tuned.
Refraction assumes a locally flat interface; ripple slopes add a small distortion,
not a full wave-surface ray trace. The procedural materials and lighting are a realistic rendering direction,
not photographic reconstruction.

If the 3D renderer cannot start, the picker explicitly names its flat-water
approximation. That older renderer retains an articulated photo skin and fitted
oblique camera. Graphics-context loss names the failure and retains a static pond;
choosing the pond again retries. Motion freezes for pause, reduced motion and hidden
tabs. The renderer supports native 4K and retina sampling on smaller monitors, with
an 8.4-million-pixel budget and the graphics device texture limit. The underwater
pass matches the output resolution. Motion is capped at 30 frames per second.

## Open water and interaction

The surface and dark floor extend beyond the camera frustum. There are no banks,
landscape props or visible basin edges, including in the orbit view. The legacy
`garden` and `courtyard` preference values remain valid for saved skins; both now
use the open-water scene. The natural and wireframe materials share the simulation.
`open-water.svg` is a code-created picker thumbnail and static fallback, with no
photographic landscape. Earlier generated pond plates remain archived assets;
the fish texture provenance is recorded in `provenance.json`.

Cursor attention blends each fish's wandering target with a loose, moving offset
around the pointer. Clicking the water makes a stronger ripple. Shallow fish near
the impact escape using the existing bend/counterstroke propulsion; more distant
fish can investigate after individual delays. Deeper and faraway swimmers can
ignore the disturbance. Temporary responses expire, and pointer attention clears
when the pointer leaves or passes over desktop controls. There are no food pellets,
feeding controls or feeding state.
