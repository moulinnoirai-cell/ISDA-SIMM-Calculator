# Methodology notes

Implementation choices where the ISDA SIMM methodology text needs interpretation, the validation evidence, and what remains unverified.
Section and paragraph numbers refer to the ISDA SIMM methodology document of the selected version.

## Versions

- **Supported versions:** 2.5, 2.5A, 2.6, 2.7, 2.7+2412, 2.8+2506 and 2.8+2512. The formula sections (B and C) of these documents are
  textually identical (word, formula-symbol and page-image comparison), so one engine serves all of them. Only the parameter packages differ.
- **Version selection:** [`parameters/version_schedule.json`](../parameters/version_schedule.json) selects the version by valuation date,
  using ISDA's "use from close of business (COB)" date for each release.
- **Outside the schedule:** dates before 2022-12-02 are rejected. Dates after `verified_through` are calculated with the latest version and
  flagged, because a newer ISDA release may exist.

## Interpretation choices

1. **Vega input units (C.30, B.10(b)).**
   - `Risk_EquityVol`, `Risk_CommodityVol` and `Risk_FXVol` amounts are ∂V/∂σ per one lognormal vol point. The model applies
     σ = RW·√(365/14)/Φ⁻¹(99%) and, for vega, HVR.
   - `Risk_IRVol`, `Risk_InflationVol` and the credit vol types are already vol-weighted (σ·∂V/∂σ), because σ is the market vol known only to the producer.
2. **Vega concentration (B.10(d)).** VCR uses the sum of VR_ik including HVR and σ.
3. **Interest-rate vega concentration.** Inflation vol is included in the currency bucket sum.
4. **Credit non-qualifying intra-bucket correlation.** It depends on `GroupName` only.
5. **Credit qualifying risk factors.** A risk factor is issuer/seniority × tenor × payment currency. The concentration sum runs over all
   tenors and payment currencies of the issuer.
6. **FX vol pairs.** Pair order is ignored (`EURUSD` = `USDEUR`).
7. **Curvature (B.11).**
   - Scaling function SF(t) = 0.5·min(1, 14/t), with t in calendar days (1m = 365/12 days).
   - θ, λ = (Φ⁻¹(99.5%)² − 1)(1 + θ) − θ; non-residual and residual parts are added.
   - Interest-rate curvature is scaled by HVR_IR⁻².
   - Equity bucket 12 curvature is zero.
8. **Calculation currency.** USD. FX delta rows for USD are excluded. RW and correlation use the "regular/high volatility calculation
   currency" tables for USD. Amounts are `AmountUSD`; no FX conversion is performed.
9. **Aggregation.** Product-class SIMM values are summed within a netting set, add-ons and multipliers are applied per netting set
   (section L), and netting sets are summed per counterparty. Nothing offsets across netting sets.
10. **Concentration thresholds.** They are held in absolute USD (the tables give USD millions).

## Validation evidence

- **Parameter packages.**
  - Each package is transcribed from the public ISDA PDF linked in its `manifest.json`.
  - 2.8+2512 was transcribed twice independently: 1,037 of 1,037 values matched.
  - 2.5, 2.6, 2.8+2506 and 2.8+2512 also match an independent open-source transcription value by value (1,024 values each).
- **Formulas.**
  - An independent floating-point re-implementation of sections B-C was compared on 324 synthetic portfolios covering every risk class
    and component. No formula difference was found.
  - Remaining differences were numerical: residues of about 1e-45 where the exact result is 0, and float cancellation of about 1e-10
    relative in one curvature case.

## Not verified

- **Official tests.** ISDA's official SIMM unit tests were not run (licensed material).
- **Real portfolios.** Results have not been compared with official or published IM figures; all checks above use synthetic data and parameter transcriptions.
- **CRIF encodings beyond Risk Data Standards 1.36.** Payment currency, group name and fixed add-ons use explicit extension columns or
  configuration and should be checked against the current standard.
- **Production margin calls.** Not covered: thresholds, minimum transfer amounts, collateral and regulatory scope decisions are outside this calculator.
