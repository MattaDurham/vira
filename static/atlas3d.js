/* Visual Network — the 3D renderer.

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
  let stars = null;

  const requestRender = () => { needsRender = true; };

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

  // The flat build's force model, extended to three axes. Constants are the
  // same; only the geometry gained a dimension.
  function tick(dt) {
    const nodes = S.nodes;
    const repel = 1300;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
        let d2 = dx * dx + dy * dy + dz * dz;
        if (d2 > 240 * 240) continue;
        if (d2 < 1) { dx = (i % 2 ? 1 : -1); dy = 0.5; dz = 0.3; d2 = 1.34; }
        const f = repel / d2;
        const d = Math.sqrt(d2);
        const fx = (dx / d) * f, fy = (dy / d) * f, fz = (dz / d) * f;
        a.vx += fx; a.vy += fy; a.vz += fz;
        b.vx -= fx; b.vy -= fy; b.vz -= fz;
      }
    }
    for (const e of S.edges) {
      const w = Math.min(1.5, e.weight) / 1.5;
      const rest = 300 - 190 * w;
      const k = (e.structural ? 0.045 : 0.012) * (0.4 + 0.6 * w);
      spring(e.an, e.bn, rest, k);
    }
    if (!S.hideEgo) {
      for (const e of S.egoEdges) {
        const w = Math.min(1, e.weight);
        spring(e.an, e.bn, e.bn.homeR * (1.15 - 0.35 * w), 0.012);
      }
    }
    for (const p of nodes) {
      const d = Math.hypot(p.x, p.y, p.z) || 1;
      const pull = (p.homeR - d) * 0.008;
      p.vx += (p.x / d) * pull;
      p.vy += (p.y / d) * pull;
      p.vz += (p.z / d) * pull;
      if (p.pin) { p.vx = p.vy = p.vz = 0; continue; }
      p.vx *= 0.86; p.vy *= 0.86; p.vz *= 0.86;
      const sp = Math.hypot(p.vx, p.vy, p.vz);
      const cap = 260 * S.alpha + 20;
      if (sp > cap) { const s = cap / sp; p.vx *= s; p.vy *= s; p.vz *= s; }
      p.x += p.vx * dt * S.alpha * 3.2;
      p.y += p.vy * dt * S.alpha * 3.2;
      p.z += p.vz * dt * S.alpha * 3.2;
    }
    S.alpha = Math.max(0, S.alpha - dt * 0.14);
  }

  function measure() {
    let r = 1;
    for (const p of S.nodes) r = Math.max(r, Math.hypot(p.x, p.y, p.z) + p.r);
    bounds = { center: [0, 0, 0], radius: r };
    return bounds;
  }

  // ---------- scene build ----------------------------------------------

  function clearScene() {
    for (const m of nodeMeshes.values()) {
      scene.remove(m);
      m.material.dispose();
    }
    for (const t of textures.values()) t.dispose();
    nodeMeshes.clear();
    textures.clear();
    for (const l of labels.values()) l.remove();
    labels.clear();
    if (edgeMesh) { scene.remove(edgeMesh); edgeGeo.dispose(); edgeMesh = null; }
  }

  function buildNodes() {
    const all = S.ego ? [...S.nodes, S.ego] : S.nodes;
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
    for (const e of S.egoEdges) edgeList.push({ e, ego: true });
    for (const e of S.edges) edgeList.push({ e, ego: false });
    const n = edgeList.length;
    edgeGeo = new THREE.BufferGeometry();
    edgeGeo.setAttribute("position",
      new THREE.BufferAttribute(new Float32Array(n * 12), 3));
    edgeGeo.setAttribute("aOther",
      new THREE.BufferAttribute(new Float32Array(n * 12), 3));
    edgeGeo.setAttribute("aSide",
      new THREE.BufferAttribute(new Float32Array(n * 4), 1));
    edgeGeo.setAttribute("aWidth",
      new THREE.BufferAttribute(new Float32Array(n * 4), 1));
    edgeGeo.setAttribute("aColor",
      new THREE.BufferAttribute(new Float32Array(n * 12), 3));
    edgeGeo.setAttribute("aAlpha",
      new THREE.BufferAttribute(new Float32Array(n * 4), 1));
    const idx = new Uint32Array(n * 6);
    const side = edgeGeo.getAttribute("aSide").array;
    for (let i = 0; i < n; i++) {
      const v = i * 4;
      side[v] = -1; side[v + 1] = 1; side[v + 2] = -1; side[v + 3] = 1;
      idx.set([v, v + 1, v + 3, v, v + 3, v + 2], i * 6);
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

  // Every press re-anchors the orbit under the cursor: the node you pressed,
  // else the front-most node near it, else the plane through the graph's
  // centre - so a click-hold drag always rotates around where you grabbed,
  // never the screen centre.
  function anchorOrbit(x, y) {
    const p = pickAt(x, y, 150);
    if (p) { controls.setOrbitPoint(p.x, p.y, p.z); return; }
    const rc = new THREE.Raycaster();
    rc.setFromCamera(new THREE.Vector2((x / W) * 2 - 1, -(y / H) * 2 + 1), camera);
    const c = bounds.center;
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(
      camera.getWorldDirection(new THREE.Vector3()),
      new THREE.Vector3(c[0], c[1], c[2]));
    const hit = new THREE.Vector3();
    if (rc.ray.intersectPlane(plane, hit))
      controls.setOrbitPoint(hit.x, hit.y, hit.z);
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
    return alpha;
  }

  function paintNodes() {
    for (const mesh of nodeMeshes.values()) {
      const p = mesh.userData.p;
      const show = visible(p);
      mesh.visible = show;
      if (!show) continue;
      mesh.position.set(p.x, p.y, p.z);
      const u = mesh.material.uniforms;
      u.uAlpha.value = nodeAlpha(p);
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
      const v = i * 4;
      const hide = ego ? (S.hideEgo || !!S.shown)
        : (S.shown && !(S.shown.has(e.an.id) && S.shown.has(e.bn.id)));
      let r = 0, g = 0, b = 0, a = 0, lw = 1;
      if (!hide) {
        const st = edgeStyle(e, ego, hasSel, focus);
        r = st[0] / 255; g = st[1] / 255; b = st[2] / 255; a = st[3]; lw = st[4];
      }
      const A = e.an, B = e.bn;
      for (let k = 0; k < 4; k++) {
        const at = k < 2 ? A : B, ot = k < 2 ? B : A;
        pos.setXYZ(v + k, at.x, at.y, at.z);
        oth.setXYZ(v + k, ot.x, ot.y, ot.z);
        col.setXYZ(v + k, r, g, b);
        alp.setX(v + k, a);
        wid.setX(v + k, lw);
      }
    }
    pos.needsUpdate = oth.needsUpdate = wid.needsUpdate = true;
    col.needsUpdate = alp.needsUpdate = true;
  }

  // Labels are DOM, not textures: they inherit the module's own type and
  // whatever skin is on, and only a handful are ever up at once.
  function paintLabels() {
    const wanted = [];
    const all = S.ego ? [...S.nodes, S.ego] : S.nodes;
    for (const p of all) {
      if (!visible(p)) continue;
      const a = nodeAlpha(p);
      if (a <= 0.25) continue;
      const r = screenR(p);
      const want = p.ego || p === S.hover || S.sel.has(p)
        || S.selPathNodes.has(p.id) || S.shared.has(p.id)
        || (S.sel.size === 1 && S.neighbors.has(p.id))
        || r > 15                       // the 3D reading of "zoomed in enough"
        || (S.match && !host.matchDim(p));
      if (!want) continue;
      const s = screenOf(p);
      if (!s || s.x < -80 || s.x > W + 80 || s.y < -40 || s.y > H + 40) continue;
      wanted.push({ p, s, a, r });
    }
    wanted.sort((x, y) => x.s.z - y.s.z);
    const keep = wanted.slice(0, LABEL_MAX);
    const live = new Set();
    for (const { p, s, a, r } of keep) {
      live.add(p.id);
      let node = labels.get(p.id);
      if (!node) {
        node = document.createElement("span");
        node.className = "atlas-label";
        labelHost.appendChild(node);
        labels.set(p.id, node);
      }
      const txt = p.ego ? p.name : host.firstLast(p.name);
      if (node.textContent !== txt) node.textContent = txt;
      node.classList.toggle("ego", !!p.ego);
      node.classList.toggle("sel", S.sel.has(p));
      node.style.transform =
        "translate(-50%,0) translate(" + s.x + "px," + (s.y + r + 4) + "px)";
      node.style.opacity = String(Math.min(1, a));
    }
    for (const [id, node] of labels) {
      if (!live.has(id)) { node.remove(); labels.delete(id); }
    }
  }

  function paint() {
    if (!camera) return;
    paintNodes();
    paintEdges();
    requestRender();
  }

  // ---------- loop -------------------------------------------------------

  const _cq = new THREE.Quaternion();

  function frame() {
    if (!running) return;
    raf = requestAnimationFrame(frame);
    const dt = Math.min(0.05, clock.getDelta());
    idleT += dt;
    if (S.alpha > 0.005) { tick(dt); paintNodes(); paintEdges(); needsRender = true; }
    // slow drift - the Image Atlas's idle auto-orbit, paused while a
    // selection owns the stage (its analogue of a focused photo)
    if (!host.reducedMotion && idleT > NAV.driftAfter && !S.sel.size
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

  // capture phase: the orbit re-anchor must run BEFORE camera-controls'
  // own pointerdown snapshots its drag state, or the anchor is ignored
  // mid-gesture
  canvas.addEventListener("pointerdown", (e) => {
    const [x, y] = xy(e);
    downXY = [x, y];
    if (e.button === 2) rightXY = [e.clientX, e.clientY];
    anchorOrbit(x, y);          // every press, as the Image Atlas does
  }, true);

  canvas.addEventListener("pointermove", (e) => {
    const [x, y] = xy(e);
    const p = pickAt(x, y, 0);
    if (p !== S.hover) {
      S.hover = p;
      canvas.style.cursor = p ? "pointer" : "grab";
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
    if (host.reducedMotion) {
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
    paint();
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
      nodes: nodeMeshes.size, edges: edgeList.length, radius: bounds.radius,
    };
  }

  resize();
  return { setGraph, paint, wake, resize, focusOn, setRunning,
           faceLoaded, dispose, canvas, state, NAV, ACTION: CameraControls.ACTION };
}
