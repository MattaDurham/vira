# Reproduce the articulated water study

This authoring tool regenerates three mechanical comparison cases from checked-in
inputs and the existing koi asset. It preserves the skeleton and records actual
articulated poses. Reproducing a run establishes what this implementation does;
it does not establish biological accuracy or photorealism.

Run from the repository root with Node.js:

```sh
node scripts/aquarium/water-study/generate.mjs --case=all --rate=960
```

| Argument | Values | Default |
|---|---|---|
| `--case` | `hinge`, `bounded`, `source13cm`, `all` | `all` |
| `--rate` | `240`, `480`, `960` steps per second | `960` |
| `--out` | Output directory | `.aquarium-build/water-study-reproduced` |

For example, regenerate one case at a different integration rate:

```sh
node scripts/aquarium/water-study/generate.mjs --case=source13cm --rate=480 --out=.aquarium-build/source13cm-480
```

Each case writes `<case>-<rate>-timeline.json`, containing `{ "runs": [run] }`,
and `<case>-<rate>-provenance.json`, its hash sidecar. The default output directory
is git-ignored. Keep generated output separate from the checked-in inputs.

Pack a regenerated 960 Hz comparison for the public viewer with:

```sh
node scripts/aquarium/water-study/pack.mjs --input-dir=.aquarium-build/water-study-reproduced
```

The packer verifies the generated timelines against their provenance sidecars.
Its separate `--archive-dir` mode is an internal extraction path for the exact
hash-pinned original research recordings; those ignored recordings are not
required by the public regeneration workflow.

## Cases and limits

| Case | Construction |
|---|---|
| `hinge` | Sequential material-patch water kick with the original SDK motors resolved in the subsequent Rapier step. A control comparison, rather than the shared muscle/water solve. |
| `bounded` | Shared active-muscle and backward-Euler water solve, with SDK motors off, joint-coordinate limits of +/-0.16 rad, and the authored 0.50 lateral section-capacity scenario. The target is a finite 2.4 rad regional C command; this is a command coefficient, not a measured joint angle. |
| `source13cm` | The bounded construction driven by the fitted fast bluegill trial `BG63_14_8`, retargeted onto a uniformly scaled 13 cm koi. It starts with the source-derived SI velocity, retains source seconds, and uses an authored active-capacity scaling by the cube of geometric scale. |

The source-sized case remains a koi-shaped mechanical proxy. Uniform scaling
and a bluegill shape cue do not provide a bluegill species model or Reynolds/
dynamic similarity calibration. Measured source head orientation and COM are
reference data; they are not prescribed root motion. The selected initial
velocity is an initial condition, not a continuing trajectory constraint.

Water forces approximate instantaneous, anisotropic quadratic resistance.
These cases do not resolve added water mass, lift, wakes, unsteady flow, or tank
walls. Joint bounds and motor capacities are authored modeling choices, not
measured cartilage or a validated muscle force-length/velocity law. Compare
integration rates and the recorded error/work ledgers before interpreting a
trajectory; an energy identity alone does not validate forces or swimming.

## Inputs and scientific provenance

[`inputs.json`](inputs.json) is a compact extract of the selected geometry,
capacity and observed-shape inputs, with exact source hashes. Keep it with the
hash sidecar when sharing a regenerated run. The implementation imports local
helpers and existing vendored libraries; it has no runtime dependency on ignored
research scripts.

The observed-shape input derives from Andrew David Clark and Eric D. Tytell,
[*Tradeoffs for bluegill sunfish turning at different rates*](https://doi.org/10.7717/peerj.21161),
using their [v1.0 data/code archive](https://doi.org/10.5281/zenodo.19102452).
The `v1.0` tag resolves to commit
`47274ebe9d171a4806985c090ef4361878afd842`. The exact
[`ProcessedData/fulldata.csv`](https://raw.githubusercontent.com/andrewdclark/BluegillTurning/47274ebe9d171a4806985c090ef4361878afd842/ProcessedData/fulldata.csv)
SHA-256 is:

```text
8ebe542fca4b67abf5fd31d3aab671eda14f50265ce79c9938db567272e8b3f5
```

Our modifications select the fast trial and filtered tracked landmarks, fit
regional curvature commands, retarget them to the koi skeleton, interpolate
with quintic Hermite curves and an authored C2 boundary window, and scale the
koi proxy to 13 cm. The source coordinates already use the authors' ninth-order
Butterworth filtering: 4 Hz for slow trials and 8 Hz for fast trials, applied
bidirectionally to 60 Hz recordings. That noncausal filtering limits temporal
and high-frequency interpretation; the interpolant supplies continuity, not
additional observations.

The lateral capacity scenario selects a 229,720 Pa maximal isometric tetanic
stress reference from Wakeling and Johnston (1999),
[*Predicting muscle force generation during fast-starts for the common carp
Cyprinus carpio*](https://doi.org/10.1007/s003600050235). A neutral skin-section
first area moment converts this reference stress to a conditional torque
bound. Multiplication by 0.50 is our authored section-occupancy scenario; it is
not measured koi muscle anatomy or activation. The selected reference does not
calibrate the large authored animal or the scaled proxy.

See [`NOTICE.md`](NOTICE.md) for attribution, licenses and modification notices.
No third-party movies, PDFs or complete source CSV are included.
