"""Self-contained HTML report: counterparty SIMM summary, drill-down and Excel (.xlsx) download.

The report is one HTML file with inline CSS, JavaScript and data, so it opens offline in any browser and loads nothing
from the network. The Excel workbooks (Korean and English) are built here with the standard library and embedded in
the page; the download button only decodes the one that matches the page language. Amounts shown in the page use the
same rounding as the command-line summary.
"""
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr
import base64, html, io, json, re, zipfile

COMPONENTS = ('Delta', 'Vega', 'Curvature', 'BaseCorr')
XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

LABELS = {
    'en': {
        'title': 'ISDA SIMM results report', 'subtitle': 'Initial margin by counterparty · USD', 'download': 'Download Excel',
        'valuation_date': 'Valuation date', 'simm_version': 'SIMM version', 'use_from': 'used from COB {date}', 'currency': 'Currency',
        'status': 'Status', 'status_value': 'SHADOW · reference only', 'crif_file': 'CRIF file', 'crif_rows': 'CRIF rows',
        'rows_value': '{read} read · {used} used · {excluded} excluded',
        'warnings': 'Check before use',
        'warn_override': 'The SIMM version was set manually ({version}); the schedule gives {scheduled} for this date.',
        'warn_beyond': 'The valuation date is after {date}, when the version schedule was last checked. ISDA may have published a version newer than {version}.',
        'warn_excluded': '{n} CRIF rows were excluded by the regulation filter.',
        'kpi_total': 'Total SIMM', 'kpi_counterparties': 'Counterparties', 'kpi_netting_sets': 'Netting sets', 'kpi_largest': 'Largest counterparty',
        'search': 'Search counterparty', 'expand_all': 'Expand all', 'collapse_all': 'Collapse all',
        'hint': 'Click a counterparty to see its netting sets, product classes and risk classes.',
        'shown': '{shown} of {total} shown', 'no_match': 'No counterparty matches the search.',
        'col_rank': 'Rank', 'col_counterparty': 'Counterparty', 'col_netting_sets': 'Netting sets', 'col_simm': 'SIMM (USD)',
        'col_share': 'Share', 'col_addon': 'Add-on (USD)', 'total': 'Total',
        'netting_set': 'Netting set', 'csa': 'CSA', 'legal_entity': 'Legal entity', 'direction': 'Direction', 'regulation': 'Regulation',
        'ns_simm': 'Netting set SIMM', 'addon': 'Add-on', 'product_class': 'Product class', 'risk_class': 'Risk class',
        'multiplier': 'Multiplier', 'im_usd': 'IM (USD)', 'contribution': 'Contribution (USD)',
        'issues': 'Validation messages', 'transformations': 'Input transformations', 'limitations': 'Limitations',
        'notice': 'SHADOW, reference only. These results come from an engine that has not been independently validated; validate them '
                  'before relying on them. ISDA SIMM® is a registered trademark of ISDA, and using it for margin calculations may require '
                  'a licence from ISDA.',
        'confidential': 'This file contains the margins and counterparty identifiers of the input CRIF. Handle it with the same care as the CRIF.',
        'offline': 'Generated offline. The page loads nothing from the network.', 'parameter_hash': 'Parameter package SHA-256',
        'sheet_counterparties': 'Counterparties', 'sheet_netting_sets': 'Netting sets', 'sheet_risk_classes': 'Risk classes',
        'sheet_run_info': 'Run info', 'item': 'Item', 'value': 'Value', 'ns_simm_usd': 'Netting set SIMM (USD)',
        'product_class_simm': 'Product class SIMM (USD)', 'risk_class_im': 'Risk class IM (USD)', 'portfolio_total': 'Total SIMM (USD)',
        'use_from_cob': 'Version used from COB', 'mode': 'Mode', 'engine_status': 'Engine status', 'crif_sha256': 'CRIF SHA-256',
        'rows_read': 'CRIF rows read', 'rows_used': 'CRIF rows used', 'rows_excluded': 'CRIF rows excluded',
        'validation_status': 'Validation status', 'notice_label': 'Notice', 'limitation_text': {},
    },
    'ko': {
        'title': 'ISDA SIMM 산출 결과 보고서', 'subtitle': '거래상대방별 개시증거금 · USD', 'download': '엑셀 다운로드',
        'valuation_date': '평가일', 'simm_version': 'SIMM 버전', 'use_from': '{date} COB부터 적용', 'currency': '통화',
        'status': '상태', 'status_value': 'SHADOW · 참고용', 'crif_file': 'CRIF 파일', 'crif_rows': 'CRIF 행',
        'rows_value': '읽음 {read} · 사용 {used} · 제외 {excluded}',
        'warnings': '사용 전 확인',
        'warn_override': '직접 지정한 SIMM 버전({version})이 이 평가일의 일정표 버전({scheduled})과 다릅니다.',
        'warn_beyond': '평가일이 버전 일정표를 마지막으로 확인한 날({date}) 이후입니다. ISDA가 {version}보다 새 버전을 냈을 수 있으니 확인하세요.',
        'warn_excluded': '규제 조건에 맞지 않는 CRIF {n}개 행이 계산에서 빠졌습니다.',
        'kpi_total': '전체 SIMM', 'kpi_counterparties': '거래상대방', 'kpi_netting_sets': 'Netting Set', 'kpi_largest': '최대 거래상대방',
        'search': '거래상대방 검색', 'expand_all': '모두 펼치기', 'collapse_all': '모두 접기',
        'hint': '거래상대방을 누르면 Netting Set, 상품군, 위험군별 상세 내역이 펼쳐집니다.',
        'shown': '{total}곳 중 {shown}곳 표시', 'no_match': '검색과 일치하는 거래상대방이 없습니다.',
        'col_rank': '순위', 'col_counterparty': '거래상대방', 'col_netting_sets': 'Netting Set 수', 'col_simm': 'SIMM (USD)',
        'col_share': '비중', 'col_addon': 'Add-on (USD)', 'total': '합계',
        'netting_set': 'Netting Set', 'csa': 'CSA', 'legal_entity': '법인', 'direction': '방향', 'regulation': '규제',
        'ns_simm': 'Netting Set SIMM', 'addon': 'Add-on', 'product_class': '상품군', 'risk_class': '위험군',
        'multiplier': 'Multiplier', 'im_usd': 'IM (USD)', 'contribution': '기여분 (USD)',
        'issues': '검증 메시지', 'transformations': '입력 변환', 'limitations': '한계',
        'notice': 'SHADOW(참고용) 결과입니다. 독립 검증을 거치지 않은 엔진의 결과이므로 업무에 쓰기 전에 별도로 검증하세요. '
                  'ISDA SIMM®은 ISDA의 등록상표이며, 증거금 계산에 쓰려면 ISDA 라이선스가 필요할 수 있습니다.',
        'confidential': '이 파일에는 입력 CRIF의 증거금 금액과 거래상대방 식별자가 들어 있습니다. CRIF와 같은 수준으로 관리하세요.',
        'offline': '오프라인으로 만든 보고서입니다. 이 페이지는 인터넷에서 아무것도 불러오지 않습니다.',
        'parameter_hash': 'Parameter 패키지 SHA-256',
        'sheet_counterparties': '거래상대방', 'sheet_netting_sets': 'Netting Set', 'sheet_risk_classes': '위험군 상세',
        'sheet_run_info': '실행 정보', 'item': '항목', 'value': '값', 'ns_simm_usd': 'Netting Set SIMM (USD)',
        'product_class_simm': '상품군 SIMM (USD)', 'risk_class_im': '위험군 IM (USD)', 'portfolio_total': '전체 SIMM (USD)',
        'use_from_cob': '버전 적용 시작(COB)', 'mode': '모드', 'engine_status': '엔진 상태', 'crif_sha256': 'CRIF SHA-256',
        'rows_read': 'CRIF 읽은 행', 'rows_used': 'CRIF 사용 행', 'rows_excluded': 'CRIF 제외 행', 'validation_status': '검증 상태',
        'notice_label': '유의사항',
        'limitation_text': {
            'Latest RDS and official golden tests pending': '최신 Risk Data Standards와 ISDA 공식 테스트로는 아직 검증하지 않았습니다.',
            'Checked against independent parameter transcriptions and an independent re-implementation on synthetic portfolios only':
                'Parameter는 독립적으로 옮겨 적은 값과, 수식은 별도로 구현한 계산 코드와 가상 포트폴리오로만 대조했습니다.',
            'USD only: no FX conversion or reporting-currency output': 'USD만 지원합니다. 환율 환산이나 다른 보고 통화 출력은 없습니다.',
            'Not a substitute for an ISDA SIMM licence or for independent model validation':
                'ISDA SIMM 라이선스나 독립적인 모델 검증을 대신하지 않습니다.',
        },
    },
}


