# Water-study attribution and licenses

## Observed bluegill data

Adapted data are credited to **Andrew David Clark and Eric D. Tytell**, authors
of [*Tradeoffs for bluegill sunfish turning at different rates*](https://doi.org/10.7717/peerj.21161).

Source archive: [*andrewdclark/BluegillTurning: Data and Code for "Tradeoffs for
bluegill sunfish turning at different rates"*, v1.0](https://doi.org/10.5281/zenodo.19102452).
The archive is licensed under **Creative Commons Attribution 4.0 International**
([license and legal code](https://creativecommons.org/licenses/by/4.0/legalcode)).
Its license is identified in the [Zenodo record](https://zenodo.org/api/records/19102452)
and [DataCite record](https://api.datacite.org/dois/10.5281/zenodo.19102452).
The licensed material is provided as-is, without warranties, under that license.

Immutable source:
[`ProcessedData/fulldata.csv` at commit `47274ebe9d171a4806985c090ef4361878afd842`](https://raw.githubusercontent.com/andrewdclark/BluegillTurning/47274ebe9d171a4806985c090ef4361878afd842/ProcessedData/fulldata.csv),
the commit resolved by the archive's `v1.0` tag.

Source CSV SHA-256:

```text
8ebe542fca4b67abf5fd31d3aab671eda14f50265ce79c9938db567272e8b3f5
```

**Our modifications:** selection of fast trial `BG63_14_8` and filtered tracked
coordinates; regional shape fitting and retargeting to the koi skeleton;
quintic interpolation with a C2 boundary window; a uniformly scaled 13 cm koi
proxy with source-derived initial SI velocity. These are adaptations by this
project, not measurements supplied by the source authors. The retained source
filtering is ninth-order, bidirectional Butterworth at 4 Hz for slow trials and
8 Hz for fast trials, from 60 Hz recordings; it is noncausal.

Retain this attribution, source links, license link and modification notice
when sharing adapted observed inputs or outputs incorporating them. No source
author endorsement is implied. No third-party source movies, PDFs or complete
CSV are redistributed here.

## Muscle-stress reference

J. M. Wakeling and I. A. Johnston (1999),
[*Predicting muscle force generation during fast-starts for the common carp
Cyprinus carpio*](https://doi.org/10.1007/s003600050235), supplies the selected
229,720 Pa stress reference. Our skin-section calculation and 0.50 capacity
scenario are authored adaptations of a numerical reference, not measured koi
muscle occupancy or anatomical validation. The paper itself is not included.

## Project code, geometry and dependencies

The original implementation, koi GLB and project-authored mass geometry use
the repository's [MIT license](../../../LICENSE). This does not replace CC BY
4.0 for the adapted observed data.

Vendored **Three.js is MIT licensed** and **Rapier is Apache-2.0 licensed**.
Their retained notices, versions and provenance are in
[`static/vendor/README.md`](../../../static/vendor/README.md),
[`three.LICENSE.txt`](../../../static/vendor/three.LICENSE.txt) and
[`rapier.LICENSE.txt`](../../../static/vendor/rapier.LICENSE.txt).
