"""Command-line runner: CRIF -> validation -> version selection -> engine -> summary (USD).

    python -m simm run --crif FILE --valuation-date YYYY-MM-DD --context context.json [options]
    python -m simm versions [--date YYYY-MM-DD]

Every input transformation (column mapping, Excel serial dates, version override) is explicit on the command
line and recorded in the output. Amounts are CRIF AmountUSD; no FX conversion and no network access.
"""
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import argparse, csv, hashlib, io, json, sys

from .crif import read_crif, validate
from .engine import Engine
from .models import plain
from .parameters import ParameterPackage
from .versioning import load_schedule, select_version

COMPONENTS = ('Delta', 'Vega', 'Curvature', 'BaseCorr')
SECRET_MARKERS = ('password', 'credential', 'secret', 'api_key', 'cookie', 'token')


def ensure_no_secrets(configuration):
    """Reject credential-like keys anywhere in a context/configuration object."""
    if isinstance(configuration, (list, tuple)):
        for value in configuration:
            ensure_no_secrets(value)
        return
    if not isinstance(configuration, dict):
        return
    for key, value in configuration.items():
        if any(x in key.lower() for x in SECRET_MARKERS):
            raise ValueError('Credentials are prohibited in calculation configuration')
        ensure_no_secrets(value)


def excel_serial_to_iso(value):
    text = str(value).strip()
    try:
        serial = float(text)
    except ValueError:
        return text  # already a date string; the validator checks the format
    return (date(1899, 12, 30) + timedelta(days=serial)).isoformat()


def prepare_rows(headers, rows, args):
    applied = []
    if args.excel_serial_dates:
        if 'ValuationDate' not in headers:
            raise ValueError('--excel-serial-dates given but the CRIF has no ValuationDate column')
        for r in rows:
            r['ValuationDate'] = excel_serial_to_iso(r['ValuationDate'])
        applied.append('ValuationDate: Excel serial numbers converted to ISO dates (--excel-serial-dates)')
    if args.netting_set_column:
        col = args.netting_set_column
        if col not in headers:
            raise ValueError(f'--netting-set-column {col} is not a CRIF column')
        if 'NettingSet' in headers:
            raise ValueError('CRIF already has NettingSet; refusing to overwrite it from another column')
        for r in rows:
            r['NettingSet'] = r.get(col, '')
        headers = headers + ['NettingSet']
        applied.append(f'NettingSet := {col} (--netting-set-column); blank values fall back to context netting_set')
    return headers, rows, applied


def summary_rows(result):
    out = []
    for cp in result['counterparties']:
        for ns in cp['netting_sets']:
            for p in ns['products']:
                for rc in p['risk_classes']:
                    row = {'counterparty': cp['counterparty'], 'netting_set': ns['scope']['netting_set'], 'csa': ns['scope']['csa'],
                           'direction': ns['scope']['direction'], 'product_class': p['product_class'], 'risk_class': rc['risk_class']}
                    row.update({c.lower(): rc['components'].get(c, '0') for c in COMPONENTS})
                    row.update({'risk_class_margin': rc['margin'], 'product_margin': p['margin'], 'netting_set_total': ns['total_simm'],
                                'counterparty_total': cp['total_simm']})
                    out.append(row)
    return out


def fmt(x):
    return f'{Decimal(str(x)):,.2f}'


def print_summary(result, meta, stream):
    w = stream.write
    w(f"SIMM {result['simm_version']} | valuation {meta['valuation_date']} | USD | SHADOW\n")
    if meta['version_selection'].get('warning'):
        w(f"WARNING: {meta['version_selection']['warning']}\n")
    w(f"Portfolio total: {fmt(result['total_simm'])} USD\n")
    for cp in result['counterparties']:
        w(f"\n[{cp['counterparty']}] total {fmt(cp['total_simm'])} USD\n")
        for ns in cp['netting_sets']:
            w(f"  netting set {ns['scope']['netting_set']} / CSA {ns['scope']['csa']}: {fmt(ns['total_simm'])} (add-on {fmt(ns['addon'])})\n")
            for p in ns['products']:
                w(f"    {p['product_class']}: {fmt(p['margin'])}\n")
                for rc in p['risk_classes']:
                    parts = ', '.join(f"{c} {fmt(rc['components'][c])}" for c in COMPONENTS if c in rc['components'] and Decimal(str(rc['components'][c])) != 0)
                    w(f"      {rc['risk_class']}: {fmt(rc['margin'])}  ({parts or 'all components 0'})\n")


