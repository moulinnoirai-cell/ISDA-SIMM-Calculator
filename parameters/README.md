# Parameter packages

One folder per ISDA SIMM methodology version (`simm_<version>/`):

- `parameters.json` holds the risk weights, correlations, concentration thresholds, historical volatility ratios, vega risk weights and
  currency groups of that version. Each `sources` entry links to the ISDA public methodology PDF and section.
- `manifest.json` holds the version, the SHA-256 of `parameters.json` (checked at load time) and transcription notes.

[`version_schedule.json`](version_schedule.json) maps valuation dates to versions by ISDA's "use from COB" dates.

**Rights.** These values are transcribed from documents published by ISDA. Rights remain with ISDA. They are **not** licensed under this
project's Apache License (see [`../NOTICE`](../NOTICE)). Use of ISDA SIMM may require a licence from ISDA.

**Adding a version.**
1. Create `simm_<version>/parameters.json` in the same structure, transcribed from the ISDA document.
2. Write its `manifest.json` with the file's SHA-256.
3. Add a schedule entry.
4. Run the tests.

Packages are rejected at load time if the hash does not match, a correlation matrix is asymmetric or out of range, or a threshold is not positive.
