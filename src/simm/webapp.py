"""Local web app: upload a CRIF file, pick a SIMM version, press Calculate and read the result in the browser.

    python -m simm serve [--port 8765] [--open]

The server uses only the standard library and listens on 127.0.0.1 only. The page, the calculation and the report all stay on
this machine: nothing is fetched from or sent to the network, and an uploaded CRIF exists only as a temporary file during the
request. The result is the same self-contained HTML report that `--report` writes (counterparty summary, drill-down, Excel download).

Endpoints (JSON in, JSON out):
    GET  /                 the page
    GET  /api/versions     version schedule for the version picker
    GET  /api/example      the bundled example CRIF and settings (when the examples folder is present)
    POST /api/inspect      CRIF file -> row count, valuation dates, regulations found
    POST /api/calculate    CRIF file + settings -> report URL, or the validation issues that blocked the run
    GET  /report/<id>      a calculated report (kept in memory; the last few only)
"""
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import base64, binascii, json, re, secrets, sys, tempfile, threading, traceback, webbrowser

from .crif import read_crif
from .models import plain
from .report import _warnings, fmt, render_report
from .service import ensure_no_secrets, excel_serial_to_iso, run_crif
from .versioning import ROOT, load_schedule

MAX_BODY = 96 * 1024 * 1024          # request body limit (base64 text of the CRIF plus settings)
MAX_REPORTS = 8                      # reports kept in memory
MAX_ISSUES_SHOWN = 200
CRIF_EXTENSIONS = ('.csv', '.tsv', '.txt', '.crif', '.xlsx')
LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '[::1]')
TEXT_FIELDS = ('regulation', 'legal_entity', 'csa', 'netting_set', 'unit_source', 'netting_set_column')
TEMPLATE = ROOT / 'config' / 'context.template.json'
EXAMPLE_CRIF = ROOT / 'examples' / 'crif_example.csv'
EXAMPLE_CONTEXT = ROOT / 'examples' / 'context.json'


class UserError(ValueError):
    """A problem with the input that the user can fix; its message is shown in the page."""


def _text(payload, key, limit=300):
    value = payload.get(key, '')
    if value is None:
        return ''
    if not isinstance(value, str):
        raise UserError(f'{key} must be text')
    value = value.strip()
    if len(value) > limit:
        raise UserError(f'{key} is too long')
    return value


