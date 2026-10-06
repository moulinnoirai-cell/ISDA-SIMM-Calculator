"""The shipped CRIF template, example and documentation stay consistent with the validator and engine."""
import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal as D
from pathlib import Path

from simm.cli import main

ROOT = Path(__file__).resolve().parents[1]


class ExampleTests(unittest.TestCase):
    def test_example_runs_and_covers_every_risk_class_and_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out.json'
            with redirect_stdout(io.StringIO()):
                code = main(['run', '--crif', str(ROOT / 'examples/crif_example.csv'), '--valuation-date', '2026-06-30',
                             '--context', str(ROOT / 'examples/context.json'), '--output', str(out)])
            self.assertEqual(code, 0)
            result = json.loads(out.read_text(encoding='utf-8'))['result']
        seen_classes, seen_components = set(), set()
        for cp in result['counterparties']:
            for ns in cp['netting_sets']:
                for p in ns['products']:
                    for rc in p['risk_classes']:
                        seen_classes.add(rc['risk_class'])
                        seen_components |= {k for k, v in rc['components'].items() if D(v) != 0}
        self.assertEqual(seen_classes, {'InterestRate', 'CreditQualifying', 'CreditNonQualifying', 'Equity', 'Commodity', 'FX'})
        self.assertEqual(seen_components, {'Delta', 'Vega', 'Curvature', 'BaseCorr'})
        a1 = result['counterparties'][0]['netting_sets'][0]
        self.assertEqual(D(a1['addon']), D('50000'))  # 5% x 1,000,000 notional
        self.assertEqual(next(p['multiplier'] for p in a1['products'] if p['product_class'] == 'RatesFX'), '1.2')

    def test_template_header_matches_example_and_documentation(self):
        template = next(csv.reader(open(ROOT / 'crif/crif_template.csv', encoding='utf-8')))
        example = next(csv.reader(open(ROOT / 'examples/crif_example.csv', encoding='utf-8')))
        self.assertEqual(template, example)
        doc = (ROOT / 'crif/CRIF_FORMAT.md').read_text(encoding='utf-8')
        for column in template:
            with self.subTest(column=column):
                self.assertIn(f'`{column}`', doc)

    def test_context_template_is_blocked_until_filled(self):
        template = json.loads((ROOT / 'config/context.template.json').read_text(encoding='utf-8'))
        self.assertTrue(all(v['source'] == '' for v in template['unit_declarations'].values()))
        self.assertEqual(template['regulation'], '')
