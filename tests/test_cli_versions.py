"""Version schedule, USD-only FX handling and the command-line runner (synthetic data only)."""
import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal as D
from pathlib import Path

from simm.cli import ensure_no_secrets, main
from simm.engine import Engine
from simm.models import Scope, RiskRow, ValidationResult
from simm.parameters import ParameterPackage
from simm.versioning import load_schedule, select_version

SCOPE = Scope('SYN_CP', 'SYN_LE', 'SYN_NS', 'SYN_CSA', 'collect', 'SYN_REG')


def fx(q, amount):
    return RiskRow(f'row:{q}', SCOPE, 'RatesFX', 'Risk_FX', q, '', '', '', D(amount), '', '', {})


class VersionScheduleTests(unittest.TestCase):
    def test_boundaries_follow_isda_use_from_cob_dates(self):
        cases = {'2022-12-02': '2.5', '2023-07-13': '2.5', '2023-07-14': '2.5A', '2023-11-30': '2.5A', '2023-12-01': '2.6',
                 '2024-12-05': '2.6', '2024-12-06': '2.7', '2025-07-10': '2.7', '2025-07-11': '2.7+2412', '2025-12-04': '2.7+2412',
                 '2025-12-05': '2.8+2506', '2026-07-09': '2.8+2506', '2026-07-10': '2.8+2512'}
        for day, version in cases.items():
            with self.subTest(day=day):
                self.assertEqual(select_version(day)['version'], version)

    def test_before_first_entry_is_rejected(self):
        with self.assertRaises(ValueError):
            select_version('2022-12-01')

    def test_beyond_verified_schedule_is_flagged(self):
        sel = select_version('2030-01-02')
        self.assertTrue(sel['beyond_verified_schedule'])
        self.assertIsNotNone(sel['warning'])
        self.assertFalse(select_version('2026-08-03')['beyond_verified_schedule'])

    def test_every_scheduled_version_has_a_package(self):
        for entry in load_schedule()['versions']:
            with self.subTest(version=entry['version']):
                self.assertEqual(ParameterPackage(entry['version']).version, entry['version'])


class UsdOnlyTests(unittest.TestCase):
    def test_usd_is_the_calculation_currency(self):
        rows = [fx('USD', '1000000'), fx('EUR', '1000')]
        result = Engine(ParameterPackage()).calculate(ValidationResult(rows=rows), {})
        self.assertEqual(D(result['total_simm']), D('7400'))
        self.assertEqual(result['currency'], 'USD')
        self.assertNotIn('reporting', result)

    def test_no_calculation_currency_argument(self):
        with self.assertRaises(TypeError):
            Engine(ParameterPackage(), calculation_currency='KRW')

    def test_secret_keys_rejected(self):
        for cfg in [{'password': 'x'}, {'a': {'api_key': 'x'}}, {'a': [{'cookie': 'x'}]}]:
            with self.subTest(cfg=cfg):
                with self.assertRaises(ValueError):
                    ensure_no_secrets(cfg)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        units = ParameterPackage().data['input_units']
        self.context = {'direction': 'collect', 'regulation': 'SYN_REG', 'crif_profile': 'public_rds_1_36_methodology_2_8',
                        'currency_codes': ['USD', 'EUR', 'KRW'], 'legal_entity': 'SYN_LE', 'csa': 'SYN_CSA', 'netting_set': 'SYN_DEFAULT',
                        'unit_declarations': {k: {'unit': v, 'source': 'SYNTHETIC_FIXTURE'} for k, v in units.items()}}
        (self.dir / 'ctx.json').write_text(json.dumps(self.context), encoding='utf-8')
        header = ['ValuationDate', 'Counterparty', 'PortfolioId', 'ProductClass', 'RiskType', 'Qualifier', 'Bucket', 'Label1', 'Label2', 'Amount', 'AmountCurrency', 'AmountUSD']
        rows = [['2026-06-30', 'CP_A', 'PF1', 'RatesFX', 'Risk_IRCurve', 'USD', '1', '5y', 'OIS', '1000', 'USD', '1000'],
                ['2026-06-30', 'CP_A', 'PF2', 'RatesFX', 'Risk_IRCurve', 'USD', '1', '5y', 'OIS', '-1000', 'USD', '-1000'],
                ['2026-06-30', 'CP_A', 'PF1', 'RatesFX', 'Risk_FX', 'EUR', '', '', '', '1000', 'USD', '1000']]
        buf = io.StringIO()
        csv.writer(buf, lineterminator='\n').writerows([header] + rows)
        (self.dir / 'crif.csv').write_text(buf.getvalue(), encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['run', '--crif', str(self.dir / 'crif.csv'), '--valuation-date', '2026-06-30', '--context', str(self.dir / 'ctx.json'),
                         '--output', str(self.dir / 'out.json'), '--summary-csv', str(self.dir / 'sum.csv'), *extra])
        return code, out.getvalue(), err.getvalue()

    def test_auto_version_and_netting_set_mapping(self):
        code, out, err = self.run_cli('--netting-set-column', 'PortfolioId')
        self.assertEqual(code, 0, err)
        payload = json.loads((self.dir / 'out.json').read_text(encoding='utf-8'))
        self.assertEqual(payload['metadata']['version_selection']['version'], '2.8+2506')
        self.assertEqual(payload['result']['simm_version'], '2.8+2506')
        ns = {n['scope']['netting_set']: D(n['total_simm']) for n in payload['result']['counterparties'][0]['netting_sets']}
        self.assertEqual(set(ns), {'PF1', 'PF2'})
        self.assertEqual(ns['PF2'], D('61000'))  # separate netting sets do not offset
        self.assertNotIn('trace', payload['result'])
        self.assertIn('NettingSet := PortfolioId', payload['metadata']['input_transformations'][0])
        self.assertTrue((self.dir / 'sum.csv').read_text(encoding='utf-8-sig').startswith('counterparty,'))
        self.assertIn('Portfolio total', out)

    def test_without_mapping_portfolios_net_in_default_netting_set(self):
        code, _, err = self.run_cli()
        self.assertEqual(code, 0, err)
        payload = json.loads((self.dir / 'out.json').read_text(encoding='utf-8'))
        self.assertEqual([n['scope']['netting_set'] for n in payload['result']['counterparties'][0]['netting_sets']], ['SYN_DEFAULT'])

    def test_blocked_validation_returns_2(self):
        ctx = dict(self.context, regulation='')
        (self.dir / 'ctx.json').write_text(json.dumps(ctx), encoding='utf-8')
        code, _, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn('REGULATION_REQUIRED', err)

    def test_reporting_options_are_not_available(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            main(['run', '--crif', 'x.csv', '--valuation-date', '2026-06-30', '--context', 'c.json', '--report-currency', 'KRW'])
