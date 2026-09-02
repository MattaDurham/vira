/* World — the 3D graph renderer.

   The module the Visual Network draws through when WebGL is available.
   atlas.js still owns the data, the state object S, and every piece of
   chrome (lenses, legend, groups, the selection card); this file owns the
   things that stop being flat when the graph becomes a volume: the layout,
   the meshes, the picking, and the camera.

   THE NAVIGATION IS COPIED, NOT INVENTED. Rotate, pan and scroll must feel
   exactly like the Image Atlas (chaska's viewer, served at /imageatlas/ and
   published at thedurham.nyc/lab/atlas), so the camera block below is a
   transcription of that viewer's own camera setup - the same vendored
   camera-controls build, the same actions on the same buttons, the same
   smoothing constants, the same dolly-to-cursor, the same bounds expressed
   as multiples of the cloud radius, and the same re-anchor-under-the-cursor
   press handler in the capture phase. Every one of those values is named in
   NAV below and pinned by tests/test_atlas3d_nav.py, which reads the Image
   Atlas viewer itself when it is present on the machine, so the two cannot
   drift apart silently.

   What is deliberately NOT copied: framing. The Image Atlas frames a dense
   ball of photographs; this frames a graph, so the opening distance is fitted
   to the graph's own extent. Framing is not a gesture.

   Picking note: the Image Atlas picks with a GPU id pass because it draws
   tens of thousands of sprites. This draws a few hundred, so picking is CPU
   projection - exact at this size, and it keeps the render path simple. */
"use strict";

import * as THREE from "./vendor/three.module.js";
import CameraControls from "./vendor/camera-controls.module.js";

CameraControls.install({ THREE });

// The Image Atlas's camera contract, in one place so the parity test can
// read it and so nothing here can quietly diverge from that viewer.
export const NAV = {
  fov: 55,
  nearFactor: 0.004,        // near plane scaled to the cloud - a fixed near
  farFactor: 40,            // starves depth precision and the sprites z-fight
  smoothTime: 0.28,
  draggingSmoothTime: 0.14,
  minDistanceFactor: 0.015,
  maxDistanceFactor: 8,
  dollyToCursor: true,
  driftRate: 0.12,          // idle auto-orbit, radians/sec
  driftAfter: 4.0,          // seconds of stillness before it starts
};

const CAM_KEY = "vira-atlas-cam.v1";
const RING_W = 0.17;        // ring band as a fraction of the node's half-size
const LABEL_MAX = 70;       // DOM labels are cheap but not free
const POINT_CLOUD_AT = 2500; // all nodes remain visible; glyph detail adapts
const PHYSICS_GLOBAL_AT = 4000;
const PHYSICS_LOCAL_LIMIT = 1400;

