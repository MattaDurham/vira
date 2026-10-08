# Koi swimming: anatomy and animation model

The animated pond uses a reduced model informed by carp research, with coefficients
tuned visually for a desktop background. It is not a calibrated animal simulation
or a full fluid solver. Fish lengths and water-plane velocities are normalized.

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
Springs run with bounded 120 Hz substeps so a slow render frame cannot explode the
rig. Pause and hidden tabs retain the pose and momentum without advancing time.

The joint stiffness, drag ratios, stroke cadence, fin movement and burst timings
are animation choices, not measured koi parameters. Pectoral movement and roll are
visual approximations; buoyancy and three-dimensional fluid forces are not solved.