def fmt(x):
    """Amount with thousands separators and two decimals (half-even), as in the command-line summary."""
    return f'{Decimal(str(x)):,.2f}'


def _d(x):
    return Decimal(str(x))


def _ordered(result):
    """Counterparties by SIMM, largest first; ties by name."""
    return sorted(result['counterparties'], key=lambda c: (-_d(c['total_simm']), c['counterparty']))


def _warnings(meta):
    sel = meta.get('version_selection', {})
    out = []
    if sel.get('override') and sel.get('scheduled_version') != sel.get('version'):
        out.append({'code': 'warn_override', 'version': sel['version'], 'scheduled': sel.get('scheduled_version', '')})
    if sel.get('beyond_verified_schedule'):
        out.append({'code': 'warn_beyond', 'date': sel.get('schedule_verified_through', ''), 'version': sel['version']})
    if meta.get('rows_excluded'):
        out.append({'code': 'warn_excluded', 'n': meta['rows_excluded']})
    return out


def report_data(result, meta):
    """Display-ready data of the report page (amounts pre-formatted, counterparties ranked by SIMM)."""
    total = _d(result['total_simm'])
    counterparties = []
    for rank, cp in enumerate(_ordered(result), 1):
        cp_total = _d(cp['total_simm'])
        share = cp_total / total if total else Decimal(0)
        addon = sum((_d(ns['addon']) for ns in cp['netting_sets']), Decimal(0))
        counterparties.append({
            'rank': rank, 'name': cp['counterparty'], 'n_ns': len(cp['netting_sets']),
            'simm': float(cp_total), 'simm_txt': fmt(cp_total), 'share': float(share), 'share_txt': f'{share * 100:.1f}%',
            'addon': float(addon), 'addon_txt': fmt(addon),
            'netting_sets': [{
                'netting_set': ns['scope']['netting_set'], 'csa': ns['scope']['csa'], 'legal_entity': ns['scope']['legal_entity'],
                'direction': ns['scope']['direction'], 'regulation': ns['scope']['regulation'],
                'simm_txt': fmt(ns['total_simm']), 'addon_txt': fmt(ns['addon']),
                'products': [{
                    'product_class': p['product_class'], 'margin_txt': fmt(p['margin']), 'multiplier': str(p['multiplier']),
                    'risk_classes': [{
                        'risk_class': rc['risk_class'], 'margin_txt': fmt(rc['margin']), 'contribution_txt': fmt(rc.get('contribution', 0)),
                        'components': {c: fmt(rc['components'][c]) for c in COMPONENTS if c in rc['components']},
                    } for rc in p['risk_classes']],
                } for p in ns['products']],
            } for ns in cp['netting_sets']],
        })
    sel = meta.get('version_selection', {})
    addon_total = sum((_d(ns['addon']) for cp in result['counterparties'] for ns in cp['netting_sets']), Decimal(0))
    return {
        'meta': {
            'valuation_date': meta['valuation_date'], 'simm_version': result['simm_version'], 'use_from_cob': sel.get('use_from_cob', ''),
            'crif_file': Path(meta['crif_file']).name, 'crif_sha256': meta['crif_sha256'], 'parameter_hash': result['parameter_hash'],
            'rows_read': meta['rows_read'], 'rows_used': meta['rows_used'], 'rows_excluded': meta['rows_excluded'],
            'validation_status': meta['validation_status'], 'issue_counts': meta['issue_counts'],
            'input_transformations': meta['input_transformations'], 'warnings': _warnings(meta),
            'mode': result['mode'], 'status': result['status'], 'limitations': result['limitations'],
        },
        'totals': {
            'simm_txt': fmt(total), 'counterparties': len(counterparties), 'netting_sets': sum(c['n_ns'] for c in counterparties),
            'share_txt': '100.0%' if total else '0.0%', 'addon_txt': fmt(addon_total),
        },
        'counterparties': counterparties,
        'labels': LABELS,
        'xlsx_name': f"SIMM_{meta['valuation_date']}_{result['simm_version']}.xlsx",
        'xlsx_mime': XLSX_MIME,
    }


