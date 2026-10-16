# Three.js vendor provenance

The Three.js family here is pinned to **0.179.0 (r179)** from the official
[`three` npm package](https://www.npmjs.com/package/three/v/0.179.0).
The aquarium benchmark adds these files:

| Vendored file | Package source |
|---|---|
| `three.webgpu.js` | `build/three.webgpu.js` |
| `three.tsl.js` | `build/three.tsl.js` |
| `GLTFLoader.js` | `examples/jsm/loaders/GLTFLoader.js` |
| `BufferGeometryUtils.js` | `examples/jsm/utils/BufferGeometryUtils.js` |
| `RectAreaLightTexturesLib.js` | `examples/jsm/lights/RectAreaLightTexturesLib.js` |

Only module specifiers are rewritten: `three` and `three/webgpu` resolve to the
local WebGPU build, and the loader's utility import resolves locally.
`three.core.js` is byte-identical to this release's `build/three.core.js`.
The existing core and WebGL module build are unchanged by the benchmark.

Three.js and these utilities are MIT licensed; their license headers are
retained. The full upstream license is included in `three.LICENSE.txt`.

RectAreaLightTexturesLib lookup literals use equivalent scientific notation
and trailing whitespace is normalized. Both Float32 lookup tables retain
the exact upstream values; the notation avoids false matches on decimal
lookup coefficients in source hygiene checks.


## Rapier articulated mechanics

`rapier.mjs` is byte-identical to `dist/rapier.mjs` in the official
[`@dimforge/rapier3d-compat` 0.21.0 npm package](https://www.npmjs.com/package/@dimforge/rapier3d-compat/v/0.21.0).
It includes the WebAssembly runtime; no CDN or package install is needed to
open the mechanics study. The ordinary aquarium does not import this engine.

- File SHA-256: `02dc6a4e2fffc013bab08fbb44fa68f501d8303615e9afe4c7d89ad4eedda0d0`
- npm archive integrity: `sha512-tCl1HPGwOhn5aCQbgqWbOH+Nx/5fnr3IPAnzQuR7zIhC/raqi2yxC6LhvZqjdI2El5hQgY5RDAwDrAjAi2FdPA==`
- Upstream: [Dimforge Rapier](https://github.com/dimforge/rapier)
- License: Apache-2.0; the complete license is in `rapier.LICENSE.txt`.
  The npm package provides no separate NOTICE file.

The upstream source-map comment is retained. The optional development map is
not distributed. Rapier supplies rigid bodies and joint constraints; the fish's
mass allocation, muscle commands and rendering bridge are separately authored.
