// Research observations and authored implementation are deliberately separate.
// Video links lead to the researchers' supplementary footage, not this render.
export const researchTopics = [
  {
    id: 'head',
    navLabel: 'Head-led turns',
    title: 'A course, then a turn',
    claim: 'In one pond-carp recording, yaw varied more than pitch or roll. Goldfish used directed pitch to reach food.',
    implementation: 'The head changes heading and pitch first; delayed orientation follows down the trunk. Climbing comes from pitched forward thrust against water drag.',
    limitation: 'These are authored choices, not a fitted behavior model. Bluegill turning is a comparison species; the goldfish video constrains sideways movement in a narrow tank slot.',
    sources: [
      { label: 'Tadokoro et al. 2025: head motion and gaze', url: 'https://doi.org/10.1152/jn.00565.2024' },
      { label: 'Source video S1: freely swimming carp', url: 'https://ndownloader.figshare.com/files/51456827' },
      { label: 'Source video S2: goldfish foraging', url: 'https://ndownloader.figshare.com/files/51456824' },
      { label: 'Clark and Tytell 2026: head and tail turn timing in bluegill', url: 'https://doi.org/10.7717/peerj.21161' },
      { label: 'Yang et al. 2009: routine koi turns', url: 'https://doi.org/10.1299/jbse.4.67' },
      { label: 'Howe and Astley 2020: curvature pulses in giant danio turns', url: 'https://doi.org/10.1002/jez.2398' },
      { label: 'Observational reference: original koi pond footage', url: 'https://www.youtube.com/watch?v=5XJteeTP5SA' },
      { label: 'Observational reference: original underwater koi footage', url: 'https://www.youtube.com/watch?v=zhLm3EsDDqc' },
    ],
  },
  {
    id: 'muscle',
    navLabel: 'Muscle wave',
    title: 'Drive, bend, release, glide',
    claim: 'Koi and carp muscle activation preceded body movement in steady flow. Koi also alternate propulsive strokes with coasting.',
    implementation: 'Delayed drive envelopes and a travelling bend act on the articulated spine. Thrust accelerates the fish; water resistance slows the glide.',
    limitation: 'This reduced-order rig does not simulate muscle tissue, measured EMG or fluid dynamics. Real flow can also bend a fish before its muscles respond.',
    sources: [
      { label: 'Afridi et al. 2025: synchronized muscle activity and motion', url: 'https://doi.org/10.1098/rspb.2025.0474' },
      { label: 'Source video M1: real fish with synchronized EMG', url: 'https://ndownloader.figshare.com/files/58032227' },
      { label: 'Yang et al. 2010: koi burst and coast', url: 'https://doi.org/10.5226/jabmech.1.30' },
      { label: 'Wardle et al. 1995: comparative review of activation and bending phase', url: 'https://pubmed.ncbi.nlm.nih.gov/9319534/' },
    ],
  },
  {
    id: 'fins',
    navLabel: 'Fins',
    title: 'Fins change jobs',
    claim: 'Measured koi pectorals expand, bend, cup and undulate during hovering and retreating. Other fishes deploy paired fins to brake and stabilize.',
    implementation: 'Independent paddle cycles separate power and recovery. Fins open for braking, make small steering corrections and trail during a propulsive burst.',
    limitation: 'Fin forces and timing are tuned. Braking evidence comes from bluegill and bass; the coordination video shows archerfish shooting, not koi swimming.',
    sources: [
      { label: 'Wang et al. 2013: three-dimensional koi fin motion', url: 'https://doi.org/10.1016/S1672-6529(13)60217-6' },
      { label: 'Higham 2007: paired fins and braking', url: 'https://biomechanics.ucr.edu/Higham_2007_JEB.pdf' },
      { label: 'Source video: archerfish fin coordination, 250 fps', url: 'https://movie.biologists.com/video/10.1242/jeb.233718/video-1' },
    ],
  },
  {
    id: 'anatomy',
    navLabel: 'Skeleton',
    title: 'Structure beneath the skin',
    claim: 'Carp have a segmented axial skeleton, paired olfactory openings and a moving gill cover with a flexible sealing flap.',
    implementation: 'Rigid centra and cranial plates sit within a deforming skin. Nasal apertures, moving jaw and gill covers, and layered eyes are separate three-dimensional surfaces.',
    limitation: 'The skeleton and face are authored, not CT reconstructions. Iris reflectance draws on zebrafish evidence. XROMM informs carp feeding mechanics, not respiratory timing.',
    sources: [
      { label: 'Al Fatle and Jawad 2026: carp axial skeleton', url: 'https://www.jnhm.uobaghdad.edu.iq/index.php/BINHM/article/view/1263' },
      { label: 'Pashchenko and Kasumyan 2017: olfactory openings', url: 'https://doi.org/10.1134/S0032945217010088' },
      { label: 'Ballintijn 1985: carp opercular valve mechanics', url: 'https://pubmed.ncbi.nlm.nih.gov/4001607/' },
      { label: 'FAO: common carp features and branched fin rays', url: 'https://www.fao.org/fi/static-media/FI/CDrom/aquaculture/I1129m/file/en/en_commoncarp.htm' },
      { label: 'Gur et al. 2018: guanine reflector in the zebrafish iris', url: 'https://doi.org/10.1002/advs.201800338' },
      { label: 'XROMM: common carp jaw protrusion during feeding', url: 'https://xromm.org/projects/fish-feeding/' },
    ],
  },
  {
    id: 'optics',
    navLabel: 'Light & depth',
    title: 'Depth is a light path',
    claim: 'Water absorbs and scatters light along its path. Longer paths attenuate color and contrast; surface reflection changes with viewing angle.',
    implementation: 'The live view clips each camera-to-fragment segment to the water volume, applies extinction and approximate scattering, and adds a moving Fresnel reflection.',
    limitation: 'Water coefficients are tuned, not measured from a tank. Live refraction and scattering remain approximations; the Cycles still traces a more complete light path.',
    sources: [
      { label: 'Pope and Fry 1997: measured pure-water absorption', url: 'https://doi.org/10.1364/AO.36.008710' },
      { label: 'Physically Based Rendering: transmittance', url: 'https://www.pbr-book.org/4ed/Volume_Scattering/Transmittance' },
      { label: 'Physically Based Rendering: dielectric reflection and refraction', url: 'https://www.pbr-book.org/4ed/Reflection_Models/Dielectric_BSDF' },
    ],
  },
  {
    id: 'craft',
    navLabel: 'The build',
    title: 'One authored fish, two renderers',
    claim: 'The live aquarium and offline reference share the same authored fish geometry and skin weights.',
    implementation: 'Blender scripts construct the body, face, scales, fin rays and skeleton. Original AI-generated skin artwork and procedural fin-tissue maps cover that geometry. Stone maps are Poly Haven Dark Rock by Amal Kumar, CC0.',
    limitation: 'Shared assets make comparisons useful, but the lighting models differ. Geometry checks and smooth playback do not establish biological accuracy or photographic quality.',
    sources: [
      { label: 'Blender: glTF geometry, materials and skinning', url: 'https://docs.blender.org/manual/en/5.1/addons/import_export/scene_gltf2.html' },
      { label: 'Three.js: WebGPU renderer and WebGL fallback', url: 'https://threejs.org/docs/pages/WebGPURenderer.html' },
      { label: 'Dark Rock by Amal Kumar: original stone material, CC0', url: 'https://polyhaven.com/a/dark_rock' },
      { label: 'Poly Haven: CC0 asset license', url: 'https://polyhaven.com/license' },
    ],
  },
];

export const buildChapters = [
  {
    id: 'observation',
    title: 'Watch the animal',
    text: 'Compare real turns, climbs, coasts and fin strokes. Keep species and experimental conditions attached to each observation.',
    topics: ['head', 'muscle', 'fins'],
  },
  {
    id: 'geometry',
    title: 'Author the structure',
    text: 'Build a volumetric fish around an articulated spine, then add facial cavities, gill covers, scales and flexible fins.',
    topics: ['anatomy', 'craft'],
  },
  {
    id: 'controls',
    title: 'Connect cause and motion',
    text: 'A chosen course drives head orientation, delayed trunk bending, strokes and fin corrections. Momentum and drag carry the fish through the glide.',
    topics: ['head', 'muscle', 'fins'],
  },
  {
    id: 'verification',
    title: 'Light it, then look again',
    text: 'Compare live light transport with the Cycles reference, inspect the exported moving surfaces, and judge recorded motion against the source footage.',
    topics: ['optics', 'craft'],
  },
];