def cmd_run(args):
    crif_path = Path(args.crif)
    context = json.loads(Path(args.context).read_text(encoding='utf-8'))
    ensure_no_secrets(context)
    context['valuation_date'] = args.valuation_date
    headers, rows = read_crif(crif_path)
    headers, rows, applied = prepare_rows(headers, rows, args)
    if args.version == 'auto':
        selection = select_version(args.valuation_date)
    else:
        selection = {'version': args.version, 'valuation_date': args.valuation_date, 'override': True,
                     'scheduled_version': select_version(args.valuation_date)['version']}
        if selection['scheduled_version'] != args.version:
            selection['warning'] = f"Version override {args.version} differs from scheduled {selection['scheduled_version']}"
    package = ParameterPackage(selection['version'])
    validation = validate(headers, rows, package, context)
    issues = Counter(f'{i.severity}:{i.code}' for i in validation.issues)
    meta = {'crif_file': str(crif_path), 'crif_sha256': hashlib.sha256(crif_path.read_bytes()).hexdigest(), 'valuation_date': args.valuation_date,
            'version_selection': selection, 'parameter_hash': package.hash, 'input_transformations': applied,
            'validation_status': validation.status, 'issue_counts': dict(issues),
            'rows_read': len(rows), 'rows_used': len(validation.rows), 'rows_excluded': len(validation.excluded)}
    if validation.blocked:
        sys.stderr.write(f'Validation blocked ({validation.status}). Issue counts: {dict(issues)}\n')
        for i in [x for x in validation.issues if x.severity in ('CRITICAL', 'REVIEW_REQUIRED')][:20]:
            sys.stderr.write(f'  {i.severity} {i.code} {i.row_id or ""} {i.field or ""}: {i.message}\n')
        if args.output:
            Path(args.output).write_text(json.dumps({'metadata': meta, 'issues': plain(validation.issues)}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return 2
    result = Engine(package).calculate(validation, {})
    if not args.quiet:
        print_summary(result, meta, sys.stdout)
    if args.output:
        payload = {'metadata': meta, 'result': result if args.include_trace else {k: v for k, v in result.items() if k != 'trace'}}
        Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if args.summary_csv:
        rows_out = summary_rows(result)
        buf = io.StringIO()
        fields = list(rows_out[0]) if rows_out else ['counterparty']
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction='ignore', lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows_out)
        Path(args.summary_csv).write_text(buf.getvalue(), encoding='utf-8-sig')
    return 0


def cmd_versions(args):
    if args.date:
        print(json.dumps(select_version(args.date), ensure_ascii=False, indent=2))
        return 0
    schedule = load_schedule()
    for v in schedule['versions']:
        print(f"{v['version']:9s} use from COB {v['use_from_cob']}  {v['announcement']}")
    print(f"schedule verified through {schedule['verified_through']}")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog='python -m simm', description='ISDA SIMM calculator in USD (shadow/reference use)')
    sub = ap.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run', help='calculate SIMM for one CRIF file')
    run.add_argument('--crif', required=True, help='CRIF file (UTF-8 CSV/TSV or single-sheet XLSX)')
    run.add_argument('--valuation-date', required=True, help='valuation (COB) date YYYY-MM-DD; must match CRIF ValuationDate')
    run.add_argument('--context', required=True, help='JSON: direction, regulation, crif_profile, currency_codes, unit_declarations, scope defaults')
    run.add_argument('--version', default='auto', help="SIMM version, or 'auto' to select by valuation date (default)")
    run.add_argument('--netting-set-column', help='explicitly use this CRIF column as NettingSet (e.g. PortfolioId)')
    run.add_argument('--excel-serial-dates', action='store_true', help='convert numeric Excel ValuationDate cells to ISO dates')
    run.add_argument('--output', help='write metadata and result JSON here')
    run.add_argument('--include-trace', action='store_true', help='include the full calculation trace in --output')
    run.add_argument('--summary-csv', help='write the counterparty/netting set/product/risk class breakdown here')
    run.add_argument('--quiet', action='store_true', help='do not print the summary')
    run.set_defaults(func=cmd_run)
    ver = sub.add_parser('versions', help='show the version schedule or the version for a date')
    ver.add_argument('--date')
    ver.set_defaults(func=cmd_versions)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError, KeyError) as exc:
        sys.stderr.write(f'error: {exc}\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
