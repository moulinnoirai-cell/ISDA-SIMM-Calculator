"""Command-line runner: CRIF -> validation -> version selection -> engine -> summary (USD).

    python -m simm run --crif FILE --valuation-date YYYY-MM-DD --context context.json [options]
    python -m simm versions [--date YYYY-MM-DD]
    python -m simm serve [--port 8765] [--open]     # local web page: upload CRIF, pick version, press Calculate

Every input transformation (column mapping, Excel serial dates, version override) is explicit on the command
line and recorded in the output. Amounts are CRIF AmountUSD; no FX conversion and no network access.
--report writes a self-contained HTML page with the counterparty summary and an Excel download.
"""
from decimal import Decimal
from pathlib import Path
import argparse, csv, io, json, sys

from .models import plain
from .report import fmt, write_report
from .service import ensure_no_secrets, run_crif  # ensure_no_secrets is re-exported for callers of simm.cli
from .versioning import load_schedule, select_version

COMPONENTS = ('Delta', 'Vega', 'Curvature', 'BaseCorr')


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
    outcome = run_crif(crif_path, context, args.valuation_date, args.version, args.netting_set_column, args.excel_serial_dates)
    meta, validation, result = outcome.meta, outcome.validation, outcome.result
    issues = meta['issue_counts']
    if validation.blocked:
        sys.stderr.write(f'Validation blocked ({validation.status}). Issue counts: {dict(issues)}\n')
        for i in [x for x in validation.issues if x.severity in ('CRITICAL', 'REVIEW_REQUIRED')][:20]:
            sys.stderr.write(f'  {i.severity} {i.code} {i.row_id or ""} {i.field or ""}: {i.message}\n')
        if args.output:
            Path(args.output).write_text(json.dumps({'metadata': meta, 'issues': plain(validation.issues)}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return 2
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
    if args.report:
        write_report(args.report, result, meta)
        if not args.quiet:
            sys.stdout.write(f'\nReport written to {args.report}\n')
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


def cmd_serve(args):
    from .webapp import serve
    return serve(args.host, args.port, args.open, args.allow_remote)


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
    run.add_argument('--report', help='write an HTML report here: counterparty summary, drill-down and Excel download (offline)')
    run.add_argument('--quiet', action='store_true', help='do not print the summary')
    run.set_defaults(func=cmd_run)
    ver = sub.add_parser('versions', help='show the version schedule or the version for a date')
    ver.add_argument('--date')
    ver.set_defaults(func=cmd_versions)
    web = sub.add_parser('serve', help='start the local web page: upload a CRIF, choose the SIMM version, press Calculate')
    web.add_argument('--host', default='127.0.0.1', help='address to listen on (default 127.0.0.1, this machine only)')
    web.add_argument('--port', type=int, default=8765, help='port (default 8765; 0 picks a free one)')
    web.add_argument('--open', action='store_true', help='also try to open the page in a browser')
    web.add_argument('--allow-remote', action='store_true', help='allow a non-local --host; anyone reaching the port can upload and read results')
    web.set_defaults(func=cmd_serve)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError, KeyError, OSError) as exc:
        sys.stderr.write(f'error: {exc}\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
