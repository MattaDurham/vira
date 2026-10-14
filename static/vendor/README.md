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