export function create(host) {
  const { stage, S } = host;
  const canvas = document.createElement("canvas");
  canvas.className = "atlas-gl";

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas, antialias: true, alpha: true,
      powerPreference: "high-performance",
    });
  } catch (e) { return null; }
  if (!renderer.getContext()) return null;

  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  // the module's own stage gradient stays visible under the graph
  renderer.setClearColor(0x000000, 0);
  stage.insertBefore(canvas, stage.firstChild);

  const labelHost = document.createElement("div");
  labelHost.className = "atlas-labels";
  stage.insertBefore(labelHost, canvas.nextSibling);

  const scene = new THREE.Scene();
  const clock = new THREE.Clock();

  let camera = null, controls = null;
  let bounds = { center: [0, 0, 0], radius: 600 };
  let needsRender = true, running = false, raf = 0;
  let idleT = 0, interacted = false, restoredCam = false;
  let W = 1, H = 1;

  const nodeMeshes = new Map();     // sim node id -> mesh
  const textures = new Map();       // sim node id -> THREE.Texture
  const labels = new Map();         // sim node id -> span
  let edgeMesh = null, edgeGeo = null, edgeList = [];
  let edgeSegments = 1;
  let edgeIndicesByNode = new Map();
  let pointCloud = null, pointGeo = null, pointMat = null, pointList = [];
  let stars = null;
  let physicsNodes = [], physicsEdges = [], physicsIds = new Set();
  let physicsMode = "local";

  const requestRender = () => { needsRender = true; };
  let lastCards = [];   // what the last pass laid out, for cards()
  let contextLost = false;   // see the context-loss block below

  // ---------- geometry + materials -------------------------------------

  const PLANE = new THREE.PlaneGeometry(1, 1);

  const NODE_VERT = `
    varying vec2 vUv;
    void main(){
      vUv = uv;
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }`;
  // One disc: the avatar inside, the cluster ring around it. The dashed
  // variant is the vault ring - in your notes, not yet met.
  const NODE_FRAG = `
    uniform sampler2D uMap;
    uniform vec3 uRing;
    uniform float uAlpha;
    uniform float uDash;
    uniform float uRingW;
    uniform float uSphere;
    varying vec2 vUv;
    void main(){
      vec2 p = vUv * 2.0 - 1.0;
      float d = length(p);
      float aa = fwidth(d) * 1.2;
      if (d > 1.0 + aa) discard;
      float edge = 1.0 - smoothstep(1.0 - aa, 1.0, d);
      float inner = 1.0 - uRingW;
      vec3 rgb;
      if (d < inner) {
        vec2 uv2 = (p / inner) * 0.5 + 0.5;
        rgb = texture2D(uMap, uv2).rgb;
      } else {
        float on = 1.0;
        if (uDash > 0.5) {
          float a = atan(p.y, p.x) + 3.14159265;
          on = step(mod(a, 0.30), 0.19);
        }
        if (on < 0.5) discard;
        rgb = uRing;
      }
      if (uSphere > 0.5) {
        float z = sqrt(max(0.0, 1.0 - dot(p, p)));
        vec3 normal = normalize(vec3(p.x, -p.y, z));
        vec3 lightDir = normalize(vec3(-0.45, 0.55, 0.72));
        float diffuse = 0.48 + 0.62 * max(0.0, dot(normal, lightDir));
        float specular = pow(max(0.0, dot(normal, lightDir)), 18.0) * 0.32;
        rgb = rgb * diffuse + vec3(specular);
      }
      gl_FragColor = vec4(rgb, uAlpha * edge);
      #include <colorspace_fragment>
    }`;

  // Screen-space expanded lines: WebGL ignores gl_LineWidth, and edge weight
  // reads as thickness in this module's design language, so each tie is a
  // quad widened perpendicular to itself in pixels.
  const EDGE_VERT = `
    uniform vec2 uHalfRes;
    attribute vec3 aOther;
    attribute float aSide;
    attribute float aWidth;
    attribute vec3 aColor;
    attribute float aAlpha;
    varying vec3 vColor;
    varying float vAlpha;
    void main(){
      vec4 c0 = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      vec4 c1 = projectionMatrix * modelViewMatrix * vec4(aOther, 1.0);
      vec2 s0 = (c0.xy / max(abs(c0.w), 1e-6)) * uHalfRes;
      vec2 s1 = (c1.xy / max(abs(c1.w), 1e-6)) * uHalfRes;
      vec2 dir = s1 - s0;
      float len = length(dir);
      dir = len > 1e-5 ? dir / len : vec2(1.0, 0.0);
      vec2 nrm = vec2(-dir.y, dir.x) * aSide * aWidth * 0.5;
      c0.xy += (nrm / uHalfRes) * c0.w;
      gl_Position = c0;
      vColor = aColor;
      vAlpha = aAlpha;
    }`;
  const EDGE_FRAG = `
    varying vec3 vColor;
    varying float vAlpha;
    void main(){
      if (vAlpha <= 0.002) discard;
      gl_FragColor = vec4(vColor, vAlpha);
      #include <colorspace_fragment>
    }`;

  const edgeMat = new THREE.ShaderMaterial({
    uniforms: { uHalfRes: { value: new THREE.Vector2(1, 1) } },
    vertexShader: EDGE_VERT, fragmentShader: EDGE_FRAG,
    transparent: true, depthWrite: false, depthTest: true,
  });

  // A full vault is tens of thousands of nodes. One Mesh + ShaderMaterial +
  // texture per item would spend the frame on draw-call overhead. The large
  // graph path draws every node as one attribute-driven point cloud;
  // selection, kind colors and time filters remain per-node buffer data.
  const POINT_VERT = `
    uniform float uScale;
    attribute float aSize;
    attribute vec3 aColor;
    attribute float aAlpha;
    varying vec3 vColor;
    varying float vAlpha;
    void main(){
      vec4 mv = modelViewMatrix * vec4(position, 1.0);
      gl_Position = projectionMatrix * mv;
      gl_PointSize = clamp(aSize * uScale / max(1.0, -mv.z), 1.5, 72.0);
      vColor = aColor;
      vAlpha = aAlpha;
    }`;
  const POINT_FRAG = `
    uniform float uSphere;
    varying vec3 vColor;
    varying float vAlpha;
    void main(){
      vec2 p = gl_PointCoord * 2.0 - 1.0;
      float d = length(p);
      if (d > 1.0 || vAlpha <= 0.002) discard;
      float edge = 1.0 - smoothstep(0.84, 1.0, d);
      vec3 rgb = vColor;
      if (uSphere > 0.5) {
        float z = sqrt(max(0.0, 1.0 - dot(p, p)));
        vec3 normal = normalize(vec3(p.x, -p.y, z));
        vec3 lightDir = normalize(vec3(-0.45, 0.55, 0.72));
        float diffuse = 0.48 + 0.62 * max(0.0, dot(normal, lightDir));
        float specular = pow(max(0.0, dot(normal, lightDir)), 18.0) * 0.32;
        rgb = rgb * diffuse + vec3(specular);
      }
      gl_FragColor = vec4(rgb, vAlpha * edge);
      #include <colorspace_fragment>
    }`;

  function tileTexture(name, letters) {
    const c = document.createElement("canvas");
    c.width = c.height = 128;
    const g = c.getContext("2d");
    g.fillStyle = host.tileColor(name);
    g.fillRect(0, 0, 128, 128);
    g.fillStyle = "rgba(207,203,194,.92)";
    g.font = "600 56px -apple-system, BlinkMacSystemFont, sans-serif";
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(letters, 64, 68);
    const t = new THREE.CanvasTexture(c);
    t.colorSpace = THREE.SRGBColorSpace;
    return t;
  }

  function faceTexture(img) {
    const t = new THREE.Texture(img);
    t.colorSpace = THREE.SRGBColorSpace;
    t.minFilter = THREE.LinearMipmapLinearFilter;
    t.generateMipmaps = true;
    t.needsUpdate = true;
    return t;
  }

  function textureFor(p) {
    const entry = S.imgs.get(p.id);
    if (entry && entry.ok) return faceTexture(entry.img);
    return tileTexture(p.name, host.initials(p.name));
  }

  // A face that arrives after the mesh was built swaps its texture in place.
  function faceLoaded(id) {
    const mesh = nodeMeshes.get(id);
    if (!mesh) return;
    const entry = S.imgs.get(id);
    if (!entry || !entry.ok) return;
    const old = textures.get(id);
    const t = faceTexture(entry.img);
    textures.set(id, t);
    mesh.material.uniforms.uMap.value = t;
    if (old) old.dispose();
    requestRender();
  }

  // ---------- the graph becomes a volume -------------------------------

  // Golden-angle spiral on a sphere: nodes arrive sorted by cluster, so
  // contiguous positions put each community on its own patch of the shell -
  // the 3D reading of the flat build's angular cluster homes.
  function seed() {
    if (S.fixedLayout) {
      S.nodes.forEach((p) => {
        p.z = Number(p.z || 0);
        p.vx = p.vy = p.vz = 0;
      });
      S.ego.x = S.ego.y = S.ego.z = 0;
      S.ego.vx = S.ego.vy = S.ego.vz = 0;
      return;
    }
    const n = S.nodes.length || 1;
    const gold = Math.PI * (3 - Math.sqrt(5));
    S.nodes.forEach((p, i) => {
      const y = 1 - (i / Math.max(1, n - 1)) * 2;
      const rr = Math.sqrt(Math.max(0, 1 - y * y));
      const th = gold * i;
      const jitter = 0.92 + 0.16 * ((i * 7919) % 13) / 13;
      const R = p.homeR * jitter;
      p.x = Math.cos(th) * rr * R;
      p.y = y * R;
      p.z = Math.sin(th) * rr * R;
      p.vx = p.vy = p.vz = 0;
    });
    S.ego.x = S.ego.y = S.ego.z = 0;
    S.ego.vx = S.ego.vy = S.ego.vz = 0;
  }

  function spring(a, b, rest, k) {
    let dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z;
    const d = Math.hypot(dx, dy, dz) || 1;
    const f = (d - rest) * k;
    dx /= d; dy /= d; dz /= d;
    if (!a.pin) { a.vx += dx * f; a.vy += dy * f; a.vz += dz * f; }
    if (!b.pin) { b.vx -= dx * f; b.vy -= dy * f; b.vz -= dz * f; }
  }

  function refreshPhysics(seed = null, heat = false) {
    const visibleNodes = S.nodes.filter((p) => host.isShown(p));
    physicsNodes = [];
    physicsEdges = [];
    physicsIds = new Set();
    if (!S.physics.enabled) {
      host.onPhysicsScope?.({ mode: "off", count: 0,
                              total: visibleNodes.length });
      S.alpha = 0;
      paint();
      return;
    }

    if (visibleNodes.length <= PHYSICS_GLOBAL_AT) {
      physicsMode = "global";
      physicsNodes = visibleNodes;
      physicsIds = new Set(visibleNodes.map((p) => p.id));
    } else {
      physicsMode = "local";
      const queue = [];
      const add = (p, depth) => {
        if (!p || p.ego || physicsIds.has(p.id) || !host.isShown(p)
            || physicsIds.size >= PHYSICS_LOCAL_LIMIT) return;
        physicsIds.add(p.id);
        physicsNodes.push(p);
        queue.push([p, depth]);
      };
      add(seed || S.dragNode, 0);
      for (const p of S.sel) add(p, 0);
      while (queue.length && physicsIds.size < PHYSICS_LOCAL_LIMIT) {
        const [p, depth] = queue.shift();
        if (depth >= 2) continue;
        for (const row of S.adj.get(p.id) || []) add(row.n, depth + 1);
      }
    }

    const seen = new Set();
    for (const p of physicsNodes) {
      for (const row of S.adj.get(p.id) || []) {
        if (!physicsIds.has(row.n.id) || seen.has(row.e)) continue;
        seen.add(row.e);
        physicsEdges.push(row.e);
      }
    }
    host.onPhysicsScope?.({
      mode: physicsMode, count: physicsNodes.length,
      total: visibleNodes.length, depth: physicsMode === "local" ? 2 : null,
      limited: physicsMode === "local"
        && physicsNodes.length >= PHYSICS_LOCAL_LIMIT,
    });
    if (heat && physicsNodes.length) {
      S.alpha = Math.max(S.alpha || 0, .72);
      setRunning(true);
    }
    paint();
  }

  // Obsidian-style center, repel, link and link-distance controls, with a
  // semantic-home force that keeps the embedding neighborhoods meaningful.
  // Full-vault physics is explicitly local: selected/dragged nodes plus two
  // connection rings. A filtered graph under PHYSICS_GLOBAL_AT runs global.
  function tick(dt) {
    if (!S.physics.enabled || !physicsNodes.length) {
      S.alpha = 0; return;
    }
    const nodes = physicsNodes;
    const range = 180 * S.physics.distance * S.display.scale;
    const range2 = range * range;
    const cells = new Map();
    const cell = (value) => Math.floor(value / Math.max(1, range));
    for (const p of nodes) {
      const key = cell(p.x) + "," + cell(p.y) + "," + cell(p.z);
      if (!cells.has(key)) cells.set(key, []);
      cells.get(key).push(p);
    }
    const repel = 5200 * S.physics.repel;
    for (const a of nodes) {
      const ax = cell(a.x), ay = cell(a.y), az = cell(a.z);
      for (let ox = -1; ox <= 1; ox++)
        for (let oy = -1; oy <= 1; oy++)
          for (let oz = -1; oz <= 1; oz++)
            for (const b of cells.get(
              (ax + ox) + "," + (ay + oy) + "," + (az + oz)) || []) {
              if (a.id >= b.id) continue;
              let dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
              let d2 = dx * dx + dy * dy + dz * dz;
              if (d2 > range2) continue;
              if (d2 < 1) { dx = 1; dy = .5; dz = .3; d2 = 1.34; }
              const d = Math.sqrt(d2);
              const f = repel * (1 - d / range) / Math.max(25, d2);
              const fx = dx / d * f, fy = dy / d * f, fz = dz / d * f;
              if (!a.pin) { a.vx += fx; a.vy += fy; a.vz += fz; }
              if (!b.pin) { b.vx -= fx; b.vy -= fy; b.vz -= fz; }
            }
    }
    for (const e of physicsEdges) {
      if (host.isEdgeShown && !host.isEdgeShown(e)) continue;
      const w = Math.min(1.5, e.weight) / 1.5;
      const rest = (210 - 100 * w) * S.physics.distance * S.display.scale;
      const k = (e.structural ? .032 : .012)
        * S.physics.link * (0.4 + 0.6 * w);
      spring(e.an, e.bn, rest, k);
    }
    for (const p of nodes) {
      p.vx += -p.x * S.physics.center * .0007;
      p.vy += -p.y * S.physics.center * .0007;
      p.vz += -p.z * S.physics.center * .0007;
      p.vx += (p.homeX - p.x) * S.physics.semantic * .003;
      p.vy += (p.homeY - p.y) * S.physics.semantic * .003;
      p.vz += (p.homeZ - p.z) * S.physics.semantic * .003;
      if (p.pin) { p.vx = p.vy = p.vz = 0; continue; }
      p.vx *= 0.84; p.vy *= 0.84; p.vz *= 0.84;
      const sp = Math.hypot(p.vx, p.vy, p.vz);
      const cap = 260 * S.alpha + 20;
      if (sp > cap) { const s = cap / sp; p.vx *= s; p.vy *= s; p.vz *= s; }
      p.x += p.vx * dt * S.alpha * 3.2;
      p.y += p.vy * dt * S.alpha * 3.2;
      p.z += p.vz * dt * S.alpha * 3.2;
    }
    S.alpha = S.dragNode ? Math.max(.28, S.alpha)
                         : Math.max(0, S.alpha - dt * .09);
  }

  function measure() {
    let r = 1;
    for (const p of S.nodes) r = Math.max(r, Math.hypot(p.x, p.y, p.z) + p.r);
    bounds = { center: [0, 0, 0], radius: r };
    return bounds;
  }

  // ---------- scene build ----------------------------------------------

  // `free` is false in exactly one place: rebuilding after a context loss.
  // Those GPU objects died WITH the context, so asking the new one to delete
  // them is an INVALID_OPERATION per object - 184 console warnings on a
  // 378-node graph, and a restore that reports itself as broken.
  function clearScene(free = true) {
    for (const m of nodeMeshes.values()) {
      scene.remove(m);
      if (free) m.material.dispose();
    }
    if (free) for (const t of textures.values()) t.dispose();
    nodeMeshes.clear();
    textures.clear();
    for (const l of labels.values()) l.remove();
    labels.clear();
    if (edgeMesh) {
      scene.remove(edgeMesh);
      if (free) edgeGeo.dispose();
      edgeMesh = null;
    }
    if (pointCloud) {
      scene.remove(pointCloud);
      if (free) {
        pointGeo.dispose();
        pointMat.dispose();
      }
      pointCloud = pointGeo = pointMat = null;
      pointList = [];
    }
  }

  function buildNodes() {
    const all = S.ego ? [...S.nodes, S.ego] : S.nodes;
    if (S.nodes.length > POINT_CLOUD_AT) {
      pointList = all;
      const n = all.length;
      pointGeo = new THREE.BufferGeometry();
      pointGeo.setAttribute("position",
        new THREE.BufferAttribute(new Float32Array(n * 3), 3));
      pointGeo.setAttribute("aSize",
        new THREE.BufferAttribute(new Float32Array(n), 1));
      pointGeo.setAttribute("aColor",
        new THREE.BufferAttribute(new Float32Array(n * 3), 3));
      pointGeo.setAttribute("aAlpha",
        new THREE.BufferAttribute(new Float32Array(n), 1));
      pointMat = new THREE.ShaderMaterial({
        uniforms: { uScale: { value: H / (2 * Math.tan(
          (NAV.fov * Math.PI / 180) / 2)) },
          uSphere: { value: S.display.sphericalNodes ? 1 : 0 } },
        vertexShader: POINT_VERT, fragmentShader: POINT_FRAG,
        transparent: true, depthWrite: true, depthTest: true,
      });
      pointCloud = new THREE.Points(pointGeo, pointMat);
      pointCloud.frustumCulled = false;
      pointCloud.renderOrder = 2;
      scene.add(pointCloud);
      return;
    }
    for (const p of all) {
      const tex = textureFor(p);
      textures.set(p.id, tex);
      const mat = new THREE.ShaderMaterial({
        uniforms: {
          uMap: { value: tex },
          uRing: { value: new THREE.Color(0x6a6a64) },
          uAlpha: { value: 1 },
          uDash: { value: p.vault ? 1 : 0 },
          uRingW: { value: RING_W },
          uSphere: { value: S.display.sphericalNodes ? 1 : 0 },
        },
        vertexShader: NODE_VERT, fragmentShader: NODE_FRAG,
        transparent: true, depthWrite: true, depthTest: true,
      });
      const mesh = new THREE.Mesh(PLANE, mat);
      const size = p.r * 2 / (1 - RING_W);
      mesh.scale.set(size, size, 1);
      mesh.renderOrder = 2;
      mesh.userData.p = p;
      scene.add(mesh);
      nodeMeshes.set(p.id, mesh);
    }
  }

  function buildEdges() {
    edgeList = [];
    edgeIndicesByNode = new Map();
    for (const e of S.egoEdges) edgeList.push({ e, ego: true });
    for (const e of S.edges) edgeList.push({ e, ego: false });
    const n = edgeList.length;
    // Two chords are enough to make a slight arc legible at full-vault
    // scale. Smaller graphs can afford a smoother five-chord curve.
    edgeSegments = S.display.curvedLinks ? (n > 30000 ? 2 : 5) : 1;
    const parts = n * edgeSegments;
    edgeGeo = new THREE.BufferGeometry();
    edgeGeo.setAttribute("position",
      new THREE.BufferAttribute(new Float32Array(parts * 12), 3));
    edgeGeo.setAttribute("aOther",
      new THREE.BufferAttribute(new Float32Array(parts * 12), 3));
    edgeGeo.setAttribute("aSide",
      new THREE.BufferAttribute(new Float32Array(parts * 4), 1));
    edgeGeo.setAttribute("aWidth",
      new THREE.BufferAttribute(new Float32Array(parts * 4), 1));
    edgeGeo.setAttribute("aColor",
      new THREE.BufferAttribute(new Float32Array(parts * 12), 3));
    edgeGeo.setAttribute("aAlpha",
      new THREE.BufferAttribute(new Float32Array(parts * 4), 1));
    const idx = new Uint32Array(parts * 6);
    const side = edgeGeo.getAttribute("aSide").array;
    for (let i = 0; i < n; i++) {
      const edge = edgeList[i].e;
      for (const node of [edge.an, edge.bn]) {
        if (!edgeIndicesByNode.has(node.id))
          edgeIndicesByNode.set(node.id, []);
        edgeIndicesByNode.get(node.id).push(i);
      }
      for (let segment = 0; segment < edgeSegments; segment++) {
        const part = i * edgeSegments + segment, v = part * 4;
        side[v] = -1; side[v + 1] = 1;
        side[v + 2] = -1; side[v + 3] = 1;
        idx.set([v, v + 1, v + 3, v, v + 3, v + 2], part * 6);
      }
    }
    edgeGeo.setIndex(new THREE.BufferAttribute(idx, 1));
    edgeMesh = new THREE.Mesh(edgeGeo, edgeMat);
    edgeMesh.frustumCulled = false;
    edgeMesh.renderOrder = 1;
    scene.add(edgeMesh);
  }

  function buildStars() {
    if (stars) { scene.remove(stars); stars.geometry.dispose(); }
    const n = 700, pos = new Float32Array(n * 3);
    const far = bounds.radius * 7;
    for (let i = 0; i < n; i++) {
      const u = Math.random() * 2 - 1, th = Math.random() * Math.PI * 2;
      const rr = Math.sqrt(1 - u * u);
      const d = far * (1 + Math.random() * 0.9);
      pos[i * 3] = Math.cos(th) * rr * d;
      pos[i * 3 + 1] = u * d;
      pos[i * 3 + 2] = Math.sin(th) * rr * d;
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    stars = new THREE.Points(g, new THREE.PointsMaterial({
      color: 0x3a3a36, size: 1.6, sizeAttenuation: false,
      transparent: true, opacity: 0.55, depthWrite: false,
    }));
    stars.renderOrder = 0;
    scene.add(stars);
  }

  // ---------- camera ----------------------------------------------------
  // Transcribed from the Image Atlas viewer. Do not "tidy" these numbers.

  function buildCamera() {
    const b = bounds;
    if (!camera) {
      camera = new THREE.PerspectiveCamera(
        NAV.fov, W / H, b.radius * NAV.nearFactor, b.radius * NAV.farFactor);
      controls = new CameraControls(camera, canvas);
      controls.dollyToCursor = NAV.dollyToCursor;
      controls.mouseButtons.middle = CameraControls.ACTION.TRUCK;  // middle-drag pans
      controls.mouseButtons.right = CameraControls.ACTION.TRUCK;   // right-drag pans too
      controls.smoothTime = NAV.smoothTime;
      controls.draggingSmoothTime = NAV.draggingSmoothTime;
      controls.addEventListener("update", requestRender);
      controls.addEventListener("control", () => { idleT = 0; interacted = true; });
      controls.addEventListener("rest", saveCam);
      addEventListener("pagehide", saveCam);
      if (host.reducedMotion) {
        controls.smoothTime = 0.01;
        controls.draggingSmoothTime = 0.01;
      }
    } else {
      camera.near = b.radius * NAV.nearFactor;
      camera.far = b.radius * NAV.farFactor;
      camera.updateProjectionMatrix();
    }
    controls.minDistance = b.radius * NAV.minDistanceFactor;
    controls.maxDistance = b.radius * NAV.maxDistanceFactor;
    controls.setTarget(b.center[0], b.center[1], b.center[2], false);
    // framing, not gesture: fit the graph's own extent rather than copying
    // the photo cloud's fixed multiple
    const dist = (b.radius / Math.tan((NAV.fov * Math.PI / 180) / 2)) * 1.06;
    const L = Math.hypot(0.5, 0.35, 1);
    controls.setPosition(b.center[0] + dist * 0.5 / L,
                         b.center[1] + dist * 0.35 / L,
                         b.center[2] + dist * 1.0 / L, false);
    // wherever you left it: restore the last camera pose (per-browser), and
    // hold the idle drift until the first interaction so it doesn't walk away
    try {
      const c = JSON.parse(localStorage.getItem(CAM_KEY));
      if (c && c.p && c.t) {
        controls.setLookAt(c.p[0], c.p[1], c.p[2], c.t[0], c.t[1], c.t[2], false);
        restoredCam = true;
      }
    } catch (_) {}
    requestRender();
  }

  function saveCam() {
    if (!controls) return;
    try {
      const t = controls.getTarget(new THREE.Vector3());
      localStorage.setItem(CAM_KEY, JSON.stringify({
        p: [camera.position.x, camera.position.y, camera.position.z],
        t: [t.x, t.y, t.z],
      }));
    } catch (_) {}
  }

  // ---------- projection + picking --------------------------------------

  const _v = new THREE.Vector3();

  // PROJECT FROM THE CAMERA AS IT IS NOW, NOT AS IT WAS LAST FRAME.
  //
  // camera-controls' update() moves the camera but only refreshes
  // matrixWorldInverse when a focal offset is set, which this module never
  // uses - and .project() reads exactly that matrix. renderer.render() then
  // refreshes it and draws the circles, so anything projected BEFORE the
  // render is a frame behind whatever the render draws.
  //
  // This is NOT the floating-name bug - that was a lost context (see the
  // context-loss block). It was measured while chasing it, and the numbers
  // are small on purpose: projecting each card under the matrix the cards
  // used against the matrix the render drew with gives 0px settled, 2.7px
  // during an ordinary drag and 4.6px during a very fast one. Worth fixing
  // because it costs nothing and the same staleness moves the PICK, but do
  // not read a name sitting a long way from its circle as this.
  //
  // Called once per BATCH (a card pass, a pick), never per node: this is
  // cheap but not free, and both callers project every node in a loop.
  // three.js recomposes from the current position/quaternion, so calling it
  // here and again inside render() is idempotent.
  function syncCamera() {
    if (camera) camera.updateMatrixWorld();
  }

  function screenOf(p) {
    _v.set(p.x, p.y, p.z).project(camera);
    if (_v.z >= 1 || _v.z <= -1) return null;
    return { x: (_v.x * 0.5 + 0.5) * W, y: (-_v.y * 0.5 + 0.5) * H, z: _v.z };
  }

  const visible = (p) => p.ego
    ? (!S.hideEgo && !S.shown) : host.isShown(p);

  // screen radius in px for a node at its world size
  function screenR(p) {
    const d = Math.max(1, camera.position.distanceTo(_v.set(p.x, p.y, p.z)));
    return p.r * (H / 2) / (d * Math.tan((camera.fov * Math.PI / 180) / 2));
  }

  // The forgiving pick the Image Atlas uses: the thing under the cursor,
  // else the front-most within reach - an exact 1px hit misses most clicks.
  function pickAt(x, y, reach = 34) {
    // the same staleness lands a click on the node a moving graph has just
    // left, so the pick projects from the current camera too
    syncCamera();
    let best = null, bestZ = Infinity;
    const all = S.ego ? [...S.nodes, S.ego] : S.nodes;
    for (const p of all) {
      if (!visible(p)) continue;
      const s = screenOf(p);
      if (!s) continue;
      const r = Math.max(screenR(p), 7);
      const d = Math.hypot(s.x - x, s.y - y);
      if (d <= r + 2 && s.z < bestZ) { bestZ = s.z; best = p; }
    }
    if (best) return best;
    let near = null, nearD = reach;
    for (const p of all) {
      if (!visible(p)) continue;
      const s = screenOf(p);
      if (!s) continue;
      const d = Math.hypot(s.x - x, s.y - y);
      if (d < nearD) { nearD = d; near = p; }
    }
    return near;
  }

  // Every press re-anchors the orbit under the cursor, so a click-hold drag
  // rotates around the node you grabbed rather than the screen centre. Two
  // guards, and each is a property the first transcription of this function
  // lost. The viewer it came from is chaska's Image Atlas.
  //
  // (a) A SELECTION IS THE ORBIT POINT, and is never re-anchored away. The
  //     viewer spells this `if (anchorId !== null) return;` and its comment
  //     says exactly what dropping it costs: re-anchoring under the cursor
  //     "would swing the pinned image off its spot on the very next drag".
  //     Measured on the real 200-node graph without it: with a person
  //     selected, a 60px drag on empty sky moved the orbit point 810 world
  //     units and swung that person 303px across the screen, taking every
  //     name plate with it. With it, the same drag moves them 4.9px.
  //
  // (b) EMPTY SKY KEEPS THE ORBIT POINT IT ALREADY HAD. The viewer falls
  //     back to where the cursor ray crosses a plane through the cloud, which
  //     is fine for a cloud that fills the frame - the hit lands inside it.
  //     A Vira graph is a ball with empty sky all around, so that fallback
  //     anchors the orbit far outside the graph and the next drag swings the
  //     whole thing through an enormous arc. Measured without this guard:
  //     three presses on empty sky walked the orbit point to 1,120 units from
  //     a centre whose radius is 380 and inflated the camera distance from
  //     774 to 1,524, each press compounding the last. Clamping the hit into
  //     the ball was tried and is not enough - it still jumped between
  //     opposite poles of the sphere and still climbed 473 -> 780 -> 889.
  //     Keeping the existing point is the honest answer and needs no magic
  //     radius: you grabbed nothing, so there is nothing new to rotate about,
  //     and the gesture still orbits a real point.
  function anchorOrbit(x, y) {
    if (S.sel.size) return;                     // (a)
    const p = pickAt(x, y, 150);
    if (p) controls.setOrbitPoint(p.x, p.y, p.z);   // (b): no node, no change
  }

  // ---------- paint (the flat build's draw(), in three dimensions) -------

  function nodeAlpha(p) {
    let alpha = 1;
    if (S.sel.size) {
      if (S.sel.has(p)) alpha = 1;
      else if (S.selPathNodes.has(p.id)) alpha = 0.95;
      else if (S.shared.has(p.id)) alpha = 0.9;
      else if (S.neighbors.has(p.id)) alpha = S.sel.size === 1 ? 0.8 : 0.3;
      else alpha = 0.08;
      if (p.ego) alpha = Math.max(alpha, 0.35);
      if (p === S.hover) alpha = Math.max(alpha, 0.7);
    } else if (host.matchDim(p) && !p.ego) {
      alpha = 0.22;
    }
    return alpha * S.display.nodeOpacity;
  }

  function paintNodes() {
    if (pointCloud) {
      pointMat.uniforms.uSphere.value = S.display.sphericalNodes ? 1 : 0;
      const pos = pointGeo.getAttribute("position");
      const size = pointGeo.getAttribute("aSize");
      const color = pointGeo.getAttribute("aColor");
      const alpha = pointGeo.getAttribute("aAlpha");
      const shade = new THREE.Color();
      for (let i = 0; i < pointList.length; i++) {
        const p = pointList[i];
        const show = visible(p);
        pos.setXYZ(i, p.x, p.y, p.z);
        alpha.setX(i, show ? nodeAlpha(p) : 0);
        const emphasized = S.sel.has(p) || p === S.hover;
        size.setX(i, p.r * 2 * (emphasized ? 1.5 : 1));
        const value = p.ego ? "#a39c8d"
          : S.sel.has(p) ? "#d4ccba"
          : (p.band && S.colors.get(p.band)) || "#6a6a64";
        shade.set(value);
        color.setXYZ(i, shade.r, shade.g, shade.b);
      }
      pos.needsUpdate = size.needsUpdate = true;
      color.needsUpdate = alpha.needsUpdate = true;
      return;
    }
    for (const mesh of nodeMeshes.values()) {
      const p = mesh.userData.p;
      const show = visible(p);
      mesh.visible = show;
      if (!show) continue;
      mesh.position.set(p.x, p.y, p.z);
      const meshSize = p.r * 2 / (1 - RING_W);
      mesh.scale.set(meshSize, meshSize, 1);
      const u = mesh.material.uniforms;
      u.uAlpha.value = nodeAlpha(p);
      u.uSphere.value = S.display.sphericalNodes ? 1 : 0;
      const isSel = S.sel.has(p);
      const col = p.ego ? "#a39c8d"
        : isSel ? "#d4ccba"
        : (p.band && S.colors.get(p.band)) || "#6a6a64";
      u.uRing.value.set(col);
      u.uRingW.value = isSel || p === S.hover ? RING_W * 1.5 : RING_W;
    }
  }

  // The flat build's edge branches, verbatim - only the destination changed.
  function edgeStyle(e, ego, hasSel, focus) {
    const w = Math.min(1.5, e.weight) / 1.5;
    if (ego) {
      const hot = hasSel ? S.sel.has(e.bn)
                         : focus && (e.bn === focus || S.ego === focus);
      return hot ? [138, 132, 120, 0.4, 1.4]
                 : [138, 132, 120, hasSel ? 0.02 : 0.05, 1];
    }
    const hot = focus && (e.an === focus || e.bn === focus);
    if (hasSel && S.selEdges.has(e))
      return [222, 214, 197, 0.95, 1.6 + 2.2 * w];
    if (hasSel && S.selPathEdges.has(e))
      return [163, 156, 141, 0.75, 1.3 + 1.2 * w];
    if (hasSel && (S.sel.has(e.an) || S.sel.has(e.bn))) {
      const spoke = S.sel.size === 1 ? 0.45 : 0.16;
      return [207, 203, 194, spoke * (0.5 + 0.5 * w), 0.8 + 1.4 * w];
    }
    if (hasSel) return [143, 141, 133, 0.015 + 0.03 * w, 0.6 + w];
    if (hot) return e.shared_interest
      ? [138, 132, 120, 0.85, 1 + 2 * w]
      : [207, 203, 194, 0.55, 1 + 2 * w];
    let alpha = 0.05 + 0.3 * w * w;
    if (S.shown) alpha = Math.min(0.8, alpha * 3 + 0.12);
    if (host.matchDim(e.an) || host.matchDim(e.bn)) alpha *= 0.2;
    return e.shared_interest
      ? [138, 132, 120, alpha + 0.08, 0.6 + 1.8 * w]
      : [143, 141, 133, alpha, 0.6 + 1.8 * w];
  }

  function paintEdges() {
    if (!edgeMesh) return;
    const pos = edgeGeo.getAttribute("position");
    const oth = edgeGeo.getAttribute("aOther");
    const wid = edgeGeo.getAttribute("aWidth");
    const col = edgeGeo.getAttribute("aColor");
    const alp = edgeGeo.getAttribute("aAlpha");
    const hasSel = S.sel.size > 0;
    const focus = hasSel ? null : S.hover;
    for (let i = 0; i < edgeList.length; i++) {
      const { e, ego } = edgeList[i];
      const hide = ego ? (S.hideEgo || !!S.shown)
        : (host.isEdgeShown ? !host.isEdgeShown(e)
                           : S.shown && !(S.shown.has(e.an.id)
                                          && S.shown.has(e.bn.id)));
      let r = 0, g = 0, b = 0, a = 0, lw = 1;
      if (!hide) {
        const st = edgeStyle(e, ego, hasSel, focus);
        r = st[0] / 255; g = st[1] / 255; b = st[2] / 255; a = st[3];
        lw = st[4] * S.display.linkThickness;
      }
      const A = e.an, B = e.bn;
      writeEdgePositions(i, A, B, pos, oth);
      for (let segment = 0; segment < edgeSegments; segment++) {
        const v = (i * edgeSegments + segment) * 4;
        for (let k = 0; k < 4; k++) {
          col.setXYZ(v + k, r, g, b);
          alp.setX(v + k, a);
          wid.setX(v + k, lw);
        }
      }
    }
    pos.needsUpdate = oth.needsUpdate = wid.needsUpdate = true;
    col.needsUpdate = alp.needsUpdate = true;
  }

  function paintMovingEdges(ids) {
    if (!edgeMesh || !ids?.size) return;
    const indexes = new Set();
    for (const id of ids)
      for (const index of edgeIndicesByNode.get(id) || []) indexes.add(index);
    const pos = edgeGeo.getAttribute("position");
    const oth = edgeGeo.getAttribute("aOther");
    for (const i of indexes) {
      const e = edgeList[i].e;
      writeEdgePositions(i, e.an, e.bn, pos, oth);
    }
    pos.needsUpdate = oth.needsUpdate = true;
  }

  const edgeArc = new Float64Array(3);

  function writeEdgePositions(index, A, B, pos, oth) {
    let ax = 0, ay = 0, az = 0;
    if (S.display.curvedLinks && S.display.linkCurve > 0) {
      const dx = B.x - A.x, dy = B.y - A.y, dz = B.z - A.z;
      const length = Math.hypot(dx, dy, dz);
      let nx = -dy, ny = dx, nz = 0;
      let normal = Math.hypot(nx, ny);
      if (normal < 1e-6) {
        nx = 0; ny = -dz; nz = dy;
        normal = Math.hypot(ny, nz);
      }
      const sign = index % 2 ? 1 : -1;
      const amplitude = length * S.display.linkCurve * sign
        / Math.max(normal, 1e-6);
      ax = nx * amplitude; ay = ny * amplitude; az = nz * amplitude;
    }
    edgeArc[0] = ax; edgeArc[1] = ay; edgeArc[2] = az;
    for (let segment = 0; segment < edgeSegments; segment++) {
      const t0 = segment / edgeSegments;
      const t1 = (segment + 1) / edgeSegments;
      const q0 = 4 * t0 * (1 - t0), q1 = 4 * t1 * (1 - t1);
      const x0 = A.x + (B.x - A.x) * t0 + edgeArc[0] * q0;
      const y0 = A.y + (B.y - A.y) * t0 + edgeArc[1] * q0;
      const z0 = A.z + (B.z - A.z) * t0 + edgeArc[2] * q0;
      const x1 = A.x + (B.x - A.x) * t1 + edgeArc[0] * q1;
      const y1 = A.y + (B.y - A.y) * t1 + edgeArc[1] * q1;
      const z1 = A.z + (B.z - A.z) * t1 + edgeArc[2] * q1;
      const v = (index * edgeSegments + segment) * 4;
      pos.setXYZ(v, x0, y0, z0); pos.setXYZ(v + 1, x0, y0, z0);
      pos.setXYZ(v + 2, x1, y1, z1); pos.setXYZ(v + 3, x1, y1, z1);
      oth.setXYZ(v, x1, y1, z1); oth.setXYZ(v + 1, x1, y1, z1);
      oth.setXYZ(v + 2, x0, y0, z0); oth.setXYZ(v + 3, x0, y0, z0);
    }
  }

  // ---------- name cards -------------------------------------------------
  //
  // A NAME BELONGS TO ITS CIRCLE, NEVER TO THE AIR BESIDE IT. Free-floating
  // labels were this module's worst artefact: a DOM label is a fixed 11px
  // whatever the depth of the node it names, so a contact 3,000 units back
  // drew a sub-pixel dot and a full-size name - a screen of text with no
  // visible owner, each name swimming independently as the camera moved.
  //
  // The contract is the standard level-of-detail one for 3D graphs, and its
  // three parts are all load-bearing:
  //
  //   LOD        a node earns its name only once its own circle is big
  //              enough to carry it (CARD_MIN_R), fading up over CARD_FADE
  //              so nothing pops in. There is NO exception - not selection,
  //              not the ego. An exception is exactly how floating text
  //              comes back, and nothing needs one: a circle too small to
  //              carry a name is read by hovering it, and the tooltip is
  //              anchored to the CURSOR, so it cannot float either.
  //   ANCHOR     the card hangs off the circle's own rim and scales with it,
  //              so it reads as part of the node rather than beside it.
  //   DECLUTTER  cards that would overlap are dropped by the map-labelling
  //              rule - greedy, highest priority first - so a dense cluster
  //              shows the few names it has room for instead of a pile.
  //
  // A second line (company, circle, or how you know them) appears only once
  // the circle is bigger still, so the card grows into the room it has.

  const CARD_MIN_R = 11;   // screen radius (px) at which a node earns a name
  const CARD_FADE = 7;     // radius over which it fades up
  const CARD_SUB_R = 24;   // radius at which the second line has room
  const CARD_MAX = 44;     // hard ceiling; the greedy pass rarely gets here
  const CARD_GAP = 3;      // px between the circle's rim and its name

  // Text width WITHOUT a layout pass: measured once per string at a
  // reference size in a 2D context and scaled from there. Reading
  // offsetWidth per card per frame would force a synchronous layout on every
  // camera move, which is the one thing a per-frame path must not do.
  const _measure = document.createElement("canvas").getContext("2d");
  const _wCache = new Map();
  let _cardFont = "";

  function textW(s, weight) {
    const key = weight + " " + s;
    let w = _wCache.get(key);
    if (w === undefined) {
      _measure.font = weight + " 100px " + _cardFont;
      w = _measure.measureText(s).width / 100;
      _wCache.set(key, w);
    }
    return w;
  }

  // A card is a caption, not a record. These fields are free text and some
  // of them are long - a company field on this graph holds a full street
  // address, and another holds a parenthesised description of the firm - so
  // an untrimmed second line makes one card wider than the cluster it sits
  // in and pushes every neighbour's card out of the greedy pass.
  const SUB_MAX = 30;
  const NAME_MAX = 22;

  function trim(t, max) {
    // Cut at the first "(" rather than stripping a balanced pair: one real
    // company field on this graph opens a parenthetical and never closes it,
    // and a balanced-pair strip leaves that one untouched - it came out as
    // the truncated head of the parenthetical instead - the wide, ugly card
    // this trimming exists to prevent, on the one row that most needed it.
    t = String(t || "").split("(")[0];
    t = t.split(/\s*[,|\u00b7]\s*/)[0].trim();   // the head, not the address
    return t.length > max ? t.slice(0, max - 1).trimEnd() + "\u2026" : t;
  }

  // What the card says under the name, when it has room for a second line.
  // Deliberately nothing rather than the contact's DEGREE: almost everyone
  // on this graph is 1st, so a line reading "1st" is a line that says
  // nothing under most of the names on screen.
  function cardSub(p) {
    if (p.ego) return "";
    if (p.company) return trim(p.company, SUB_MAX);
    const band = S.bands && S.bands.find((b) => b.id === p.band);
    if (band && band.label) return trim(band.label, SUB_MAX);
    if (p.vault) return trim(p.qualifier || "in your notes", SUB_MAX);
    return "";
  }

  function paintLabels() {
    // A NAME OVER A DEAD CANVAS IS THE FLOATING TEXT THIS MODULE FORBIDS.
    // The cards are DOM and the circles are WebGL, so they fail apart: when
    // the context is gone the canvas holds its last frame (or clears) while
    // this pass keeps happily re-projecting names against a camera that is
    // still drifting. Within seconds every name has walked off its circle -
    // measured at 180-361px after six seconds of idle auto-orbit.
    if (contextLost) return;
    syncCamera();
    if (!_cardFont)
      _cardFont = getComputedStyle(labelHost).fontFamily || "sans-serif";

    // --- every node that clears the level-of-detail floor ---
    const want = [];
    const all = S.ego ? [...S.nodes, S.ego] : S.nodes;
    for (const p of all) {
      if (!visible(p)) continue;
      const a = nodeAlpha(p);
      if (a <= 0.25) continue;
      const r = screenR(p);
      if (r < CARD_MIN_R) continue;             // the whole fix, in one line
      const s = screenOf(p);
      if (!s) continue;
      const fs = Math.max(10, Math.min(17, r * 0.42));
      const name = trim(p.ego ? p.name : host.firstLast(p.name), NAME_MAX);
      const sub = r >= CARD_SUB_R ? cardSub(p) : "";
      const w = Math.max(textW(name, 600) * fs,
                         sub ? textW(sub, 400) * (fs * 0.82) : 0);
      const h = fs * 1.25 + (sub ? fs * 1.05 : 0);
      const top = s.y + r + CARD_GAP;
      if (top > H || top + h < 0) continue;
      // A card is for a node you can SEE. Without this the clamp below drags
      // an off-stage node's name back into view, which is a floating label
      // again - it sits over the graph with its circle nowhere on screen.
      if (s.x < 0 || s.x > W || s.y < 0 || s.y > H) continue;
      // Held inside the stage. The card is clipped by .atlas-labels, so a
      // node near an edge would otherwise show a half-name cut off mid-word
      // at the border; the vertical anchor is untouched, so the card still
      // plainly belongs to the circle directly above it.
      const cx = w / 2 + 2 > W - w / 2 - 2
        ? W / 2 : Math.max(w / 2 + 2, Math.min(W - w / 2 - 2, s.x));
      // priority decides who survives a collision, never who is drawn first
      const rank = S.sel.has(p) ? 3 : (p === S.hover || p.ego ? 2 : 0);
      want.push({ p, s, r, fs, name, sub, a, rank, cx,
                  x0: cx - w / 2, x1: cx + w / 2, y0: top, y1: top + h });
    }

    // Circles a card must not be written across. A DOM card always paints
    // above the canvas, so without this a distant contact's name lands on
    // the FACE of someone standing in front of them - which reads as the
    // floating text this replaced, only worse, because it now looks like it
    // belongs to the face underneath.
    const occ = [];
    for (const p of all) {
      if (!visible(p) || nodeAlpha(p) <= 0.08) continue;
      const r = screenR(p);
      if (r < 6) continue;
      const s = screenOf(p);
      if (!s) continue;
      occ.push({ p, z: s.z,
                 x0: s.x - r, x1: s.x + r, y0: s.y - r, y1: s.y + r });
    }

    // --- the map-labelling rule: greedy, highest priority first ---
    want.sort((m, n) => (n.rank - m.rank) || (n.r - m.r) || (m.s.z - n.s.z));
    const keep = [];
    const hits = (a, b) =>
      a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0;
    for (const c of want) {
      if (keep.length >= CARD_MAX) break;
      let clash = false;
      for (const k of keep) {
        if (hits(c, k)) { clash = true; break; }
      }
      if (!clash) {
        for (const o of occ) {
          if (o.p === c.p || o.z >= c.s.z) continue;   // behind us, or us
          // the card written across a nearer face, or the node itself
          // standing behind one - either way there is nothing to label
          if (hits(c, o)
              || (c.s.x > o.x0 && c.s.x < o.x1
                  && c.s.y > o.y0 && c.s.y < o.y1)) {
            clash = true;
            break;
          }
        }
      }
      if (!clash) keep.push(c);
    }

    // --- paint ---
    const live = new Set();
    for (const c of keep) {
      const p = c.p;
      live.add(p.id);
      let node = labels.get(p.id);
      if (!node) {
        node = document.createElement("span");
        node.className = "atlas-label";
        node.appendChild(document.createElement("b"));
        node.appendChild(document.createElement("i"));
        labelHost.appendChild(node);
        labels.set(p.id, node);
      }
      const nm = node.firstChild, sb = node.lastChild;
      if (nm.textContent !== c.name) nm.textContent = c.name;
      if (sb.textContent !== c.sub) sb.textContent = c.sub;
      sb.style.display = c.sub ? "" : "none";
      node.classList.toggle("ego", !!p.ego);
      node.classList.toggle("sel", S.sel.has(p));
      node.style.fontSize = c.fs.toFixed(1) + "px";
      node.style.transform = "translate(-50%,0) translate("
        + c.cx.toFixed(1) + "px," + c.y0.toFixed(1) + "px)";
      // fade up with the circle, so a name never arrives at full strength on
      // a node that has only just become big enough to own one
      const grow = Math.min(1, (c.r - CARD_MIN_R) / CARD_FADE);
      node.style.opacity = (Math.min(1, c.a) * grow).toFixed(2);
      node.style.zIndex = String(1000 - Math.round(c.s.z * 900));
    }
    for (const [id, node] of labels) {
      if (!live.has(id)) { node.remove(); labels.delete(id); }
    }
    lastCards = keep;
  }

  function paint() {
    if (!camera) return;
    paintNodes();
    paintEdges();
    requestRender();
  }

  // ---------- loop -------------------------------------------------------

  const _cq = new THREE.Quaternion();

  // ---------- context loss ---------------------------------------------
  //
  // A WebGL context is not ours to keep. The browser drops it under GPU
  // memory pressure, on wake from sleep, and - the case this module invites
  // - when a tab holds more live contexts than it is allowed: Vira can have
  // this graph, the Image Atlas viewer and the Flows board alive at once,
  // and Chrome evicts the least recently used one WITHOUT telling the page.
  // renderer.render() then silently does nothing, which is why this went
  // unnoticed: no throw, no console error, every measurement still correct.
  //
  // So the loss is handled rather than assumed away: the loop stops, every
  // name card is removed (the circles are gone, so their names must go with
  // them), and the stage says what happened instead of showing a picture
  // that is quietly no longer being drawn.

  function dropLabels() {
    for (const l of labels.values()) l.remove();
    labels.clear();
    lastCards = [];
  }

  canvas.addEventListener("webglcontextlost", (e) => {
    // preventDefault is what makes the context restorable at all - without
    // it the browser never fires webglcontextrestored and the module is
    // dead until the view is rebuilt by hand.
    e.preventDefault();
    contextLost = true;
    setRunning(false);
    dropLabels();
    stage.classList.add("atlas-lost");
  });

  canvas.addEventListener("webglcontextrestored", () => {
    contextLost = false;
    stage.classList.remove("atlas-lost");
    // Every GPU-side object died with the context. The LAYOUT and the CAMERA
    // did not, and must not be rebuilt: re-seeding would scatter the graph
    // and rebuilding the camera would throw away where the owner was
    // looking. So this rebuilds the meshes only, then repaints in place.
    clearScene(false);
    // buildStars disposes the old field on the way in, for the same reason
    if (stars) { scene.remove(stars); stars = null; }
    buildNodes();
    buildEdges();
    buildStars();
    paint();
    setRunning(true);
  });


  function frame() {
    if (!running) return;
    raf = requestAnimationFrame(frame);
    // the loop can be started before setGraph() has built the camera and
    // controls - the IntersectionObserver calls setRunning(true) on its own
    // schedule, and atlasLoad() is async - so this frame has nothing to draw
    // yet. paint() already guards `camera`; without the same guard here the
    // loop threw on controls.update() every frame instead (observed: a stage
    // of console errors and a permanently black graph).
    if (!controls || !camera) return;
    const dt = Math.min(0.05, clock.getDelta());
    idleT += dt;
    if (S.alpha > 0.005) {
      tick(dt); paintNodes(); paintMovingEdges(physicsIds); needsRender = true;
    }
    // slow drift - the Image Atlas's idle auto-orbit, paused while a
    // selection owns the stage (its analogue of a focused photo)
    if (S.display.autoRotate && !host.reducedMotion
        && idleT > NAV.driftAfter && !S.sel.size
        && (interacted || !restoredCam)) {
      controls.azimuthAngle += NAV.driftRate * dt;
      needsRender = true;
    }
    const updated = controls.update(dt);
    if (updated || needsRender) {
      _cq.copy(camera.quaternion);
      for (const m of nodeMeshes.values()) if (m.visible) m.quaternion.copy(_cq);
      paintLabels();
      renderer.render(scene, camera);
      needsRender = false;
    }
  }

  function setRunning(on) {
    if (on === running) return;
    running = on;
    if (on) { clock.getDelta(); raf = requestAnimationFrame(frame); }
    else { cancelAnimationFrame(raf); saveCam(); }
  }

  function wake(heat = 0.6) {
    if (!physicsNodes.length) refreshPhysics();
    if (!S.physics.enabled || !physicsNodes.length) {
      S.alpha = 0; paint(); return;
    }
    S.alpha = Math.max(S.alpha || 0, heat);
    if (host.reducedMotion) {
      for (let i = 0; i < 260; i++) tick(1 / 60);
      S.alpha = 0;
      measure();
    }
    paint();
  }

  function resize() {
    const w = stage.clientWidth, h = stage.clientHeight;
    if (!w || !h) return;
    W = w; H = h;
    renderer.setSize(w, h, false);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    edgeMat.uniforms.uHalfRes.value.set(w / 2, h / 2);
    if (pointMat)
      pointMat.uniforms.uScale.value = h / (2 * Math.tan(
        (NAV.fov * Math.PI / 180) / 2));
    if (camera) {
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    }
    requestRender();
  }

  // ---------- input ------------------------------------------------------

  const xy = (e) => {
    const r = canvas.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  };
  let downXY = [0, 0], rightXY = [0, 0];
  let drag3 = null;
  const dragRay = new THREE.Raycaster();
  const dragPlane = new THREE.Plane();
  const dragHit = new THREE.Vector3();
  const dragNormal = new THREE.Vector3();
  const dragMouse = new THREE.Vector2();

  function pointOnDragPlane(x, y, target) {
    dragMouse.set(x / W * 2 - 1, -(y / H) * 2 + 1);
    dragRay.setFromCamera(dragMouse, camera);
    return dragRay.ray.intersectPlane(dragPlane, target);
  }

  // capture phase: the orbit re-anchor must run BEFORE camera-controls'
  // own pointerdown snapshots its drag state, or the anchor is ignored
  // mid-gesture
  canvas.addEventListener("pointerdown", (e) => {
    const [x, y] = xy(e);
    downXY = [x, y];
    if (e.button === 2) rightXY = [e.clientX, e.clientY];
    anchorOrbit(x, y);          // every press, as the Image Atlas does
    if (e.button !== 0 || e.shiftKey || !camera || !controls) return;
    const p = pickAt(x, y, 0);
    if (!p || p.ego) return;
    camera.getWorldDirection(dragNormal);
    dragPlane.setFromNormalAndCoplanarPoint(
      dragNormal, dragHit.set(p.x, p.y, p.z));
    const hit = pointOnDragPlane(x, y, new THREE.Vector3());
    drag3 = {
      p, x, y, moved: false,
      offset: hit ? new THREE.Vector3(p.x, p.y, p.z).sub(hit)
                  : new THREE.Vector3(),
    };
    S.dragNode = p;
    p.pin = true;
    controls.enabled = false;
    canvas.setPointerCapture(e.pointerId);
    canvas.style.cursor = "grabbing";
    refreshPhysics(p, true);
    e.preventDefault();
    e.stopImmediatePropagation();
  }, true);

  canvas.addEventListener("pointermove", (e) => {
    if (!drag3) return;
    const [x, y] = xy(e);
    const hit = pointOnDragPlane(x, y, dragHit);
    if (hit) {
      hit.add(drag3.offset);
      drag3.p.x = hit.x; drag3.p.y = hit.y; drag3.p.z = hit.z;
      drag3.moved ||= Math.hypot(x - drag3.x, y - drag3.y) > 3;
      S.alpha = Math.max(S.alpha || 0, .72);
      paintNodes();
      paintMovingEdges(new Set([drag3.p.id]));
      requestRender();
    }
    e.preventDefault();
    e.stopImmediatePropagation();
  }, true);

  const finishNodeDrag = (e, cancelled = false) => {
    if (!drag3) return;
    const { p, moved } = drag3;
    drag3 = null;
    p.pin = false;
    S.dragNode = null;
    if (controls) controls.enabled = true;
    canvas.style.cursor = moved ? "grab" : "pointer";
    try { canvas.releasePointerCapture(e.pointerId); } catch { /* released */ }
    if (!cancelled && !moved) host.onSelect(p);
    else if (!cancelled) refreshPhysics(p, true);
    e.preventDefault();
    e.stopImmediatePropagation();
  };
  canvas.addEventListener("pointerup", (e) => finishNodeDrag(e), true);
  canvas.addEventListener("pointercancel",
    (e) => finishNodeDrag(e, true), true);

  canvas.addEventListener("pointermove", (e) => {
    const [x, y] = xy(e);
    const p = pickAt(x, y, 0);
    if (p !== S.hover) {
      S.hover = p;
      canvas.style.cursor = "grab";
      host.onHover(p, x, y);
      paint();
    } else if (p) {
      host.onHover(p, x, y, true);
    }
  });

  canvas.addEventListener("pointerup", (e) => {
    if (e.button !== 0) return;
    const [x, y] = xy(e);
    if (Math.hypot(x - downXY[0], y - downXY[1]) > 5) return;   // was a drag
    const p = pickAt(x, y);
    if (p && !p.ego) host.onSelect(p);
    else if (!p) host.onEmpty();
  });

  canvas.addEventListener("dblclick", (e) => {
    const [x, y] = xy(e);
    const p = pickAt(x, y);
    if (p && !p.ego) host.onOpen(p);
  });

  // Right-drag pans (the Image Atlas binding), so a right press that MOVED
  // is a pan and must not also open a menu; a right press that stayed put is
  // an ordinary right-click and belongs to Vira's own menu.
  canvas.addEventListener("contextmenu", (e) => {
    if (Math.hypot(e.clientX - rightXY[0], e.clientY - rightXY[1]) > 5) {
      e.preventDefault();
      return;
    }
    const [x, y] = xy(e);
    const p = pickAt(x, y);
    if (!p || p.ego) return;            // fall through to the Vira menu
    e.preventDefault();
    host.onContext(p, e);
  });

  // shift-drag pans, exactly as the Image Atlas rebinds it
  const shiftDown = (e) => {
    if (e.key === "Shift" && controls)
      controls.mouseButtons.left = CameraControls.ACTION.TRUCK;
  };
  const shiftUp = (e) => {
    if (e.key === "Shift" && controls)
      controls.mouseButtons.left = CameraControls.ACTION.ROTATE;
  };
  addEventListener("keydown", shiftDown);
  addEventListener("keyup", shiftUp);

  const ro = new ResizeObserver(() => resize());
  ro.observe(stage);

  // ---------- public -----------------------------------------------------

  function setGraph() {
    clearScene();
    seed();
    if (S.fixedLayout) {
      S.alpha = 0;
    } else if (host.reducedMotion) {
      S.alpha = 1;
      for (let i = 0; i < 420; i++) tick(1 / 60);
      S.alpha = 0;
    } else {
      S.alpha = 1;
    }
    measure();
    resize();
    buildNodes();
    buildEdges();
    buildStars();
    buildCamera();
    refreshPhysics();
    paint();
  }

  function geometryChanged(resetPositions = false) {
    measure();
    if (camera) {
      camera.near = bounds.radius * NAV.nearFactor;
      camera.far = bounds.radius * NAV.farFactor;
      camera.updateProjectionMatrix();
      controls.minDistance = bounds.radius * NAV.minDistanceFactor;
      controls.maxDistance = bounds.radius * NAV.maxDistanceFactor;
    }
    refreshPhysics();
    paint();
  }

  function linkGeometryChanged() {
    if (edgeMesh) scene.remove(edgeMesh);
    edgeGeo?.dispose();
    edgeMesh = edgeGeo = null;
    buildEdges();
    paintEdges();
    requestRender();
  }

  function focusOn(p) {
    if (!controls) return;
    const d = Math.max(bounds.radius * 0.12, p.r * 26);
    const dir = new THREE.Vector3();
    camera.getWorldDirection(dir);
    controls.setLookAt(p.x - dir.x * d, p.y - dir.y * d, p.z - dir.z * d,
                       p.x, p.y, p.z, !host.reducedMotion);
    requestRender();
  }

  function dispose() {
    setRunning(false);
    ro.disconnect();
    removeEventListener("keydown", shiftDown);
    removeEventListener("keyup", shiftUp);
    clearScene();
    renderer.dispose();
    canvas.remove();
    labelHost.remove();
  }

  // What the camera is doing right now. The Image Atlas keeps a console
  // handle for the same reason: a camera you cannot interrogate is a camera
  // whose behaviour can only be argued about.
  function state() {
    if (!controls) return null;
    const t = controls.getTarget(new THREE.Vector3());
    return {
      position: camera.position.toArray(),
      target: t.toArray(),
      distance: controls.distance,
      azimuth: controls.azimuthAngle,
      polar: controls.polarAngle,
      minDistance: controls.minDistance,
      maxDistance: controls.maxDistance,
      smoothTime: controls.smoothTime,
      draggingSmoothTime: controls.draggingSmoothTime,
      dollyToCursor: controls.dollyToCursor,
      buttons: { left: controls.mouseButtons.left,
                 middle: controls.mouseButtons.middle,
                 right: controls.mouseButtons.right,
                 wheel: controls.mouseButtons.wheel },
      physics: { enabled: S.physics.enabled, mode: physicsMode,
                 nodes: physicsNodes.length, alpha: S.alpha },
      nodes: nodeMeshes.size, edges: edgeList.length, radius: bounds.radius,
    };
  }

  // Every card laid out on the last pass, with the node it belongs to, so
  // "is that name attached to that circle?" is a measurement rather than an
  // argument about a screenshot - the same reason state() exists.
  function cards() {
    return lastCards.map((c) => ({
      name: c.name, sub: c.sub, r: +c.r.toFixed(1),
      node: [+c.s.x.toFixed(1), +c.s.y.toFixed(1)],
      card: [+c.cx.toFixed(1), +c.y0.toFixed(1)],
      gap: +(c.y0 - (c.s.y + c.r)).toFixed(1),
      dx: +(c.cx - c.s.x).toFixed(1),
    }));
  }

  resize();
  return { setGraph, paint, wake, resize, focusOn, setRunning, faceLoaded,
           geometryChanged, linkGeometryChanged, refreshPhysics,
           dispose, canvas, state, cards, NAV, ACTION: CameraControls.ACTION };
}
