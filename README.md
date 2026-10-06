# ISDA SIMM calculator (Python, USD)

A Python implementation of the ISDA Standard Initial Margin Model (ISDA SIMM®) for uncleared OTC derivatives, covering methodology
versions **2.5 to 2.8+2512**. It reads a CRIF file, validates it strictly, picks the SIMM version from the valuation date and reports
initial margin in USD by counterparty, netting set, product class, risk class and component (delta, vega, curvature, base correlation).

- **Offline.** No network access and no FX conversion: amounts are the CRIF `AmountUSD` values.
- **Decimal arithmetic.** Every result keeps a calculation trace back to the CRIF rows.
- **Netting sets stay separate.** Margins are summed per netting set and never offset across netting sets.

> **ISDA notice.**
> - ISDA SIMM® is a registered trademark and intellectual property of the International Swaps and Derivatives Association, Inc.
>   (ISDA), and is subject to U.S. Patent No. 10,515,410.
> - The files in `parameters/` are transcriptions of ISDA's public methodology documents (links in each `manifest.json`). They are
>   **not** covered by this project's licence, and the ISDA documents themselves are not redistributed here.
> - Using ISDA SIMM for margin calculations, including commercially, may require a licence from ISDA. See the
>   [ISDA SIMM licensing FAQ](https://www.isda.org/2021/04/08/isda-simm-licensing-faq/) and contact isdalegal@isda.org.
> - This project is not affiliated with or endorsed by ISDA and comes with no warranty. Validate results independently before relying on them.

## Quick start

Requires Python 3.11+. Reading `.xlsx` CRIF files additionally needs `openpyxl`.

```bash
pip install -e .            # or: export PYTHONPATH=src
python -m simm versions     # version schedule
python -m simm run --crif examples/crif_example.csv --valuation-date 2026-06-30 --context examples/context.json
```

Output of the example:

```
SIMM 2.8+2506 | valuation 2026-06-30 | USD | SHADOW
Portfolio total: 33,496,812.42 USD

[CP_A] total 14,300,634.89 USD
  netting set NS_A1 / CSA CSA_1: 14,300,634.89 (add-on 50,000.00)
    RatesFX: 7,290,168.27
      InterestRate: 1,614,228.80  (Delta 1,559,056.99, Vega 21,989.09, Curvature 33,182.72)
      FX: 5,697,557.53  (Delta 5,545,277.27, Vega 94,228.44, Curvature 58,051.83)
    ...
```

Useful options:

| Option | Purpose |
| --- | --- |
| `--output result.json` | Write metadata and result JSON. Add `--include-trace` for the full trace. |
| `--summary-csv summary.csv` | Write the breakdown table. |
| `--version 2.7` | Override automatic version selection. The override is recorded and a mismatch is warned. |
| `--netting-set-column PortfolioId` | Use another column as `NettingSet`. |
| `--excel-serial-dates` | Convert numeric Excel `ValuationDate` cells. |

Exit codes: 0 success, 1 error, 2 validation blocked (the issues are listed).

## Preparing your own run

1. **CRIF.** Fill [`crif/crif_template.csv`](crif/crif_template.csv) following [`crif/CRIF_FORMAT.md`](crif/CRIF_FORMAT.md).
2. **Context.** Copy [`config/context.template.json`](config/context.template.json) and fill direction, regulation, scope defaults and,
   for every risk type, the evidence for the units your CRIF uses. Empty evidence keeps the run blocked by design.
3. **Run.** Run `python -m simm run ...` as above.

## Versions

| Version | Used from COB |
| --- | --- |
| 2.5 | 2022-12-02 |
| 2.5A | 2023-07-14 |
| 2.6 | 2023-12-01 |
| 2.7 | 2024-12-06 |
| 2.7+2412 | 2025-07-11 |
| 2.8+2506 | 2025-12-05 |
| 2.8+2512 | 2026-07-10 |

Dates after the schedule's `verified_through` date are flagged until the schedule is re-checked against new ISDA releases. Adding a
version means adding a `parameters/simm_<version>/` package and a schedule entry.

## Validation status

The validation evidence and its limits are summarised in [`docs/METHODOLOGY_NOTES.md`](docs/METHODOLOGY_NOTES.md).
- **Parameters:** cross-checked by independent transcriptions.
- **Formulas:** cross-checked against an independent re-implementation on synthetic portfolios.
- **Not verified:** ISDA's official unit tests were not run, and vega, curvature, credit, equity and commodity have synthetic checks only.

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -t .
```

## Licence

The code, documentation, CRIF template and examples are licensed under the [Apache License 2.0](LICENSE). You may use, modify and
redistribute them, including commercially, without asking permission, provided you keep the licence and the [NOTICE](NOTICE) file and
mark files you change.

The ISDA SIMM parameter values in `parameters/` are excluded from that licence (see [NOTICE](NOTICE) and
[parameters/README.md](parameters/README.md)).

## Layout

```
src/simm/        engine, CRIF reader/validator, parameter loader, version schedule, CLI
parameters/      one package per SIMM version + version_schedule.json
crif/            CRIF template and format description
examples/        synthetic CRIF and context
config/          context template
docs/            methodology notes
tests/           synthetic tests
```

## 한국어 요약

ISDA SIMM(비청산 장외파생상품 개시증거금) Python 계산기입니다.
- **지원 범위:** 방법론 2.5~2.8+2512 버전입니다.
- **계산 방식:** CRIF 파일을 엄격히 검증한 뒤 평가일에 맞는 버전을 자동 선택해 USD로 계산합니다.
- **산출 결과:** 거래상대방·Netting Set·상품·위험군·구성요소별 증거금을 냅니다.
- **오프라인:** 외부 접속과 환율 환산이 없으며, 금액은 CRIF의 `AmountUSD`를 그대로 씁니다.
- **CRIF 작성법:** `crif/CRIF_FORMAT.md`를 보시고, 예시는 `examples/`에 있습니다.
- **이 프로젝트의 라이선스:** 코드·문서·양식은 Apache-2.0입니다. 허락 없이 사용·수정·재배포·상업적 이용이 가능하고, LICENSE와 NOTICE를 유지하고 수정한 파일에 수정 사실을 표시하면 됩니다.
- **ISDA 권리:** ISDA SIMM은 ISDA의 상표·지적재산이며 미국 특허(10,515,410) 대상입니다. `parameters/`의 Parameter 값은 이 라이선스 적용 대상이 아니고, SIMM 사용에는 ISDA 라이선스가 필요할 수 있습니다. 결과는 별도로 검증한 뒤 사용하십시오.