# ---------------------------------------------------------------------------------------------------------------------
# Excel workbook (Office Open XML, written with zipfile; inline strings, no shared strings, no external libraries)

_XML_INVALID = re.compile('[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]')
_STYLE = {'text': 0, 'money': 1, 'pct': 2, 'head': 3, 'int': 4, 'bold': 5, 'bold_money': 6, 'bold_pct': 7, 'bold_int': 8,
          'wrap': 9, 'num': 0}
_NUMERIC = {'money', 'pct', 'int', 'bold_money', 'bold_pct', 'bold_int', 'num'}
_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_PKG_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
_STYLES = (
    f'<styleSheet xmlns="{_MAIN}">'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="0.0%"/></numFmts>'
    '<fonts count="3">'
    '<font><sz val="11"/><color rgb="FF000000"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="11"/><color rgb="FF000000"/><name val="Calibri"/><family val="2"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF1F3A5F"/><bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="10">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    '<xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="4" fontId="2" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>'
    '<xf numFmtId="164" fontId="2" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>'
    '<xf numFmtId="3" fontId="2" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>')


def _col(n):
    """1 -> A, 27 -> AA."""
    s = ''
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref, value, kind):
    if value is None or value == '':
        return ''
    style = _STYLE[kind]
    if kind in _NUMERIC:
        number = str(value) if isinstance(value, int) else repr(float(_d(value)))
        return f'<c r="{ref}" s="{style}"><v>{number}</v></c>'
    text = escape(_XML_INVALID.sub('', str(value)))
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet_xml(sheet):
    view = ('<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>') if sheet.get('freeze') else ''
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n', f'<worksheet xmlns="{_MAIN}">',
             f'<sheetViews><sheetView workbookViewId="0">{view}</sheetView></sheetViews>', '<sheetFormatPr defaultRowHeight="15"/>',
             '<cols>' + ''.join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i, w in enumerate(sheet['widths'], 1)) + '</cols>',
             '<sheetData>']
    for r, row in enumerate(sheet['rows'], 1):
        cells = ''.join(_cell(f'{_col(c)}{r}', value, kind) for c, (value, kind) in enumerate(row, 1))
        if cells:
            parts.append(f'<row r="{r}">{cells}</row>')
    parts.append('</sheetData>')
    if sheet.get('filter_rows'):
        parts.append(f'<autoFilter ref="A1:{_col(len(sheet["widths"]))}{sheet["filter_rows"]}"/>')
    parts.append('</worksheet>')
    return ''.join(parts)


def _workbook_parts(sheets):
    names = ''.join(f'<sheet name={quoteattr(s["name"])} sheetId="{i}" r:id="rId{i}"/>' for i, s in enumerate(sheets, 1))
    filters = ''.join(
        f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
        f'{escape(chr(39) + s["name"].replace(chr(39), chr(39) * 2) + chr(39))}!$A$1:${_col(len(s["widths"]))}${s["filter_rows"]}</definedName>'
        for i, s in enumerate(sheets) if s.get('filter_rows'))
    workbook = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook xmlns="{_MAIN}" xmlns:r="{_REL}">'
                f'<bookViews><workbookView activeTab="0"/></bookViews><sheets>{names}</sheets>'
                + (f'<definedNames>{filters}</definedNames>' if filters else '') + '</workbook>')
    rels = ''.join(f'<Relationship Id="rId{i}" Type="{_REL}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1))
    rels += f'<Relationship Id="rId{len(sheets) + 1}" Type="{_REL}/styles" Target="styles.xml"/>'
    types = ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets) + 1))
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    return [
        ('[Content_Types].xml', head + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
         '<Default Extension="xml" ContentType="application/xml"/>'
         '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
         '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
         + types + '</Types>'),
        ('_rels/.rels', head + f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>'),
        ('xl/workbook.xml', workbook),
        ('xl/_rels/workbook.xml.rels', head + f'<Relationships xmlns="{_PKG_REL}">{rels}</Relationships>'),
        ('xl/styles.xml', head + _STYLES),
    ] + [(f'xl/worksheets/sheet{i}.xml', _sheet_xml(s)) for i, s in enumerate(sheets, 1)]


def _zip(parts):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))  # fixed timestamp: same input, same bytes
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            archive.writestr(info, text.encode('utf-8'))
    return buf.getvalue()


