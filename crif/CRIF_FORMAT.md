# CRIF input format

The calculator reads one CRIF (Common Risk Interchange Format) file per run, as UTF-8 CSV/TSV or a single-sheet XLSX.
Start from [`crif_template.csv`](crif_template.csv). [`../examples/crif_example.csv`](../examples/crif_example.csv) covers every risk type.

Conventions follow the public ISDA Risk Data Standards 1.36 where they exist. Where that version is silent or outdated, the methodology
document governs and an explicit extension column is used (marked *extension* below). Check your producer's encoding against the
current licensed Risk Data Standards before relying on results.

## Columns

| Column | Required | Description |
| --- | --- | --- |
| `ValuationDate` | recommended | `YYYY-MM-DD`. Must equal `--valuation-date`. Excel date serials need `--excel-serial-dates`. |
| `Counterparty` | yes* | Counterparty identifier. |
| `NettingSet` | yes* | Netting set (CSA portfolio). Margins of different netting sets are **summed, never offset**. Use `--netting-set-column <col>` to take it from another column such as `PortfolioId`. |
| `LegalEntity`, `CSA` | yes* | Your legal entity and CSA identifier. |
| `TradeID` | optional | Keeps rows of different trades distinct. Exact duplicate rows are flagged for review. |
| `IMModel` | optional | `SIMM`. Any other value (for example `Schedule`) blocks the run. |
| `SIMMVersion` | optional | If present, it must equal the selected version. |
| `ProductClass` | yes | `RatesFX`, `Credit`, `Equity` or `Commodity`. Blank only for add-on parameter rows. |
| `RiskType` | yes | See the next table. |
| `Qualifier` | yes | See the next table. |
| `Bucket` | yes (may be blank) | See the next table. |
| `Label1`, `Label2` | yes (may be blank) | See the next table. |
| `Amount` | yes | Sensitivity in `AmountCurrency`. |
| `AmountCurrency` | yes | ISO 4217 code from the context `currency_codes`. |
| `AmountUSD` | yes | Sensitivity in USD. **This is the amount used.** For USD rows it must equal `Amount` exactly. The calculator performs no FX conversion. |
| `PaymentCurrency` | credit only (*extension*) | Payment currency of credit delta/vega rows. Different payment currencies of the same issuer are distinct risk factors. |
| `GroupName` | non-qualifying credit (*extension*) | Group name (for example `CMBX`, `ABX`) for the same/different group correlation. |
| `CollectRegulations`, `PostRegulations` | optional | If present, rows whose list excludes the context regulation are excluded and reported. |
| `Unit`, `SensitivityUnit` | optional | If present, they must equal the declared unit. |

\* A scope column may be omitted if the context file gives a default (`counterparty`, `legal_entity`, `netting_set`, `csa`).

## Risk types

Tenors (`Label1`) are lower case: `2w 1m 3m 6m 1y 2y 3y 5y 10y 15y 20y 30y`. Credit tenors are `1y 2y 3y 5y 10y`.
Non-standard tenors are rejected rather than re-gridded.

| RiskType | Qualifier | Bucket | Label1 | Label2 | Amount unit |
| --- | --- | --- | --- | --- | --- |
| `Risk_IRCurve` | currency | `1` regular vol, `2` low vol (JPY), `3` high vol | tenor | sub-curve: `OIS`, `Libor1m`, `Libor3m`, `Libor6m`, `Libor12m`, plus `Prime`, `Municipal` for USD | PV01 per 1bp |
| `Risk_Inflation` | currency | blank | blank | blank | PV01 per 1bp |
| `Risk_XCcyBasis` | currency | blank | blank | blank | PV01 per 1bp |
| `Risk_IRVol` | currency | blank | option expiry | blank | vol-weighted vega (σ × ∂V/∂σ) |
| `Risk_InflationVol` | currency | blank | option expiry | blank | vol-weighted vega (σ × ∂V/∂σ) |
| `Risk_CreditQ` | issuer/seniority as `ISIN:XX#########N`, otherwise flagged for review | `1`-`12`, `Residual` | credit tenor | blank, or `Sec` | CS01 per 1bp |
| `Risk_CreditVol` | issuer/seniority | `1`-`12`, `Residual` | option expiry (credit tenors) | blank | vol-weighted vega (σ × ∂V/∂σ) |
| `Risk_BaseCorr` | index family (for example `CDX IG`) | blank | blank | blank | BC01 per 1 correlation point |
| `Risk_CreditNonQ` | tranche | `1`, `2`, `Residual` | credit tenor | blank | CS01 per 1bp |
| `Risk_CreditVolNonQ` | tranche | `1`, `2`, `Residual` | option expiry (credit tenors) | blank | vol-weighted vega (σ × ∂V/∂σ) |
| `Risk_Equity` | issuer as ISIN (buckets 1-10, `Residual`); any name for buckets 11-12 | `1`-`12`, `Residual` | blank | blank | delta per 1% relative move |
| `Risk_EquityVol` | as `Risk_Equity` | `1`-`12`, `Residual` | option expiry | blank | vega per 1 vol point (lognormal); σ applied by the model |
| `Risk_Commodity` | commodity | `1`-`17` | blank | blank | delta per 1% relative move |
| `Risk_CommodityVol` | commodity | `1`-`17` | option expiry | blank | vega per 1 vol point (lognormal); σ applied by the model |
| `Risk_FX` | currency (USD rows are allowed and excluded: USD is the calculation currency) | blank | blank | blank | delta per 1% relative move |
| `Risk_FXVol` | currency pair, 6 letters (`EURUSD` = `USDEUR`) | blank | option expiry | blank | vega per 1 vol point (lognormal); σ applied by the model |

## Add-on and multiplier rows (methodology section L)

Labels and Bucket must be blank. The values apply to the row's netting set.

| RiskType | ProductClass | Qualifier | AmountUSD |
| --- | --- | --- | --- |
| `Param_ProductClassMultiplier` | same as Qualifier | product class | multiplier ≥ 1 |
| `Param_AddOnNotionalFactor` | blank | product name | add-on factor in percent (5 = 5%) |
| `Notional` | blank | product name | trade notional (absolute values are summed) |

A fixed add-on amount is supplied through the engine configuration (`fixed_addons`), not as a CRIF row.

## Context file

[`../config/context.template.json`](../config/context.template.json) holds the run context.

| Field | Meaning |
| --- | --- |
| `direction` | `collect` or `post`. |
| `regulation` | Regulation label; required. It filters rows when `CollectRegulations`/`PostRegulations` are present. |
| `crif_profile` | Must be `public_rds_1_36_methodology_2_8`. |
| `currency_codes` | ISO currencies accepted in `AmountCurrency`, IR/FX qualifiers and FX pairs. |
| `counterparty`, `legal_entity`, `netting_set`, `csa` | Defaults for missing scope columns. |
| `unit_declarations` | For each risk type, the unit your CRIF uses and the evidence for it. The units must equal the package `input_units`. **An empty `source` blocks the run by design.** |

## Validation outcome

Issues are `CRITICAL`, `REVIEW_REQUIRED` or `WARNING`. Any critical or review issue blocks the calculation, and the CLI exits with code 2 and lists the issues.
Nothing is repaired silently: no trimming, no tenor re-gridding, no currency inference and no FX conversion.
