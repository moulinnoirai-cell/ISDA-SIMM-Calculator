"""HTML report and Excel workbook, built from the synthetic example (standard library only, no network)."""
import base64
import io
import json
import re
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from decimal import Decimal as D
from pathlib import Path

from simm.cli import main
from simm.report import COMPONENTS, LABELS, _PAGE, build_xlsx, render_report, report_data

ROOT = Path(__file__).resolve().parents[1]
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def run_example(tmp, *extra):
    out = Path(tmp) / 'out.json'
    with redirect_stdout(io.StringIO()):
        code = main(['run', '--crif', str(ROOT / 'examples/crif_example.csv'), '--valuation-date', '2026-06-30',
                     '--context', str(ROOT / 'examples/context.json'), '--output', str(out), *extra])
    payload = json.loads(out.read_text(encoding='utf-8'))
    return code, payload['result'], payload['metadata']


def sheet_rows(xlsx, number):
    """{row number: {column letter: text}} of one worksheet."""
    root = ET.fromstring(zipfile.ZipFile(io.BytesIO(xlsx)).read(f'xl/worksheets/sheet{number}.xml'))
    rows = {}
    for row in root.find('m:sheetData', NS):
        values = {}
        for c in row:
            column = re.match(r'[A-Z]+', c.get('r')).group()
            values[column] = c.find('m:is/m:t', NS).text if c.get('t') == 'inlineStr' else c.find('m:v', NS).text
        rows[int(row.get('r'))] = values
    return rows


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            cls.code, cls.result, cls.meta = run_example(tmp)

    def test_counterparties_are_ranked_and_shares_add_up(self):
        data = report_data(self.result, self.meta)
        self.assertEqual([c['name'] for c in data['counterparties']], ['CP_B', 'CP_A'])
        self.assertEqual([c['simm_txt'] for c in data['counterparties']], ['19,196,177.54', '14,300,634.89'])
        self.assertEqual([c['share_txt'] for c in data['counterparties']], ['57.3%', '42.7%'])
        self.assertAlmostEqual(sum(c['share'] for c in data['counterparties']), 1.0, places=12)
        self.assertEqual(data['totals'], {'simm_txt': '33,496,812.42', 'counterparties': 2, 'netting_sets': 3,
                                          'share_txt': '100.0%', 'addon_txt': '50,000.00'})
        self.assertEqual(data['xlsx_name'], 'SIMM_2026-06-30_2.8+2506.xlsx')

    def test_workbook_is_a_complete_package_in_both_languages(self):
        for lang, first in (('en', 'Counterparties'), ('ko', '거래상대방')):
            with self.subTest(lang=lang):
                archive = zipfile.ZipFile(io.BytesIO(build_xlsx(self.result, self.meta, lang)))
                self.assertIsNone(archive.testzip())
                for name in archive.namelist():
                    ET.fromstring(archive.read(name))  # every part is well-formed XML
                types = archive.read('[Content_Types].xml').decode('utf-8')
                for part in ['/xl/workbook.xml', '/xl/styles.xml'] + [f'/xl/worksheets/sheet{i}.xml' for i in range(1, 5)]:
                    self.assertIn(f'PartName="{part}"', types)
                sheets = [s.get('name') for s in ET.fromstring(archive.read('xl/workbook.xml')).find('m:sheets', NS)]
                self.assertEqual(len(sheets), 4)
                self.assertEqual(sheets[0], first)

    def test_workbook_values_match_the_engine(self):
        xlsx = build_xlsx(self.result, self.meta, 'en')
        by_name = {c['counterparty']: c for c in self.result['counterparties']}
        counterparties = sheet_rows(xlsx, 1)
        self.assertEqual(counterparties[1]['B'], 'Counterparty')
        self.assertEqual([counterparties[r]['B'] for r in (2, 3)], ['CP_B', 'CP_A'])
        for r in (2, 3):
            self.assertEqual(float(counterparties[r]['D']), float(D(by_name[counterparties[r]['B']]['total_simm'])))
        self.assertNotIn(4, counterparties)  # blank row before the total
        self.assertEqual(counterparties[5]['B'], 'Total')
        self.assertEqual(float(counterparties[5]['D']), float(D(self.result['total_simm'])))
        netting_sets = sheet_rows(xlsx, 2)
        self.assertEqual(len(netting_sets), 1 + 3)
        risk_classes = sheet_rows(xlsx, 3)
        expected = sum(len(p['risk_classes']) for c in self.result['counterparties'] for n in c['netting_sets'] for p in n['products'])
        self.assertEqual(len(risk_classes), 1 + expected)
        run_info = {v['A']: v.get('B') for v in sheet_rows(xlsx, 4).values() if 'A' in v}
        self.assertEqual(run_info['SIMM version'], '2.8+2506')
        self.assertEqual(run_info['CRIF SHA-256'], self.meta['crif_sha256'])

    def test_workbook_is_deterministic(self):
        self.assertEqual(build_xlsx(self.result, self.meta, 'ko'), build_xlsx(self.result, self.meta, 'ko'))

    def test_warnings_for_manual_version_and_excluded_rows(self):
        meta = dict(self.meta, rows_excluded=2, version_selection={'version': '2.7', 'override': True, 'scheduled_version': '2.8+2506'})
        self.assertEqual([w['code'] for w in report_data(self.result, meta)['meta']['warnings']], ['warn_override', 'warn_excluded'])
        texts = [v.get('B', '') for v in sheet_rows(build_xlsx(self.result, meta, 'en'), 4).values()]
        self.assertTrue(any('set manually (2.7)' in t for t in texts))
        self.assertTrue(any('2 CRIF rows were excluded' in t for t in texts))

    def test_page_is_self_contained_and_escapes_input(self):
        result = json.loads(json.dumps(self.result))
        result['counterparties'][0]['counterparty'] = '</script><img src=x onerror=alert(1)>'
        page = render_report(result, self.meta)
        self.assertNotIn('</script><img', page)
        self.assertIn('\\u003c/script\\u003e\\u003cimg', page)
        for marker in ('<script src', '<link', 'http://', 'https://', '@import', 'url('):
            self.assertNotIn(marker, page)
        self.assertEqual(page.count('</script>'), 3)
        workbooks = json.loads(re.search(r'id="simm-xlsx">(.*?)</script>', page, re.S).group(1))
        self.assertEqual(set(workbooks), {'ko', 'en'})
        for b64 in workbooks.values():
            self.assertTrue(base64.b64decode(b64).startswith(b'PK'))

    def test_labels_cover_both_languages_and_the_page(self):
        self.assertEqual(set(LABELS['en']), set(LABELS['ko']))
        used = set(re.findall(r'data-t="(\w+)"', _PAGE)) | set(re.findall(r"\bt\('(\w+)'", _PAGE)) | set(re.findall(r"\['(\w+)', ", _PAGE))
        self.assertLessEqual(used - set(COMPONENTS), set(LABELS['en']))

    def test_cli_writes_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'report.html'
            code, _, _ = run_example(tmp, '--report', str(report))
            self.assertEqual(code, 0)
            page = report.read_text(encoding='utf-8')
        self.assertIn('id="simm-data"', page)
        self.assertIn('"name":"CP_B"', page)


if __name__ == '__main__':
    unittest.main()