def _sheets(result, meta, data, lang):
    L = LABELS[lang]
    ordered = _ordered(result)
    total = _d(result['total_simm'])
    head = lambda *labels: [(L.get(k, k), 'head') for k in labels]

    cp_rows = [head('col_rank', 'col_counterparty', 'col_netting_sets', 'col_simm', 'col_share', 'col_addon')]
    for rank, cp in enumerate(ordered, 1):
        cp_total = _d(cp['total_simm'])
        addon = sum((_d(ns['addon']) for ns in cp['netting_sets']), Decimal(0))
        cp_rows.append([(rank, 'int'), (cp['counterparty'], 'text'), (len(cp['netting_sets']), 'int'), (cp_total, 'money'),
                        (cp_total / total if total else Decimal(0), 'pct'), (addon, 'money')])
    n_cp = len(cp_rows) - 1
    addon_total = sum((_d(ns['addon']) for cp in ordered for ns in cp['netting_sets']), Decimal(0))
    cp_rows += [[], [('', 'text'), (L['total'], 'bold'), (data['totals']['netting_sets'], 'bold_int'), (total, 'bold_money'),
                     (1 if total else 0, 'bold_pct'), (addon_total, 'bold_money')]]

    ns_rows = [head('col_counterparty', 'netting_set', 'csa', 'legal_entity', 'direction', 'regulation', 'ns_simm_usd', 'addon_usd')]
    rc_rows = [head('col_counterparty', 'netting_set', 'csa', 'product_class', 'product_class_simm', 'multiplier', 'risk_class',
                    'Delta', 'Vega', 'Curvature', 'BaseCorr', 'risk_class_im', 'contribution')]
    for cp in ordered:
        for ns in cp['netting_sets']:
            s = ns['scope']
            ns_rows.append([(cp['counterparty'], 'text'), (s['netting_set'], 'text'), (s['csa'], 'text'), (s['legal_entity'], 'text'),
                            (s['direction'], 'text'), (s['regulation'], 'text'), (ns['total_simm'], 'money'), (ns['addon'], 'money')])
            for p in ns['products']:
                for rc in p['risk_classes']:
                    comps = [(rc['components'].get(c, ''), 'money') for c in COMPONENTS]
                    rc_rows.append([(cp['counterparty'], 'text'), (s['netting_set'], 'text'), (s['csa'], 'text'), (p['product_class'], 'text'),
                                    (p['margin'], 'money'), (p['multiplier'], 'num'), (rc['risk_class'], 'text')] + comps
                                   + [(rc['margin'], 'money'), (rc.get('contribution', ''), 'money')])

    m = data['meta']
    info = [head('item', 'value'),
            [(L['valuation_date'], 'text'), (m['valuation_date'], 'text')],
            [(L['simm_version'], 'text'), (m['simm_version'], 'text')],
            [(L['use_from_cob'], 'text'), (m['use_from_cob'], 'text')],
            [(L['currency'], 'text'), ('USD', 'text')],
            [(L['mode'], 'text'), (m['mode'], 'text')],
            [(L['engine_status'], 'text'), (m['status'], 'text')],
            [(L['portfolio_total'], 'text'), (total, 'money')],
            [(L['kpi_counterparties'], 'text'), (n_cp, 'int')],
            [(L['kpi_netting_sets'], 'text'), (data['totals']['netting_sets'], 'int')],
            [(L['crif_file'], 'text'), (m['crif_file'], 'text')],
            [(L['crif_sha256'], 'text'), (m['crif_sha256'], 'text')],
            [(L['parameter_hash'], 'text'), (m['parameter_hash'], 'text')],
            [(L['rows_read'], 'text'), (m['rows_read'], 'int')],
            [(L['rows_used'], 'text'), (m['rows_used'], 'int')],
            [(L['rows_excluded'], 'text'), (m['rows_excluded'], 'int')],
            [(L['validation_status'], 'text'), (m['validation_status'], 'text')],
            [(L['issues'], 'text'), ('; '.join(f'{k} × {v}' for k, v in m['issue_counts'].items()), 'wrap')]]
    info += [[(L['warnings'], 'text'), (L[w['code']].format(**w), 'wrap')] for w in m['warnings']]
    info += [[(L['transformations'], 'text'), (x, 'wrap')] for x in m['input_transformations']]
    info += [[(L['limitations'], 'text'), (L['limitation_text'].get(x, x), 'wrap')] for x in m['limitations']]
    info += [[(L['notice_label'], 'text'), (L['notice'], 'wrap')], [('', 'text'), (L['confidential'], 'wrap')]]

    return [
        {'name': L['sheet_counterparties'], 'rows': cp_rows, 'widths': [8, 34, 14, 20, 10, 18], 'freeze': True, 'filter_rows': n_cp + 1},
        {'name': L['sheet_netting_sets'], 'rows': ns_rows, 'widths': [30, 24, 16, 18, 11, 16, 24, 16], 'freeze': True,
         'filter_rows': len(ns_rows)},
        {'name': L['sheet_risk_classes'], 'rows': rc_rows, 'widths': [30, 22, 14, 16, 24, 11, 22, 16, 16, 16, 16, 20, 20],
         'freeze': True, 'filter_rows': len(rc_rows)},
        {'name': L['sheet_run_info'], 'rows': info, 'widths': [30, 110]},
    ]


def build_xlsx(result, meta, lang='en', data=None):
    """Excel workbook (bytes): counterparties, netting sets, risk classes and run information."""
    return _zip(_workbook_parts(_sheets(result, meta, data or report_data(result, meta), lang)))


# ---------------------------------------------------------------------------------------------------------------------
# HTML page

