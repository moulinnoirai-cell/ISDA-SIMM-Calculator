"""SIMM 2.9+2606 parameter package: values checked against the ISDA public methodology PDF, structure against 2.8+2512."""
from decimal import Decimal
from pathlib import Path
import io, json, tempfile, unittest
from contextlib import redirect_stdout

from simm.cli import main
from simm.parameters import ParameterPackage

ROOT = Path(__file__).resolve().parents[1]


def shape(o):
    return {k: shape(v) for k, v in o.items()} if isinstance(o, dict) else 'list' if isinstance(o, list) else 'value'


class Simm29PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = ParameterPackage('2.9+2606')
        cls.d = cls.p.data

    def test_identity(self):
        self.assertEqual(self.d['version'], '2.9+2606')
        self.assertEqual(self.d['effective_date'], '2026-12-05')
        self.assertEqual(self.d['methodology_id'], 'simm_2_9')
        self.assertEqual(self.p.manifest['sha256'], self.p.hash)
        self.assertEqual(self.p.manifest['source_url'], 'https://www.isda.org/a/ZYxiE/ISDA-SIMM_v2.92606_PUBLIC.pdf')
        self.assertFalse(self.p.manifest['production_approved'])

    def test_same_structure_as_previous_version(self):
        old = json.loads((ROOT / 'parameters' / 'simm_2_8_2512' / 'parameters.json').read_text(encoding='utf-8'))
        a, b = shape(self.d), shape(old)
        for key in b:
            if key in ('version', 'package_version', 'effective_date', 'methodology_id', 'source_sha256'):
                continue
            self.assertEqual(a[key], b[key], key)

    def test_values_from_the_pdf(self):
        d = self.d
        ir = d['InterestRate']
        self.assertEqual([ir['rw']['regular'][t] for t in ('2w', '10y', '30y')], ['100', '49', '63'])
        self.assertEqual([ir['rw']['low'][t] for t in ('2w', '10y')], ['21', '26'])
        self.assertEqual([ir['rw']['high'][t] for t in ('2w', '30y')], ['306', '95'])
        self.assertEqual((ir['inflation_rw'], ir['xccy_rw'], Decimal(ir['hvr']), Decimal(ir['vrw'])), ('46', '21', Decimal('0.8'), Decimal('0.17')))
        self.assertEqual((Decimal(ir['subcurve_corr']), Decimal(ir['inflation_corr']), Decimal(ir['xccy_corr']), Decimal(ir['inter_corr'])),
                         (Decimal('0.972'), Decimal('0.44'), Decimal('-0.09'), Decimal('0.3')))
        self.assertEqual(ir['tenor_corr']['2w']['1m'], '0.72')
        self.assertEqual(ir['tenor_corr']['20y']['30y'], '0.99')
        self.assertEqual((ir['ct']['high'], ir['vt']['high']), ('17000000', '170000000'))
        self.assertIn('CNY', ir['regular_currencies'])
        cq = d['CreditQualifying']
        self.assertEqual([cq['rw'][b] for b in ('1', '4', '7', '11', 'Residual')], ['68', '47', '182', '345', '345'])
        self.assertEqual((Decimal(cq['vrw']), Decimal(cq['base_corr']), Decimal(cq['different_corr'])), (Decimal('0.44'), Decimal('0.12'), Decimal('0.45')))
        self.assertEqual(d['CreditNonQualifying']['inter_corr'], '0.78')
        eq = d['Equity']
        self.assertEqual([eq['rw'][b] for b in ('2', '10', '12')], ['33', '44', '16'])
        self.assertEqual((Decimal(eq['hvr']), Decimal(eq['vol_index_vrw'])), (Decimal('0.55'), Decimal('0.73')))
        com = d['Commodity']
        self.assertEqual([com['rw'][b] for b in ('1', '10', '16')], ['22', '55', '55'])
        self.assertEqual((Decimal(com['hvr']), Decimal(com['vrw']), com['intra_corr']['1']), (Decimal('0.75'), Decimal('0.35'), '0.76'))
        fx = d['FX']
        self.assertEqual(fx['rw'], {'regular': {'regular': '7.2', 'high': '11.3'}, 'high': {'regular': '11.3', 'high': '8.4'}})
        self.assertEqual(fx['delta_corr']['high']['regular']['regular'], '0.79')
        self.assertIn('KRW', fx['high_currencies'])
        self.assertEqual(len(fx['high_currencies']), 37)
        self.assertEqual(d['risk_class_corr']['InterestRate']['Commodity'], '0.42')
        self.assertEqual(d['risk_class_corr']['Equity']['Commodity'], '0.29')

    def test_fx_high_group_changes_krw_rate_class(self):
        self.assertEqual(self.p.fx_group('KRW'), 'high')
        self.assertEqual(ParameterPackage('2.8+2512').fx_group('KRW'), 'regular')
        self.assertEqual(self.p.ir_group('CNY'), 'regular')


class Simm29RunTests(unittest.TestCase):
    def run_example(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(['run', '--crif', str(ROOT / 'examples' / 'crif_example.csv'), '--valuation-date', '2026-06-30',
                         '--context', str(ROOT / 'examples' / 'context.json'), *extra])
        return code, out.getvalue()

    def test_example_runs_with_2_9(self):
        code, text = self.run_example('--version', '2.9+2606')
        self.assertEqual(code, 0)
        self.assertIn('SIMM 2.9+2606', text)
        self.assertIn('Portfolio total: 34,330,409.00 USD', text)  # regression anchor of this engine, not an ISDA figure

    def test_unknown_version_is_a_clean_error(self):
        code, _ = self.run_example('--version', '9.9')
        self.assertEqual(code, 1)


if __name__ == '__main__':
    unittest.main()
