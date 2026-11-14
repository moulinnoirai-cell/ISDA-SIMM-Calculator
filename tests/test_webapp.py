"""Local web app: page, API, safety checks and the CRIF -> Calculate -> report flow."""
from pathlib import Path
import http.client, json, threading, unittest

from simm.webapp import SimmServer, example_payload

ROOT = Path(__file__).resolve().parents[1]


class WebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = SimmServer(('127.0.0.1', 0), quiet=True)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.example = example_payload()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=20)
        hdrs = dict(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            hdrs.setdefault('Content-Type', 'application/json')
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        content = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), content

    def payload(self, **overrides):
        ex = self.example
        base = {'file_name': ex['file_name'], 'file_b64': ex['file_b64'], 'valuation_date': ex['valuation_date'], **ex['fields']}
        base.update(overrides)
        return base

    def calc(self, **overrides):
        status, _, content = self.request('POST', '/api/calculate', self.payload(**overrides))
        return status, json.loads(content)

    # ---- page and listings
    def test_page_has_upload_version_and_calculate_controls(self):
        status, headers, content = self.request('GET', '/')
        page = content.decode('utf-8')
        self.assertEqual(status, 200)
        for needle in ('type="file"', 'id="version"', 'id="run"', 'id="valuation_date"', '/api/calculate', 'id="unit_source"'):
            self.assertIn(needle, page)
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_versions_list_includes_2_9(self):
        status, _, content = self.request('GET', '/api/versions')
        data = json.loads(content)
        self.assertEqual(status, 200)
        self.assertEqual(data['versions'][-1]['version'], '2.9+2606')
        self.assertEqual(data['versions'][-1]['use_from_cob'], '2026-12-04')

    def test_inspect_reports_rows_and_dates(self):
        status, _, content = self.request('POST', '/api/inspect', {'file_name': self.example['file_name'], 'file_b64': self.example['file_b64']})
        info = json.loads(content)
        self.assertEqual((status, info['status'], info['rows'], info['valuation_dates']), (200, 'ok', 38, ['2026-06-30']))

    # ---- calculation
    def test_calculate_with_each_version(self):
        totals = {}
        for version, expected in (('auto', '2.8+2506'), ('2.8+2512', '2.8+2512'), ('2.9+2606', '2.9+2606')):
            status, data = self.calc(version=version)
            self.assertEqual((status, data['status'], data['version']), (200, 'ok', expected))
            totals[version] = data['total_simm']
        self.assertEqual(totals['2.9+2606'], '34,330,409.00')
        self.assertEqual(len(set(totals.values())), 3)

    def test_report_is_served_and_is_the_selected_version(self):
        _, data = self.calc(version='2.9+2606')
        status, headers, content = self.request('GET', data['report_url'])
        self.assertEqual(status, 200)
        self.assertTrue(content.startswith(b'<!doctype html>'))
        self.assertIn(b'2.9+2606', content)
        self.assertEqual(headers['X-Frame-Options'], 'SAMEORIGIN')
        status, headers, _ = self.request('GET', data['report_url'] + '?download=1')
        self.assertIn('SIMM_2026-06-30_2.9+2606.html', headers['Content-Disposition'])

    def test_matches_command_line_result(self):
        import io
        from contextlib import redirect_stdout
        from simm.cli import main
        out = io.StringIO()
        with redirect_stdout(out):
            main(['run', '--crif', str(ROOT / 'examples' / 'crif_example.csv'), '--valuation-date', '2026-06-30',
                  '--context', str(ROOT / 'examples' / 'context.json'), '--version', '2.9+2606'])
        _, data = self.calc(version='2.9+2606')
        self.assertIn(f"Portfolio total: {data['total_simm']} USD", out.getvalue())

    def test_validation_block_returns_issues_not_a_report(self):
        status, data = self.calc(valuation_date='2026-12-04', version='2.9+2606')
        self.assertEqual((status, data['status']), (200, 'blocked'))
        self.assertNotIn('report_url', data)
        self.assertIn('DATE_MISMATCH', {i['code'] for i in data['issues']})

    def test_uploaded_context_replaces_the_form(self):
        context = json.loads((ROOT / 'examples' / 'context.json').read_text(encoding='utf-8'))
        status, data = self.calc(context_json=json.dumps(context), regulation='', unit_source='', direction='')
        self.assertEqual((status, data['status']), (200, 'ok'))

    def test_form_errors_are_readable_and_leak_no_paths(self):
        for overrides, fragment in (({'unit_source': ''}, 'sensitivity units'), ({'regulation': ''}, 'regulation'), ({'direction': 'x'}, 'direction'),
                                    ({'valuation_date': '30/06/2026'}, 'YYYY-MM-DD'), ({'version': '9.9'}, 'Unknown SIMM version'),
                                    ({'file_name': 'crif.exe'}, 'Supported files'), ({'file_b64': '***'}, 'decoded'), ({'file_b64': ''}, 'empty'),
                                    ({'context_json': '{broken'}, 'not valid JSON'), ({'context_json': '{"api_key": "x"}'}, 'Credentials')):
            with self.subTest(overrides=overrides):
                status, data = self.calc(**overrides)
                self.assertEqual((status, data['status']), (400, 'error'))
                self.assertIn(fragment, data['message'])
                self.assertNotIn('/', data['message'].replace('YYYY-MM-DD', '').replace('CollectRegulations / PostRegulations', '').replace('/api', ''))

    # ---- safety
    def test_foreign_host_and_origin_are_refused(self):
        self.assertEqual(self.request('GET', '/api/versions', headers={'Host': 'evil.example.com'})[0], 403)
        self.assertEqual(self.request('GET', '/api/versions', headers={'Host': '127.0.0.1.evil.example.com:80'})[0], 403)
        self.assertEqual(self.request('POST', '/api/calculate', self.payload(), headers={'Origin': 'http://evil.example.com'})[0], 403)

    def test_post_needs_json_and_a_sane_length(self):
        self.assertEqual(self.request('POST', '/api/calculate', raw=b'{}', headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('POST', '/api/calculate', raw=b'[]', headers={'Content-Type': 'application/json'})[0], 400)
        self.assertEqual(self.request('POST', '/api/calculate', raw=b'not json', headers={'Content-Type': 'application/json'})[0], 400)

    def test_oversized_upload_is_refused_before_reading(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=20)
        conn.putrequest('POST', '/api/calculate')
        conn.putheader('Content-Type', 'application/json')
        conn.putheader('Content-Length', str(200 * 1024 * 1024))
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        resp.read()
        conn.close()

    def test_connection_survives_a_refused_request_with_a_body(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=20)
        conn.request('POST', '/api/calculate', body=b'{"a": 1}', headers={'Content-Type': 'text/plain'})
        first = conn.getresponse()
        first.read()
        self.assertEqual(first.status, 415)
        conn.request('GET', '/api/versions')  # the library reconnects; the server must not choke on leftover bytes
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()

    def test_unknown_routes_and_reports(self):
        self.assertEqual(self.request('GET', '/nope')[0], 404)
        self.assertEqual(self.request('GET', '/report/doesnotexist')[0], 404)
        self.assertEqual(self.request('POST', '/api/nope', {})[0], 404)

    def test_remote_listening_needs_explicit_opt_in(self):
        from simm.webapp import serve
        with self.assertRaises(ValueError):
            serve('0.0.0.0', 0)


if __name__ == '__main__':
    unittest.main()