def _script_json(obj):
    """JSON safe inside <script>: no '<', '>' or '&' can close the element or start markup."""
    text = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
    return text.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def render_report(result, meta):
    """The report page as one self-contained HTML string."""
    data = report_data(result, meta)
    xlsx = {lang: base64.b64encode(build_xlsx(result, meta, lang, data)).decode('ascii') for lang in ('ko', 'en')}
    page = _PAGE.replace('__TITLE__', html.escape(f"ISDA SIMM · {meta['valuation_date']}"))
    page = page.replace('__XLSX__', _script_json(xlsx))
    return page.replace('__DATA__', _script_json(data))


def write_report(path, result, meta):
    Path(path).write_text(render_report(result, meta), encoding='utf-8')


_PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { --navy: #1f3a5f; --navy-2: #2d5281; --teal: #0f7b74; --ink: #1f2328; --muted: #5b6672; --line: #d8dee4;
          --soft: #f4f7fa; --soft-2: #eaf1f8; --warn-bg: #fff4e5; --warn-line: #d9822b; color-scheme: light; }
  * { box-sizing: border-box; }
  html { background: #eef2f6; }
  body { margin: 0; color: var(--ink); background: #eef2f6; font: 14px/1.5 "Segoe UI", "Malgun Gothic", "Apple SD Gothic Neo", system-ui, sans-serif; }
  .wrap { max-width: 1200px; margin: 0 auto; padding: 24px 20px 40px; }
  header.top { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-end; justify-content: space-between;
               background: var(--navy); color: #fff; border-radius: 12px; padding: 22px 24px; }
  header.top h1 { margin: 0; font-size: 22px; line-height: 1.3; }
  header.top .sub { opacity: .85; margin-top: 4px; }
  .actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .lang { display: inline-flex; border: 1px solid rgba(255,255,255,.35); border-radius: 8px; overflow: hidden; }
  .lang button { background: transparent; color: #fff; border: 0; padding: 8px 12px; font: inherit; cursor: pointer; }
  .lang button[aria-pressed="true"] { background: rgba(255,255,255,.2); font-weight: 600; }
  .download { display: inline-flex; gap: 8px; align-items: center; background: #fff; color: var(--navy); border: 0; border-radius: 8px;
              padding: 9px 16px; font: inherit; font-weight: 600; cursor: pointer; }
  .download:hover { background: #e6f2ef; }
  .download svg { width: 18px; height: 18px; }
  button:focus-visible, input:focus-visible, tr.cp:focus-visible { outline: 2px solid var(--teal); outline-offset: 2px; }
  dl.meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px 24px; margin: 16px 0 0;
            background: #fff; border: 1px solid var(--line); border-radius: 12px; padding: 16px 20px; }
  @media (min-width: 900px) { dl.meta { grid-template-columns: repeat(3, 1fr); } }
  dl.meta dt { color: var(--muted); font-size: 12px; }
  dl.meta dd { margin: 2px 0 0; font-weight: 600; overflow-wrap: anywhere; }
  .warnbox { background: var(--warn-bg); border-left: 4px solid var(--warn-line); border-radius: 8px; padding: 10px 16px; margin-top: 16px; }
  .warnbox ul { margin: 4px 0 0; padding-left: 20px; }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin-top: 16px; }
  .kpi { background: #fff; border: 1px solid var(--line); border-radius: 12px; padding: 14px 18px; min-width: 0; }
  .kpi .lbl { color: var(--muted); font-size: 12.5px; }
  .kpi .val { font-size: 24px; font-weight: 700; color: var(--navy); margin-top: 2px; font-variant-numeric: tabular-nums;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .kpi .val small { font-size: 13px; color: var(--muted); font-weight: 600; margin-left: 6px; }
  .kpi .note { color: var(--muted); font-size: 12.5px; margin-top: 2px; font-variant-numeric: tabular-nums; }
  .card { background: #fff; border: 1px solid var(--line); border-radius: 12px; margin-top: 16px; overflow: hidden; }
  .toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; padding: 14px 16px; border-bottom: 1px solid var(--line); }
  .toolbar input { flex: 1 1 220px; max-width: 320px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; font: inherit; }
  .btn { background: var(--soft); color: var(--ink); border: 1px solid var(--line); border-radius: 8px; padding: 7px 12px; font: inherit; cursor: pointer; }
  .btn:hover { background: var(--soft-2); }
  .count { color: var(--teal); font-weight: 600; }
  .hint { color: var(--muted); font-size: 12.5px; margin-left: auto; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 9px 12px; border-bottom: 1px solid #edf0f3; text-align: left; white-space: nowrap; }
  thead th { background: var(--soft); color: var(--muted); font-size: 12.5px; font-weight: 600; }
  th button { all: unset; cursor: pointer; }
  th button::after { content: attr(data-arrow); margin-left: 4px; font-size: 10px; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .strong { font-weight: 600; }
  tr.cp { cursor: pointer; }
  tr.cp:hover td { background: #f7fafc; }
  tr.cp[aria-expanded="true"] td { background: #eaf3f8; }
  .caret { display: inline-block; width: 16px; color: var(--teal); transition: transform .15s; }
  tr.cp[aria-expanded="true"] .caret { transform: rotate(90deg); }
  td.name { font-weight: 600; color: var(--navy); white-space: normal; overflow-wrap: anywhere; min-width: 160px; }
  .share { display: flex; align-items: center; gap: 8px; justify-content: flex-end; }
  .bar { width: 120px; height: 8px; background: #e3eaf0; border-radius: 4px; overflow: hidden; }
  .bar i { display: block; height: 100%; background: var(--teal); }
  .pct { min-width: 48px; }
  tfoot td { font-weight: 700; background: var(--soft); border-top: 2px solid var(--line); }
  tr.detail > td { background: #f8fbfd; padding: 4px 16px 16px 40px; white-space: normal; }
  .detail-box { width: 0; min-width: 100%; }  /* the drill-down scrolls on its own instead of widening the summary table */
  tr.empty td { color: var(--muted); text-align: center; padding: 24px; }
  .ns { border: 1px solid var(--line); border-radius: 10px; background: #fff; margin-top: 12px; overflow: hidden; }
  .ns-head { display: flex; flex-wrap: wrap; gap: 6px 18px; align-items: center; padding: 10px 14px; background: var(--soft-2); }
  .ns-title { display: flex; gap: 8px; align-items: center; }
  .tag { background: var(--navy); color: #fff; border-radius: 6px; padding: 1px 8px; font-size: 12px; }
  .ns-facts { display: flex; flex-wrap: wrap; gap: 4px 14px; color: var(--ink); font-size: 12.5px; }
  .ns-facts em { font-style: normal; color: var(--muted); margin-right: 4px; }
  .ns-amount { margin-left: auto; display: flex; gap: 10px; align-items: baseline; font-variant-numeric: tabular-nums; }
  .ns-amount .lbl, .ns-amount .addon { color: var(--muted); font-size: 12.5px; }
  table.rc th, table.rc td { padding: 6px 10px; font-size: 13px; }
  tr.pc td { font-weight: 700; background: #f7f9fb; }
  .mult { margin-left: 10px; font-weight: 600; color: var(--teal); font-size: 12px; }
  footer { color: var(--muted); font-size: 12.5px; margin-top: 20px; }
  footer h3 { font-size: 13px; color: var(--ink); margin: 14px 0 4px; }
  footer ul { margin: 0; padding-left: 20px; }
  footer .notice { color: var(--ink); }
  @media (max-width: 760px) {
    .wrap { padding: 12px 10px 28px; }
    header.top { padding: 16px; }
    tr.detail > td { padding: 4px 10px 12px; }
    .hint { margin-left: 0; }
    .bar { width: 60px; }
    td.name { min-width: 110px; }
    th, td { padding: 8px 8px; }
  }
  @media print {
    html, body { background: #fff; }
    .actions, .toolbar { display: none; }
    header.top { background: #fff; color: var(--ink); border: 1px solid var(--line); }
    .card, .kpi, dl.meta { break-inside: avoid; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1 data-t="title">ISDA SIMM results report</h1>
      <div class="sub" data-t="subtitle"></div>
    </div>
    <div class="actions">
      <div class="lang" role="group" aria-label="Language / 언어">
        <button type="button" data-lang="ko">한국어</button><button type="button" data-lang="en">English</button>
      </div>
      <button type="button" class="download" id="download">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/></svg>
        <span data-t="download"></span>
      </button>
    </div>
  </header>
  <dl class="meta" id="meta"></dl>
  <div class="warnbox" id="warnings" hidden></div>
  <div class="kpis" id="kpis"></div>
  <section class="card">
    <div class="toolbar">
      <input type="search" id="search" data-tp="search" autocomplete="off">
      <button type="button" class="btn" id="expand" data-t="expand_all"></button>
      <button type="button" class="btn" id="collapse" data-t="collapse_all"></button>
      <span class="count" id="count"></span>
      <span class="hint" data-t="hint"></span>
    </div>
    <div class="table-wrap">
      <table id="cp-table">
        <thead><tr>
          <th class="num"><button type="button" data-sort="rank" data-t="col_rank"></button></th>
          <th><button type="button" data-sort="name" data-t="col_counterparty"></button></th>
          <th class="num"><button type="button" data-sort="n_ns" data-t="col_netting_sets"></button></th>
          <th class="num"><button type="button" data-sort="simm" data-t="col_simm"></button></th>
          <th class="num"><button type="button" data-sort="share" data-t="col_share"></button></th>
          <th class="num"><button type="button" data-sort="addon" data-t="col_addon"></button></th>
        </tr></thead>
        <tbody id="cp-body"></tbody>
        <tfoot id="cp-foot"></tfoot>
      </table>
    </div>
  </section>
  <footer id="footer"></footer>
  <noscript><p>This report needs JavaScript. / 이 보고서를 보려면 JavaScript가 필요합니다.</p></noscript>
</div>
<script type="application/json" id="simm-data">__DATA__</script>
<script type="application/json" id="simm-xlsx">__XLSX__</script>
<script>
(function () {
  'use strict';
  var DATA = JSON.parse(document.getElementById('simm-data').textContent);
  var XLSX = JSON.parse(document.getElementById('simm-xlsx').textContent);
  var L = DATA.labels;
  var COMPONENTS = ['Delta', 'Vega', 'Curvature', 'BaseCorr'];
  var FIRST_DIR = { rank: 1, name: 1, n_ns: -1, simm: -1, share: -1, addon: -1 };
  var state = { lang: pickLang(), key: 'rank', dir: 1, query: '', open: Object.create(null) };
  var maxShare = DATA.counterparties.reduce(function (m, c) { return Math.max(m, c.share); }, 0) || 1;

  function pickLang() {
    var m = /(?:^|[#&?])lang=(ko|en)(?:&|$)/.exec(location.hash + '&' + location.search);
    if (m) return m[1];
    try {
      var saved = window.localStorage.getItem('simm-report-lang');
      if (saved === 'ko' || saved === 'en') return saved;
    } catch (e) { /* storage not available: fall back to the browser language */ }
    return String(navigator.language || '').toLowerCase().indexOf('ko') === 0 ? 'ko' : 'en';
  }

  function t(key, vars) {
    var s = L[state.lang][key];
    if (s === undefined) s = L.en[key];
    if (s === undefined) return key;
    return vars ? s.replace(/\{(\w+)\}/g, function (all, k) { return vars[k] === undefined ? all : String(vars[k]); }) : s;
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function each(selector, fn) { Array.prototype.forEach.call(document.querySelectorAll(selector), fn); }

  function renderStatic() {
    document.documentElement.lang = state.lang;
    document.title = t('title') + ' · ' + DATA.meta.valuation_date;
    each('[data-t]', function (n) { n.textContent = t(n.getAttribute('data-t')); });
    each('[data-tp]', function (n) { n.placeholder = t(n.getAttribute('data-tp')); n.setAttribute('aria-label', n.placeholder); });
    each('[data-lang]', function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-lang') === state.lang)); });
  }

  function renderMeta() {
    var m = DATA.meta, box = clear(document.getElementById('meta'));
    function item(label, value, title) {
      var d = el('div'), dd = el('dd', null, value);
      if (title) dd.title = title;
      d.appendChild(el('dt', null, label));
      d.appendChild(dd);
      box.appendChild(d);
    }
    item(t('valuation_date'), m.valuation_date);
    item(t('simm_version'), m.simm_version + (m.use_from_cob ? ' · ' + t('use_from', { date: m.use_from_cob }) : ''));
    item(t('currency'), 'USD');
    item(t('status'), t('status_value'));
    item(t('crif_file'), m.crif_file + ' · SHA-256 ' + m.crif_sha256.slice(0, 12) + '…', m.crif_sha256);
    item(t('crif_rows'), t('rows_value', { read: m.rows_read, used: m.rows_used, excluded: m.rows_excluded }));
  }

  function renderWarnings() {
    var box = clear(document.getElementById('warnings')), list = DATA.meta.warnings, ul = el('ul');
    box.hidden = !list.length;
    if (!list.length) return;
    box.appendChild(el('strong', null, t('warnings')));
    list.forEach(function (w) { ul.appendChild(el('li', null, t(w.code, w))); });
    box.appendChild(ul);
  }

  function renderKpis() {
    var box = clear(document.getElementById('kpis')), top = DATA.counterparties[0];
    function kpi(label, value, unit, note) {
      var d = el('div', 'kpi'), v = el('div', 'val', value);
      if (unit) v.appendChild(el('small', null, unit));
      d.appendChild(el('div', 'lbl', label));
      d.appendChild(v);
      if (note) d.appendChild(el('div', 'note', note));
      box.appendChild(d);
    }
    kpi(t('kpi_total'), DATA.totals.simm_txt, 'USD');
    kpi(t('kpi_counterparties'), String(DATA.totals.counterparties));
    kpi(t('kpi_netting_sets'), String(DATA.totals.netting_sets));
    if (top) kpi(t('kpi_largest'), top.name, null, top.simm_txt + ' USD · ' + top.share_txt);
  }

  function visible() {
    var q = state.query.trim().toLowerCase(), k = state.key, d = state.dir;
    return DATA.counterparties.filter(function (c) { return !q || c.name.toLowerCase().indexOf(q) !== -1; })
      .sort(function (a, b) {
        var c = typeof a[k] === 'string' ? a[k].localeCompare(b[k]) : a[k] - b[k];
        return c * d || a.rank - b.rank;
      });
  }

  function detail(c) {
    var box = el('div', 'detail-box');
    c.netting_sets.forEach(function (ns) {
      var card = el('div', 'ns'), head = el('div', 'ns-head'), title = el('div', 'ns-title'), facts = el('div', 'ns-facts');
      var amount = el('div', 'ns-amount'), table = el('table', 'rc'), thead = el('thead'), hr = el('tr'), body = el('tbody');
      var tw = el('div', 'table-wrap');
      title.appendChild(el('span', 'tag', t('netting_set')));
      title.appendChild(el('strong', null, ns.netting_set));
      [['csa', ns.csa], ['legal_entity', ns.legal_entity], ['direction', ns.direction], ['regulation', ns.regulation]].forEach(function (f) {
        var s = el('span');
        s.appendChild(el('em', null, t(f[0])));
        s.appendChild(document.createTextNode(f[1]));
        facts.appendChild(s);
      });
      amount.appendChild(el('span', 'lbl', t('ns_simm')));
      amount.appendChild(el('strong', null, ns.simm_txt + ' USD'));
      if (ns.addon_txt !== '0.00') amount.appendChild(el('span', 'addon', t('addon') + ' ' + ns.addon_txt));
      head.appendChild(title);
      head.appendChild(facts);
      head.appendChild(amount);
      card.appendChild(head);
      [['product_class', ''], ['risk_class', ''], ['Delta', 'num'], ['Vega', 'num'], ['Curvature', 'num'], ['BaseCorr', 'num'],
       ['im_usd', 'num'], ['contribution', 'num']].forEach(function (h) { hr.appendChild(el('th', h[1], t(h[0]))); });
      thead.appendChild(hr);
      table.appendChild(thead);
      ns.products.forEach(function (p) {
        var pr = el('tr', 'pc'), name = el('td', null, p.product_class);
        name.colSpan = 6;
        if (Number(p.multiplier) !== 1) name.appendChild(el('span', 'mult', t('multiplier') + ' ×' + p.multiplier));
        pr.appendChild(name);
        pr.appendChild(el('td', 'num', p.margin_txt));
        pr.appendChild(el('td'));
        body.appendChild(pr);
        p.risk_classes.forEach(function (r) {
          var rr = el('tr');
          rr.appendChild(el('td'));
          rr.appendChild(el('td', null, r.risk_class));
          COMPONENTS.forEach(function (k) { rr.appendChild(el('td', 'num', r.components[k] || '–')); });
          rr.appendChild(el('td', 'num strong', r.margin_txt));
          rr.appendChild(el('td', 'num', r.contribution_txt));
          body.appendChild(rr);
        });
      });
      table.appendChild(body);
      tw.appendChild(table);
      card.appendChild(tw);
      box.appendChild(card);
    });
    return box;
  }

  function renderTable(focusName) {
    var body = clear(document.getElementById('cp-body')), rows = visible(), focusRow = null;
    rows.forEach(function (c) {
      var open = !!state.open[c.name], tr = el('tr', 'cp'), first = el('td', 'num'), shareCell = el('td', 'num');
      var share = el('div', 'share'), bar = el('span', 'bar'), fill = el('i');
      tr.tabIndex = 0;
      tr.setAttribute('aria-expanded', String(open));
      first.appendChild(el('span', 'caret', '▸'));
      first.appendChild(document.createTextNode(String(c.rank)));
      fill.style.width = (c.share / maxShare * 100).toFixed(1) + '%';
      bar.appendChild(fill);
      share.appendChild(bar);
      share.appendChild(el('span', 'pct', c.share_txt));
      shareCell.appendChild(share);
      tr.appendChild(first);
      tr.appendChild(el('td', 'name', c.name));
      tr.appendChild(el('td', 'num', String(c.n_ns)));
      tr.appendChild(el('td', 'num strong', c.simm_txt));
      tr.appendChild(shareCell);
      tr.appendChild(el('td', 'num', c.addon_txt));
      tr.addEventListener('click', function () { toggle(c.name); });
      tr.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(c.name, true); }
      });
      body.appendChild(tr);
      if (c.name === focusName) focusRow = tr;
      if (open) {
        var dr = el('tr', 'detail'), td = el('td');
        td.colSpan = 6;
        td.appendChild(detail(c));
        dr.appendChild(td);
        body.appendChild(dr);
      }
    });
    if (!rows.length) {
      var er = el('tr', 'empty'), etd = el('td', null, t('no_match'));
      etd.colSpan = 6;
      er.appendChild(etd);
      body.appendChild(er);
    }
    document.getElementById('count').textContent = rows.length === DATA.counterparties.length ? ''
      : t('shown', { shown: rows.length, total: DATA.counterparties.length });
    each('#cp-table thead button[data-sort]', function (b) {
      var active = b.getAttribute('data-sort') === state.key;
      b.parentNode.setAttribute('aria-sort', active ? (state.dir > 0 ? 'ascending' : 'descending') : 'none');
      b.setAttribute('data-arrow', active ? (state.dir > 0 ? '▲' : '▼') : '');
    });
    if (focusRow) focusRow.focus();
  }

  function renderFoot() {
    var foot = clear(document.getElementById('cp-foot')), tr = el('tr');
    tr.appendChild(el('td'));
    tr.appendChild(el('td', null, t('total')));
    tr.appendChild(el('td', 'num', String(DATA.totals.netting_sets)));
    tr.appendChild(el('td', 'num', DATA.totals.simm_txt));
    tr.appendChild(el('td', 'num', DATA.totals.share_txt));
    tr.appendChild(el('td', 'num', DATA.totals.addon_txt));
    foot.appendChild(tr);
  }

  function list(box, title, items) {
    var ul = el('ul');
    if (!items.length) return;
    box.appendChild(el('h3', null, title));
    items.forEach(function (s) { ul.appendChild(el('li', null, s)); });
    box.appendChild(ul);
  }

  function renderFooter() {
    var box = clear(document.getElementById('footer')), m = DATA.meta, text = L[state.lang].limitation_text || {};
    box.appendChild(el('p', 'notice', t('notice')));
    box.appendChild(el('p', null, t('confidential')));
    list(box, t('issues'), Object.keys(m.issue_counts).map(function (k) { return k.replace(':', ' ') + ' × ' + m.issue_counts[k]; }));
    list(box, t('transformations'), m.input_transformations);
    list(box, t('limitations'), m.limitations.map(function (s) { return text[s] || s; }));
    box.appendChild(el('p', null, t('offline') + ' ' + t('parameter_hash') + ': ' + m.parameter_hash));
  }

  function toggle(name, keepFocus) {
    state.open[name] = !state.open[name];
    renderTable(keepFocus ? name : null);
  }

  function download() {
    var bin = window.atob(XLSX[state.lang] || XLSX.en), bytes = new Uint8Array(bin.length), i, url, a, blob;
    for (i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    blob = new Blob([bytes], { type: DATA.xlsx_mime });
    if (window.navigator.msSaveOrOpenBlob) { window.navigator.msSaveOrOpenBlob(blob, DATA.xlsx_name); return; }
    url = URL.createObjectURL(blob);
    a = document.createElement('a');
    a.href = url;
    a.download = DATA.xlsx_name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
  }

  function render() {
    renderStatic();
    renderMeta();
    renderWarnings();
    renderKpis();
    renderTable();
    renderFoot();
    renderFooter();
  }

  document.getElementById('download').addEventListener('click', download);
  document.getElementById('search').addEventListener('input', function (e) { state.query = e.target.value; renderTable(); });
  document.getElementById('expand').addEventListener('click', function () {
    visible().forEach(function (c) { state.open[c.name] = true; });
    renderTable();
  });
  document.getElementById('collapse').addEventListener('click', function () { state.open = Object.create(null); renderTable(); });
  each('[data-lang]', function (b) {
    b.addEventListener('click', function () {
      state.lang = b.getAttribute('data-lang');
      try { window.localStorage.setItem('simm-report-lang', state.lang); } catch (e) { /* storage not available */ }
      render();
    });
  });
  each('#cp-table thead button[data-sort]', function (b) {
    b.addEventListener('click', function () {
      var k = b.getAttribute('data-sort');
      if (state.key === k) state.dir = -state.dir;
      else { state.key = k; state.dir = FIRST_DIR[k]; }
      renderTable();
    });
  });
  render();
}());
</script>
</body>
</html>
'''