def decode_crif(payload):
    """Validated (file name, bytes) of the uploaded CRIF."""
    name = _text(payload, 'file_name', 260)
    ext = Path(name).suffix.lower()
    if ext not in CRIF_EXTENSIONS:
        raise UserError('Supported files: .csv, .tsv, .txt, .crif and single-sheet .xlsx')
    try:
        raw = base64.b64decode(payload.get('file_b64', ''), validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise UserError('The uploaded file could not be decoded')
    if not raw:
        raise UserError('The uploaded file is empty')
    return name, ext, raw


def with_temp_crif(payload, action):
    """Write the upload to a private temporary file, run `action(path, name)` and remove the file again."""
    name, ext, raw = decode_crif(payload)
    with tempfile.TemporaryDirectory(prefix='simm-') as folder:
        path = Path(folder) / ('crif' + ext)
        path.write_bytes(raw)
        return action(path, name)


def build_context(payload):
    """Calculation context: an uploaded context.json as is, otherwise the template filled from the form."""
    uploaded = _text(payload, 'context_json', MAX_BODY)
    if uploaded:
        try:
            context = json.loads(uploaded)
        except json.JSONDecodeError as exc:
            raise UserError(f'context.json is not valid JSON: {exc}')
        if not isinstance(context, dict):
            raise UserError('context.json must be a JSON object')
        ensure_no_secrets(context)
        return context
    direction = _text(payload, 'direction')
    if direction not in ('collect', 'post'):
        raise UserError('Select the direction: collect or post')
    regulation = _text(payload, 'regulation')
    if not regulation:
        raise UserError('Enter the regulation (it must match the CRIF CollectRegulations / PostRegulations values)')
    unit_source = _text(payload, 'unit_source')
    if not unit_source:
        raise UserError('Enter the source that documents the sensitivity units of this CRIF')
    context = json.loads(TEMPLATE.read_text(encoding='utf-8'))
    context.update({'direction': direction, 'regulation': regulation})
    for key in ('legal_entity', 'csa', 'netting_set'):
        context[key] = _text(payload, key)
    for declaration in context['unit_declarations'].values():
        declaration['source'] = unit_source
    return context


def inspect_crif(payload):
    def action(path, name):
        headers, rows = read_crif(path)
        dates = sorted({r.get('ValuationDate', '') for r in rows if r.get('ValuationDate')})
        serial = bool(dates) and all(re.fullmatch(r'\d+(\.\d+)?', d) for d in dates)
        regulations = set()
        for column in ('CollectRegulations', 'PostRegulations'):
            if column in headers:
                for r in rows:
                    regulations.update(x.strip() for x in r.get(column, '').replace('[', '').replace(']', '').split(',') if x.strip())
        return {'status': 'ok', 'file_name': name, 'rows': len(rows), 'columns': len(headers),
                'valuation_dates': dates[:5], 'excel_serial': serial,
                'suggested_dates': [excel_serial_to_iso(d) for d in dates[:5]] if serial else dates[:5],
                'regulations': sorted(regulations)[:50]}
    return with_temp_crif(payload, action)


class Reports:
    """The last few calculated reports, in memory only."""

    def __init__(self, limit=MAX_REPORTS):
        self._items = OrderedDict()
        self._limit = limit
        self._lock = threading.Lock()

    def add(self, html, filename):
        key = secrets.token_urlsafe(12)
        with self._lock:
            self._items[key] = (html, filename)
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
        return key

    def get(self, key):
        with self._lock:
            return self._items.get(key)


def calculate(payload, reports):
    valuation_date = _text(payload, 'valuation_date', 10)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', valuation_date):
        raise UserError('Enter the valuation date as YYYY-MM-DD')
    version = _text(payload, 'version', 20) or 'auto'
    context = build_context(payload)
    netting_set_column = _text(payload, 'netting_set_column', 100) or None
    excel_serial_dates = bool(payload.get('excel_serial_dates'))

    def action(path, name):
        return run_crif(path, context, valuation_date, version, netting_set_column, excel_serial_dates, crif_label=name)

    outcome = with_temp_crif(payload, action)
    meta = outcome.meta
    if outcome.blocked:
        issues = [i for i in outcome.validation.issues if i.severity in ('CRITICAL', 'REVIEW_REQUIRED')]
        return {'status': 'blocked', 'validation_status': meta['validation_status'], 'issue_counts': meta['issue_counts'],
                'version': meta['version_selection']['version'], 'issue_total': len(issues),
                'issues': [plain(i) for i in issues[:MAX_ISSUES_SHOWN]]}
    result = outcome.result
    version_used = result['simm_version']
    filename = f"SIMM_{valuation_date}_{version_used}.html"
    key = reports.add(render_report(result, meta), filename)
    return {'status': 'ok', 'report_url': f'/report/{key}', 'version': version_used, 'valuation_date': valuation_date,
            'total_simm': fmt(result['total_simm']), 'counterparties': len(result['counterparties']),
            'rows': {'read': meta['rows_read'], 'used': meta['rows_used'], 'excluded': meta['rows_excluded']},
            'version_warning': meta['version_selection'].get('warning'), 'warnings': [w['code'] for w in _warnings(meta)]}


def versions_payload():
    schedule = load_schedule()
    return {'versions': [{k: v[k] for k in ('version', 'use_from_cob', 'isda_effective_date', 'announcement')} for v in schedule['versions']],
            'verified_through': schedule['verified_through']}


def example_payload():
    if not (EXAMPLE_CRIF.exists() and EXAMPLE_CONTEXT.exists()):
        return None
    context = json.loads(EXAMPLE_CONTEXT.read_text(encoding='utf-8'))
    source = next(iter(context['unit_declarations'].values()))['source']
    return {'file_name': EXAMPLE_CRIF.name, 'file_b64': base64.b64encode(EXAMPLE_CRIF.read_bytes()).decode('ascii'),
            'valuation_date': '2026-06-30',
            'fields': {'direction': context['direction'], 'regulation': context['regulation'], 'legal_entity': context['legal_entity'],
                       'csa': context['csa'], 'netting_set': context['netting_set'], 'unit_source': source}}


class Handler(BaseHTTPRequestHandler):
    server_version = 'SimmLocal'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    # ---- plumbing
    def log_message(self, fmt_, *args):
        if self.server.quiet:
            return
        sys.stderr.write('%s - %s\n' % (self.address_string(), fmt_ % args))

    def _send(self, status, body, content_type, extra=None):
        data = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        if self.close_connection:
            self.send_header('Connection', 'close')  # a request body we did not read must not be parsed as the next request
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False), 'application/json; charset=utf-8')

    def _guard(self):
        """Only the page's own origin may talk to the server: blocks DNS-rebinding and cross-site requests."""
        host = self.headers.get('Host', '').lower()
        if not self.server.allow_remote:
            name = host.rsplit(':', 1)[0] if not host.endswith(']') else host
            if name not in LOOPBACK_HOSTS:
                self.close_connection = True
                self._json(HTTPStatus.FORBIDDEN, {'status': 'error', 'message': 'Local access only'})
                return False
            origin = self.headers.get('Origin')
            if origin and origin.lower() != f'http://{host}':
                self.close_connection = True
                self._json(HTTPStatus.FORBIDDEN, {'status': 'error', 'message': 'Cross-site request refused'})
                return False
        return True

    # ---- routes
    def do_GET(self):
        if not self._guard():
            return
        path = self.path.split('?', 1)[0]
        query = self.path.split('?', 1)[1] if '?' in self.path else ''
        if path == '/':
            self._send(HTTPStatus.OK, PAGE, 'text/html; charset=utf-8', {'X-Frame-Options': 'DENY'})
        elif path == '/api/versions':
            self._json(HTTPStatus.OK, versions_payload())
        elif path == '/api/example':
            payload = example_payload()
            if payload is None:
                self._json(HTTPStatus.NOT_FOUND, {'status': 'error', 'message': 'No example files in this installation'})
            else:
                self._json(HTTPStatus.OK, payload)
        elif path.startswith('/report/'):
            item = self.server.reports.get(path[len('/report/'):])
            if item is None:
                self._json(HTTPStatus.NOT_FOUND, {'status': 'error', 'message': 'Report not found (only the latest reports are kept)'})
                return
            html, filename = item
            extra = {'X-Frame-Options': 'SAMEORIGIN'}
            if 'download=1' in query.split('&'):
                extra['Content-Disposition'] = f'attachment; filename="{filename}"'
            self._send(HTTPStatus.OK, html, 'text/html; charset=utf-8', extra)
        else:
            self._json(HTTPStatus.NOT_FOUND, {'status': 'error', 'message': 'Not found'})

    def do_POST(self):
        if not self._guard():
            return
        route = {'/api/inspect': inspect_crif, '/api/calculate': lambda p: calculate(p, self.server.reports)}.get(self.path)
        if route is None:
            self.close_connection = True
            self._json(HTTPStatus.NOT_FOUND, {'status': 'error', 'message': 'Not found'})
            return
        if not self.headers.get('Content-Type', '').lower().startswith('application/json'):
            self.close_connection = True
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {'status': 'error', 'message': 'Send application/json'})
            return
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            self.close_connection = True
            self._json(HTTPStatus.LENGTH_REQUIRED, {'status': 'error', 'message': 'Content-Length required'})
            return
        if length > MAX_BODY or length < 0:
            self.close_connection = True
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {'status': 'error', 'message': 'The file is too large for the local calculator'})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise UserError('Request must be a JSON object')
            self._json(HTTPStatus.OK, route(payload))
        except (UserError, ValueError, KeyError, FileNotFoundError, UnicodeDecodeError, ImportError) as exc:
            message = 'openpyxl is needed to read .xlsx files: pip install openpyxl' if isinstance(exc, ImportError) else str(exc)
            self._json(HTTPStatus.BAD_REQUEST, {'status': 'error', 'message': message})
        except Exception:
            traceback.print_exc()
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {'status': 'error', 'message': 'Internal error; see the server console'})


class SimmServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, allow_remote=False, quiet=False):
        super().__init__(address, Handler)
        self.allow_remote = allow_remote
        self.quiet = quiet
        self.reports = Reports()


def serve(host='127.0.0.1', port=8765, open_browser=False, allow_remote=False):
    if host not in ('127.0.0.1', 'localhost', '::1') and not allow_remote:
        raise ValueError('Refusing to listen on a non-local address: uploaded CRIF files hold counterparty data. Use --allow-remote to override.')
    server = SimmServer((host, port), allow_remote)
    shown = f'http://{host if host != "::1" else "[::1]"}:{server.server_address[1]}/'
    sys.stdout.write(f'ISDA SIMM calculator is running at {shown}\nOpen it in a browser on this machine. Press Ctrl+C to stop.\n')
    if allow_remote:
        sys.stdout.write('WARNING: remote access is on; anyone who can reach this port can upload CRIF files and read results.\n')
    sys.stdout.flush()
    if open_browser:
        webbrowser.open(shown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write('\nStopped.\n')
    finally:
        server.server_close()
    return 0


PAGE = r'''<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ISDA SIMM</title>
<style>
  :root { --navy: #1f3a5f; --teal: #0f7b74; --ink: #1f2328; --muted: #5b6672; --line: #d8dee4; --soft: #f4f7fa;
          --err-bg: #fdecea; --err-line: #c0392b; --warn-bg: #fff4e5; --warn-line: #d9822b; --ok-bg: #e8f5f2; color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #eef2f6; color: var(--ink); font: 14px/1.55 "Segoe UI", "Malgun Gothic", "Apple SD Gothic Neo", system-ui, sans-serif; }
  .wrap { max-width: 1200px; margin: 0 auto; padding: 24px 16px 48px; }
  header.top { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; background: var(--navy);
               color: #fff; border-radius: 12px; padding: 20px 24px; }
  header.top h1 { margin: 0; font-size: 21px; line-height: 1.3; }
  header.top .sub { opacity: .85; margin-top: 2px; }
  .lang { display: inline-flex; border: 1px solid rgba(255,255,255,.35); border-radius: 8px; overflow: hidden; }
  .lang button { background: transparent; color: #fff; border: 0; padding: 8px 12px; font: inherit; cursor: pointer; }
  .lang button[aria-pressed="true"] { background: rgba(255,255,255,.2); font-weight: 600; }
  .card { background: #fff; border: 1px solid var(--line); border-radius: 12px; padding: 20px 22px; margin-top: 16px; }
  .card h2 { margin: 0 0 12px; font-size: 16px; color: var(--navy); }
  .card h2 .n { display: inline-block; width: 24px; height: 24px; line-height: 24px; text-align: center; border-radius: 50%; background: var(--navy);
                color: #fff; font-size: 13px; margin-right: 8px; }
  .drop { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; min-height: 110px; padding: 16px;
          border: 2px dashed #9fb3c8; border-radius: 10px; background: var(--soft); cursor: pointer; text-align: center; color: var(--muted); }
  .drop:hover, .drop.over, .drop:focus-visible { border-color: var(--teal); background: #eaf6f4; outline: none; }
  .drop strong { color: var(--ink); font-size: 15px; }
  .fileinfo { margin-top: 10px; padding: 10px 14px; border-radius: 8px; background: var(--ok-bg); border: 1px solid #b6dcd5; display: none; overflow-wrap: anywhere; }
  .fileinfo.err { display: block; background: var(--err-bg); border-color: var(--err-line); }
  .fileinfo.show { display: block; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px 20px; }
  label.f { display: flex; flex-direction: column; gap: 4px; font-weight: 600; font-size: 13px; }
  label.f .hint { font-weight: 400; color: var(--muted); font-size: 12px; }
  input[type=text], input[type=date], select, textarea { font: inherit; padding: 9px 10px; border: 1px solid #b8c2cc; border-radius: 8px; background: #fff; color: var(--ink); width: 100%; }
  textarea { min-height: 64px; resize: vertical; }
  input:focus-visible, select:focus-visible, textarea:focus-visible, button:focus-visible, summary:focus-visible { outline: 2px solid var(--teal); outline-offset: 2px; }
  details { margin-top: 14px; border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px; background: #fafcfd; }
  details summary { cursor: pointer; font-weight: 600; color: var(--navy); }
  details .grid { margin-top: 12px; }
  .resolved { margin-top: 2px; color: var(--teal); font-weight: 600; font-size: 12px; min-height: 18px; }
  .row { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-top: 18px; }
  button.primary { background: var(--teal); color: #fff; border: 0; border-radius: 8px; padding: 12px 34px; font: inherit; font-size: 16px; font-weight: 700; cursor: pointer; }
  button.primary:hover:not(:disabled) { background: #0b645e; }
  button.primary:disabled { background: #9ab5b2; cursor: not-allowed; }
  button.ghost { background: #fff; color: var(--navy); border: 1px solid var(--navy); border-radius: 8px; padding: 9px 16px; font: inherit; font-weight: 600; cursor: pointer; }
  button.ghost:hover { background: var(--soft); }
  .status { color: var(--muted); }
  .spin { display: inline-block; width: 14px; height: 14px; border: 2px solid #b8c2cc; border-top-color: var(--teal); border-radius: 50%; animation: s .8s linear infinite; vertical-align: -2px; margin-right: 6px; }
  @keyframes s { to { transform: rotate(360deg); } }
  .banner { border-radius: 8px; padding: 12px 16px; margin-bottom: 12px; border: 1px solid; }
  .banner.ok { background: var(--ok-bg); border-color: #b6dcd5; }
  .banner.err { background: var(--err-bg); border-color: var(--err-line); }
  .banner.warn { background: var(--warn-bg); border-color: var(--warn-line); }
  .banner b { font-size: 15px; }
  .links { display: flex; flex-wrap: wrap; gap: 14px; margin: 8px 0 12px; }
  .links a { color: var(--navy); font-weight: 600; }
  iframe { width: 100%; height: 82vh; min-height: 520px; border: 1px solid var(--line); border-radius: 10px; background: #fff; }
  .tablewrap { overflow-x: auto; }
  table.issues { border-collapse: collapse; width: 100%; font-size: 13px; }
  table.issues th, table.issues td { border: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }
  table.issues th { background: var(--soft); }
  .note { color: var(--muted); font-size: 12px; margin-top: 16px; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div><h1 data-i18n="title"></h1><div class="sub" data-i18n="subtitle"></div></div>
    <div class="lang" role="group" aria-label="Language">
      <button type="button" data-lang="ko">한국어</button><button type="button" data-lang="en">English</button>
    </div>
  </header>

  <section class="card" aria-labelledby="h1">
    <h2 id="h1"><span class="n">1</span><span data-i18n="step_file"></span></h2>
    <label class="drop" id="drop" tabindex="0">
      <input type="file" id="file" accept=".csv,.tsv,.txt,.crif,.xlsx" hidden>
      <strong data-i18n="drop_title"></strong>
      <span data-i18n="drop_sub"></span>
    </label>
    <div class="fileinfo" id="fileinfo" role="status"></div>
    <div class="row" style="margin-top:10px"><button type="button" class="ghost" id="example" hidden data-i18n="example"></button></div>
  </section>

  <section class="card" aria-labelledby="h2">
    <h2 id="h2"><span class="n">2</span><span data-i18n="step_params"></span></h2>
    <div class="grid">
      <label class="f"><span data-i18n="f_date"></span><input type="date" id="valuation_date" required>
        <span class="hint" data-i18n="f_date_hint"></span></label>
      <label class="f"><span data-i18n="f_version"></span><select id="version"></select>
        <span class="resolved" id="resolved" aria-live="polite"></span></label>
      <label class="f"><span data-i18n="f_direction"></span>
        <select id="direction"><option value="collect" data-i18n="collect"></option><option value="post" data-i18n="post"></option></select></label>
      <label class="f"><span data-i18n="f_regulation"></span><input type="text" id="regulation" list="regs" autocomplete="off">
        <datalist id="regs"></datalist><span class="hint" data-i18n="f_regulation_hint"></span></label>
    </div>
    <label class="f" style="margin-top:14px"><span data-i18n="f_units"></span><textarea id="unit_source"></textarea>
      <span class="hint" data-i18n="f_units_hint"></span></label>
    <details>
      <summary data-i18n="scope_title"></summary>
      <div class="grid">
        <label class="f"><span data-i18n="f_entity"></span><input type="text" id="legal_entity" autocomplete="off"></label>
        <label class="f"><span data-i18n="f_csa"></span><input type="text" id="csa" autocomplete="off"></label>
        <label class="f"><span data-i18n="f_nettingset"></span><input type="text" id="netting_set" autocomplete="off"></label>
      </div>
      <p class="hint" style="margin:8px 0 0;color:var(--muted)" data-i18n="scope_hint"></p>
    </details>
    <details>
      <summary data-i18n="adv_title"></summary>
      <div class="grid">
        <label class="f"><span data-i18n="f_context"></span><input type="file" id="context_file" accept=".json,application/json">
          <span class="hint" data-i18n="f_context_hint"></span></label>
        <label class="f"><span data-i18n="f_nscol"></span><input type="text" id="netting_set_column" autocomplete="off">
          <span class="hint" data-i18n="f_nscol_hint"></span></label>
        <label class="f" style="flex-direction:row;align-items:center;gap:8px"><input type="checkbox" id="excel_serial_dates" style="width:auto">
          <span data-i18n="f_serial"></span></label>
      </div>
    </details>
    <div class="row">
      <button type="button" class="primary" id="run" disabled data-i18n="run"></button>
      <span class="status" id="status" role="status" aria-live="polite"></span>
    </div>
  </section>

  <section class="card" id="result" hidden aria-labelledby="h3">
    <h2 id="h3"><span class="n">3</span><span data-i18n="step_result"></span></h2>
    <div id="resultBody"></div>
  </section>

  <p class="note" data-i18n="note"></p>
</div>
<script>
(function () {
  'use strict';
  var I18N = {
    ko: {
      title: 'ISDA SIMM 산출', subtitle: 'CRIF 파일을 올리고 버전을 고른 뒤 [산출]을 누르세요 · USD · 이 컴퓨터 안에서만 계산합니다',
      step_file: 'CRIF 파일 올리기', drop_title: '여기를 눌러 파일을 고르거나 끌어다 놓으세요', drop_sub: 'CSV · TSV · CRIF · XLSX(시트 1개)',
      example: '예제 CRIF로 시험해 보기', step_params: '산출 조건', step_result: '산출 결과',
      f_date: '평가일 (COB)', f_date_hint: 'CRIF의 ValuationDate와 같아야 합니다. 파일을 올리면 자동으로 채워집니다.',
      f_version: 'SIMM 버전', f_direction: '방향', collect: '수취 (collect)', post: '교부 (post)',
      f_regulation: '규제 (Regulation)', f_regulation_hint: 'CRIF의 CollectRegulations/PostRegulations 값과 같아야 합니다.',
      f_units: '민감도 단위의 근거', f_units_hint: '이 CRIF의 민감도 단위(PV01, CS01, Vega 등)를 정의한 문서나 시스템 이름. 비워 두면 계산하지 않습니다.',
      scope_title: '거래 범위 기본값 (선택)', f_entity: '법인', f_csa: 'CSA', f_nettingset: 'Netting Set',
      scope_hint: 'CRIF에 해당 열이 없거나 비어 있을 때 쓰는 값입니다.',
      adv_title: '고급 옵션', f_context: 'context.json 직접 올리기', f_context_hint: '선택하면 위의 방향·규제·단위 근거·거래 범위 대신 이 파일을 씁니다.',
      f_nscol: 'Netting Set으로 쓸 CRIF 열', f_nscol_hint: '예: PortfolioId. CRIF에 NettingSet 열이 없을 때만 씁니다.',
      f_serial: 'ValuationDate가 엑셀 일련번호(숫자)입니다',
      run: '산출', note: 'SHADOW(참고용) 결과입니다. 독립 검증을 거치지 않은 엔진이므로 업무에 쓰기 전에 별도로 검증하세요. ISDA SIMM®은 ISDA의 등록상표이며 사용에는 ISDA 라이선스가 필요할 수 있습니다.',
      auto: '자동 (평가일 기준)', from_cob: '{d} COB부터', resolved: '→ 이 평가일에는 {v} 적용', pick_date: '평가일을 입력하면 적용 버전이 표시됩니다.',
      file_ok: '{n} · {size} · {rows}행 · 열 {cols}개', dates_found: '평가일: {d}', dates_multi: '평가일이 여러 개입니다: {d}. 한 번에 하나만 계산할 수 있습니다.',
      reading: '파일을 읽는 중…', calculating: '산출 중…', need_file: 'CRIF 파일을 먼저 올려 주세요.', need_date: '평가일을 입력해 주세요.',
      need_regulation: '규제를 입력해 주세요.', need_units: '민감도 단위의 근거를 입력해 주세요.',
      ok_title: '산출 완료', ok_line: 'SIMM {v} · 평가일 {d} · 전체 {t} USD · 거래상대방 {c}곳 · CRIF {used}/{read}행 사용',
      open_new: '새 탭에서 열기', save_html: 'HTML 파일로 저장', blocked_title: '검증에서 막혀 계산하지 않았습니다',
      blocked_line: '아래 항목을 CRIF나 조건에서 고친 뒤 다시 [산출]을 누르세요. (표시 {shown} / 전체 {total}건)',
      th_sev: '구분', th_code: '코드', th_row: '행', th_field: '열', th_msg: '내용', error_title: '오류',
      w_override: '직접 고른 버전이 이 평가일의 일정표 버전과 다릅니다.', w_beyond: '버전 일정표를 마지막으로 확인한 날 이후의 평가일입니다. 더 새로운 SIMM 버전이 있는지 확인하세요.',
      w_excluded: '규제 조건에 맞지 않아 계산에서 빠진 행이 있습니다.', net: '서버에 연결하지 못했습니다. `python -m simm serve`가 실행 중인지 확인하세요.'
    },
    en: {
      title: 'ISDA SIMM calculation', subtitle: 'Upload a CRIF file, choose the version and press Calculate · USD · everything is calculated on this computer',
      step_file: 'Upload the CRIF file', drop_title: 'Click to choose a file, or drop it here', drop_sub: 'CSV · TSV · CRIF · XLSX (single sheet)',
      example: 'Try the example CRIF', step_params: 'Calculation settings', step_result: 'Result',
      f_date: 'Valuation date (COB)', f_date_hint: 'Must equal the CRIF ValuationDate. Filled in automatically when you upload a file.',
      f_version: 'SIMM version', f_direction: 'Direction', collect: 'Collect', post: 'Post',
      f_regulation: 'Regulation', f_regulation_hint: 'Must match a value in the CRIF CollectRegulations / PostRegulations column.',
      f_units: 'Source of the sensitivity units', f_units_hint: 'The document or system that defines the sensitivity units (PV01, CS01, vega, ...) of this CRIF. Nothing is calculated while this is empty.',
      scope_title: 'Default scope (optional)', f_entity: 'Legal entity', f_csa: 'CSA', f_nettingset: 'Netting set',
      scope_hint: 'Used when the CRIF has no such column or the cell is empty.',
      adv_title: 'Advanced options', f_context: 'Upload your own context.json', f_context_hint: 'If chosen, this file replaces direction, regulation, unit source and default scope above.',
      f_nscol: 'CRIF column to use as netting set', f_nscol_hint: 'For example PortfolioId. Only when the CRIF has no NettingSet column.',
      f_serial: 'ValuationDate holds Excel serial numbers',
      run: 'Calculate', note: 'SHADOW, reference only. The engine has not been independently validated; validate results before relying on them. ISDA SIMM® is a registered trademark of ISDA and using it may require an ISDA licence.',
      auto: 'Automatic (by valuation date)', from_cob: 'from COB {d}', resolved: '→ {v} applies to this valuation date', pick_date: 'Enter the valuation date to see which version applies.',
      file_ok: '{n} · {size} · {rows} rows · {cols} columns', dates_found: 'Valuation date: {d}', dates_multi: 'Several valuation dates found: {d}. Only one can be calculated at a time.',
      reading: 'Reading the file…', calculating: 'Calculating…', need_file: 'Upload a CRIF file first.', need_date: 'Enter the valuation date.',
      need_regulation: 'Enter the regulation.', need_units: 'Enter the source of the sensitivity units.',
      ok_title: 'Calculation complete', ok_line: 'SIMM {v} · valuation {d} · total {t} USD · {c} counterparties · {used}/{read} CRIF rows used',
      open_new: 'Open in a new tab', save_html: 'Save as HTML file', blocked_title: 'Validation blocked the calculation',
      blocked_line: 'Fix the items below in the CRIF or the settings and press Calculate again. (showing {shown} of {total})',
      th_sev: 'Severity', th_code: 'Code', th_row: 'Row', th_field: 'Field', th_msg: 'Message', error_title: 'Error',
      w_override: 'The version you chose differs from the scheduled version for this valuation date.', w_beyond: 'The valuation date is after the last schedule check. Look for a newer SIMM version.',
      w_excluded: 'Some rows were excluded by the regulation filter.', net: 'Could not reach the server. Check that `python -m simm serve` is running.'
    }
  };
  var STORE = ['direction', 'regulation', 'legal_entity', 'csa', 'netting_set', 'unit_source', 'netting_set_column'];
  var $ = function (id) { return document.getElementById(id); };
  var state = { lang: String(navigator.language || '').toLowerCase().indexOf('ko') === 0 ? 'ko' : 'en', file: null, info: null, versions: [], busy: false };

  function t(key, vars) {
    var s = (I18N[state.lang][key] !== undefined ? I18N[state.lang][key] : key);
    Object.keys(vars || {}).forEach(function (k) { s = s.split('{' + k + '}').join(vars[k]); });
    return s;
  }
  function store(op, key, value) {
    try { return op === 'get' ? localStorage.getItem('simm.' + key) : localStorage.setItem('simm.' + key, value); } catch (e) { return null; }
  }
  function size(n) { return n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB'; }
  function el(tag, text, cls) { var e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; }
  function toB64(buf) {
    var bytes = new Uint8Array(buf), out = '', chunk = 0x8000;
    for (var i = 0; i < bytes.length; i += chunk) out += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    return btoa(out);
  }
  function readFile(file, asText) {
    return new Promise(function (resolve, reject) {
      var r = new FileReader();
      r.onload = function () { resolve(r.result); };
      r.onerror = function () { reject(new Error(String(r.error || 'read error'))); };
      asText ? r.readAsText(file) : r.readAsArrayBuffer(file);
    });
  }
  function api(path, body) {
    var opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    return fetch(path, opt).then(function (r) {
      return r.json().catch(function () { return { status: 'error', message: 'HTTP ' + r.status }; });
    }, function () { throw new Error(t('net')); });
  }

  function applyLang() {
    document.documentElement.lang = state.lang;
    document.querySelectorAll('[data-i18n]').forEach(function (n) { n.textContent = t(n.getAttribute('data-i18n')); });
    document.querySelectorAll('.lang button').forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-lang') === state.lang)); });
    document.title = t('title');
    renderVersions(); renderFileInfo(); updateResolved();
  }

  // ---- versions
  function renderVersions() {
    var sel = $('version'), keep = sel.value || 'auto';
    sel.textContent = '';
    var auto = el('option', t('auto')); auto.value = 'auto'; sel.appendChild(auto);
    state.versions.slice().reverse().forEach(function (v) {
      var o = el('option', v.version + '  (' + t('from_cob', { d: v.use_from_cob }) + ')'); o.value = v.version; sel.appendChild(o);
    });
    sel.value = keep;
  }
  function scheduled(date) {
    var hit = null;
    state.versions.forEach(function (v) { if (v.use_from_cob <= date) hit = v.version; });
    return hit;
  }
  function updateResolved() {
    var d = $('valuation_date').value, r = $('resolved');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(d)) { r.textContent = t('pick_date'); return; }
    var v = scheduled(d);
    r.textContent = v ? t('resolved', { v: v }) : '';
  }

  // ---- file
  function renderFileInfo() {
    var box = $('fileinfo');
    box.className = 'fileinfo';
    if (!state.file) { box.textContent = ''; return; }
    if (state.file.error) { box.className = 'fileinfo err'; box.textContent = state.file.error; return; }
    var i = state.info;
    box.className = 'fileinfo show';
    box.textContent = '';
    box.appendChild(el('div', i ? t('file_ok', { n: state.file.name, size: size(state.file.size), rows: i.rows, cols: i.columns }) : state.file.name + ' · ' + size(state.file.size)));
    if (i && i.valuation_dates.length) {
      box.appendChild(el('div', i.valuation_dates.length === 1 ? t('dates_found', { d: i.suggested_dates.join(', ') }) : t('dates_multi', { d: i.suggested_dates.join(', ') })));
    }
  }
  function setFile(name, b64, bytes) {
    state.file = { name: name, b64: b64, size: bytes };
    state.info = null;
    renderFileInfo();
    $('status').textContent = t('reading');
    return api('/api/inspect', { file_name: name, file_b64: b64 }).then(function (info) {
      $('status').textContent = '';
      if (info.status !== 'ok') { state.file.error = info.message || 'error'; renderFileInfo(); updateRun(); return; }
      state.info = info;
      if (info.suggested_dates.length === 1) { $('valuation_date').value = info.suggested_dates[0]; }
      $('excel_serial_dates').checked = !!info.excel_serial;
      var dl = $('regs'); dl.textContent = '';
      info.regulations.forEach(function (r) { var o = document.createElement('option'); o.value = r; dl.appendChild(o); });
      if (info.regulations.length === 1 && !$('regulation').value) { $('regulation').value = info.regulations[0]; }
      renderFileInfo(); updateResolved(); updateRun();
    }).catch(function (e) { $('status').textContent = ''; state.file.error = e.message; renderFileInfo(); updateRun(); });
  }
  function chooseFile(file) {
    if (!file) return;
    readFile(file, false).then(function (buf) { return setFile(file.name, toB64(buf), file.size); })
      .catch(function (e) { state.file = { name: file.name, size: file.size, error: e.message }; renderFileInfo(); updateRun(); });
  }
  function updateRun() { $('run').disabled = state.busy || !state.file || !!state.file.error; }

  // ---- calculate
  function showResult(node) { var b = $('resultBody'); b.textContent = ''; b.appendChild(node); $('result').hidden = false; $('result').scrollIntoView({ behavior: 'smooth', block: 'start' }); }
  function banner(kind, title, line) {
    var d = el('div', undefined, 'banner ' + kind); d.appendChild(el('b', title));
    if (line) { d.appendChild(document.createElement('br')); d.appendChild(el('span', line)); }
    return d;
  }
  function renderOk(r) {
    var box = document.createElement('div');
    box.appendChild(banner('ok', t('ok_title'), t('ok_line', { v: r.version, d: r.valuation_date, t: r.total_simm, c: r.counterparties, used: r.rows.used, read: r.rows.read })));
    var notes = [];
    if (r.warnings.indexOf('warn_override') >= 0) notes.push(t('w_override'));
    if (r.warnings.indexOf('warn_beyond') >= 0) notes.push(t('w_beyond'));
    if (r.warnings.indexOf('warn_excluded') >= 0) notes.push(t('w_excluded'));
    notes.forEach(function (n) { box.appendChild(banner('warn', n)); });
    var links = el('div', undefined, 'links');
    var a = el('a', t('open_new')); a.href = r.report_url; a.target = '_blank'; a.rel = 'noopener'; links.appendChild(a);
    var s = el('a', t('save_html')); s.href = r.report_url + '?download=1'; links.appendChild(s);
    box.appendChild(links);
    var f = document.createElement('iframe');
    f.src = r.report_url; f.title = t('step_result'); f.setAttribute('sandbox', 'allow-scripts allow-downloads allow-popups');
    box.appendChild(f);
    return box;
  }
  function renderBlocked(r) {
    var box = document.createElement('div');
    box.appendChild(banner('err', t('blocked_title'), t('blocked_line', { shown: r.issues.length, total: r.issue_total })));
    var wrap = el('div', undefined, 'tablewrap'), table = el('table', undefined, 'issues'), head = document.createElement('tr');
    ['th_sev', 'th_code', 'th_row', 'th_field', 'th_msg'].forEach(function (k) { head.appendChild(el('th', t(k))); });
    table.appendChild(head);
    r.issues.forEach(function (i) {
      var tr = document.createElement('tr');
      [i.severity, i.code, i.row_id || '', i.field || '', i.message].forEach(function (v) { tr.appendChild(el('td', v)); });
      table.appendChild(tr);
    });
    wrap.appendChild(table); box.appendChild(wrap);
    return box;
  }
  function run() {
    var uploaded = $('context_file').files[0];
    if (!state.file) return alertStatus(t('need_file'));
    if (!$('valuation_date').value) return alertStatus(t('need_date'));
    if (!uploaded && !$('regulation').value.trim()) return alertStatus(t('need_regulation'));
    if (!uploaded && !$('unit_source').value.trim()) return alertStatus(t('need_units'));
    STORE.forEach(function (k) { store('set', k, $(k).value); });
    state.busy = true; updateRun();
    $('status').textContent = ''; var sp = el('span', undefined, 'spin'); $('status').appendChild(sp); $('status').appendChild(document.createTextNode(t('calculating')));
    (uploaded ? readFile(uploaded, true) : Promise.resolve('')).then(function (contextText) {
      var body = { file_name: state.file.name, file_b64: state.file.b64, valuation_date: $('valuation_date').value, version: $('version').value,
                   context_json: contextText, excel_serial_dates: $('excel_serial_dates').checked };
      STORE.forEach(function (k) { body[k] = $(k).value; });
      return api('/api/calculate', body);
    }).then(function (r) {
      $('status').textContent = '';
      if (r.status === 'ok') showResult(renderOk(r));
      else if (r.status === 'blocked') showResult(renderBlocked(r));
      else showResult(banner('err', t('error_title'), r.message || 'error'));
    }).catch(function (e) { $('status').textContent = ''; showResult(banner('err', t('error_title'), e.message)); })
      .then(function () { state.busy = false; updateRun(); });
  }
  function alertStatus(msg) { $('status').textContent = msg; }

  // ---- wiring
  document.querySelectorAll('.lang button').forEach(function (b) { b.addEventListener('click', function () { state.lang = b.getAttribute('data-lang'); applyLang(); }); });
  var drop = $('drop');
  $('file').addEventListener('change', function () { chooseFile(this.files[0]); });
  drop.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('file').click(); } });
  ['dragenter', 'dragover'].forEach(function (n) { drop.addEventListener(n, function (e) { e.preventDefault(); drop.classList.add('over'); }); });
  ['dragleave', 'drop'].forEach(function (n) { drop.addEventListener(n, function (e) { e.preventDefault(); drop.classList.remove('over'); }); });
  drop.addEventListener('drop', function (e) { chooseFile(e.dataTransfer.files[0]); });
  $('valuation_date').addEventListener('input', updateResolved);
  $('run').addEventListener('click', run);
  $('example').addEventListener('click', function () {
    api('/api/example').then(function (ex) {
      if (ex.status === 'error') { alertStatus(ex.message); return; }
      $('valuation_date').value = ex.valuation_date;
      Object.keys(ex.fields).forEach(function (k) { $(k).value = ex.fields[k]; });
      setFile(ex.file_name, ex.file_b64, atob(ex.file_b64).length);
    });
  });

  STORE.forEach(function (k) { var v = store('get', k); if (v) $(k).value = v; });
  Promise.all([api('/api/versions'), api('/api/example')]).then(function (all) {
    state.versions = all[0].versions || [];
    if (all[1].status !== 'error') $('example').hidden = false;
    applyLang();
  }).catch(function (e) { applyLang(); alertStatus(e.message); });
  applyLang();
}());
</script>
</body>
</html>
'''
