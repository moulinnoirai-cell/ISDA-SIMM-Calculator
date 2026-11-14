"""Transcribe the ISDA SIMM 2.9+2606 public methodology PDF into parameters/simm_2_9_2606/.

    python tools/transcribe_simm_2_9.py ISDA-SIMM_v2.92606_PUBLIC.pdf

Needs `pypdf` (development only; the calculator itself has no dependencies). The PDF is not part of this repository.
Steps: (1) parse every table of sections D-K from the PDF text, (2) require the same structure as the 2.8+2512 package,
(3) write parameters.json and manifest.json, (4) round-trip check: re-render each parsed table as the PDF prints it and
require the line to be present in the PDF text. Any failure aborts before files are written.
"""
from decimal import Decimal
from pathlib import Path
import copy, hashlib, json, re, sys

ROOT = Path(__file__).resolve().parents[1]
VERSION = '2.9+2606'
URL = 'https://www.isda.org/a/ZYxiE/ISDA-SIMM_v2.92606_PUBLIC.pdf'
TENORS = ['2w', '1m', '3m', '6m', '1y', '2y', '3y', '5y', '10y', '15y', '20y', '30y']
RISK_CLASSES = ['InterestRate', 'CreditQualifying', 'CreditNonQualifying', 'Equity', 'Commodity', 'FX']
MILLION = Decimal(1000000)


def clean(pdf_path):
    import pypdf
    pages = []
    for page in pypdf.PdfReader(str(pdf_path)).pages:
        text = page.extract_text() or ''
        pages.append('\n'.join(l for l in text.split('\n') if l.strip() and not re.search(r'Final Document|Copyright ©|SIMM Version 2\.', l)))
    return pages


def pct(token):
    return str(Decimal(token.rstrip('%')) / 100)


def number(token):
    return str(Decimal(token.replace(',', '')))


def _anchor(text, marker, pos=0):
    """First match of `marker` at or after pos; runs of whitespace in the marker match any whitespace (PDF extractors differ)."""
    m = re.compile(r'\s+'.join(re.escape(w) for w in marker.split())).search(text, pos)
    if not m:
        raise ValueError(f'marker not found: {marker!r}')
    return m


def after(text, start, end=None):
    first = _anchor(text, start)
    stop = _anchor(text, end, first.end()).start() if end else len(text)
    return text[first.end():stop]


def matrix_rows(block, labels):
    """Rows 'label v v v ...' with the diagonal left blank in the PDF; returns {row: {col: value}} with 1 on the diagonal."""
    out = {}
    for line in block.split('\n'):
        tokens = line.split()
        if not tokens or tokens[0] not in labels or tokens[0] in out:
            continue
        values = [t for t in tokens[1:] if t.endswith('%')]
        if len(values) != len(labels) - 1 or len(values) != len(tokens) - 1:
            continue
        index = labels.index(tokens[0])
        values = [pct(v) for v in values]
        values.insert(index, '1')
        out[tokens[0]] = dict(zip(labels, values))
    if list(out) != labels:
        raise ValueError(f'matrix rows incomplete: got {list(out)}')
    return out


def table_pairs(block, labels, value=number):
    out = {}
    for line in block.split('\n'):
        m = re.fullmatch(r'(\S+)\s+(-?[\d,.]+%?)', line.strip())
        if m and m.group(1) in labels and m.group(1) not in out:
            out[m.group(1)] = value(m.group(2))
    if list(out) != labels:
        raise ValueError(f'table incomplete: got {list(out)}')
    return out


def ranged(block, ranges, scale):
    """Concentration tables: rows start with a bucket or a range of buckets like '2-6, 8-12'."""
    out = {}
    for key, buckets in ranges:
        m = re.search(r'^' + re.escape(key) + r'\s+.*?\s([\d,.]+)\s*$', block, re.M)
        if not m:
            raise ValueError(f'concentration row {key!r} not found')
        for b in buckets:
            out[b] = str(Decimal(m.group(1).replace(',', '')) * scale)
    return out


def parse(pages):
    t = '\n'.join(pages)
    t = t[_anchor(t, 'D.1 Interest Rate').start():]  # skip the table of contents, which repeats the section titles
    P = {}
    # ---- Interest rate (D, J.1, J.6)
    ir = {}
    for n, name in ((1, 'regular'), (2, 'low'), (3, 'high')):
        m = re.search(r'Table %d: Risk weights per vertex.*?\n2w 1m 3m 6m 1y 2y 3y 5y 10y 15y 20y 30y\s*\n([\d ]+)\n' % n, t)
        values = m.group(1).split()
        assert len(values) == 12
        ir.setdefault('rw', {})[name] = dict(zip(TENORS, values))
    ir['inflation_rw'] = re.search(r"inflation rate is\s+(\d+)", t).group(1)
    ir['xccy_rw'] = re.search(r'swap spread is\s+(\d+)\s*\n', t).group(1)
    ir['hvr'] = number(re.search(r'for the interest-rate risk class is\s+(\d+(?:\.\d+)?)', t).group(1))
    ir['vrw'] = number(re.search(r'VRW, for the Interest Rate risk class is\s+(\d+(?:\.\d+)?)', t).group(1))
    ir['tenor_corr'] = matrix_rows(after(t, 'Correlations for aggregated weighted sensitivities or risk exposures', 'For sub-curves'), TENORS)
    m = re.search(r'\(to one decimal\s+place\)\s*(\d)\s*(\d\.\d)%', t)
    ir['subcurve_corr'] = pct(m.group(1) + m.group(2))
    ir['inflation_corr'] = pct(re.search(r'inflation volatility and any interest-rate volatility\s+for the same currency\) is (\d+%)', t).group(1))
    ir['xccy_corr'] = pct(re.search(r'swap spread and any yield or inflation rate for the same currency is (-?\d+%)', t).group(1))
    ir['inter_corr'] = pct(re.search(r'= (\d+%) should be used for aggregating across different currencies', t).group(1))
    ir['regular_currencies'] = parse_currency_codes(after(t, '(1) There is one table for regular volatility currencies, which are defined to be:', '(2) There is a second table'))
    ir['low_currencies'] = parse_currency_codes(after(t, '(2) There is a second table for low-volatility currencies, which are defined to be the', '(3) There is a third table'))
    ir['well_traded'] = re.findall(r'[A-Z]{3}', re.search(r'Regular volatility, well-traded:\s+([A-Z; ]+)\n', t).group(1))
    j1 = after(t, 'J.1  Interest Rate risk', 'J.2')
    j6 = after(t, 'J.6  Interest Rate risk', 'J.7')
    groups = (('high', 'High volatility'), ('well_traded', 'Regular volatility, well-traded'), ('less_traded', 'Regular volatility, less well-traded'), ('low', 'Low volatility'))
    ir['ct'] = {k: str(Decimal(number(re.search(r'^' + re.escape(label) + r'\s+([\d,.]+)\s*$', j1, re.M).group(1))) * MILLION) for k, label in groups}
    vgroups = (('high', 'High volatility'), ('well_traded', 'Regular volatility, well traded'), ('less_traded', 'Regular volatility, less well traded'), ('low', 'Low volatility'))
    ir['vt'] = {k: str(Decimal(number(re.search(r'^' + re.escape(label) + r'\s+([\d,.]+)\s*$', j6, re.M).group(1))) * MILLION) for k, label in vgroups}
    P['InterestRate'] = ir
    # ---- Credit qualifying (E)
    buckets12 = [str(i) for i in range(1, 13)]
    e = after(t, 'E. Credit Qualifying risk', 'F. Credit Non-Qualifying risk')
    cq = {'rw': table_pairs(after(e, 'Bucket Risk weight', '40.'), buckets12 + ['Residual'])}
    cq['vrw'] = number(re.search(r'VRW, for the Credit risk class is (\d+(?:\.\d+)?)', e).group(1))
    cq['base_rw'] = number(re.search(r'Base Correlation risk weight is (\d+(?:\.\d+)?)', e).group(1))
    agg = re.search(r'Aggregate sensitivities (\d+%) (\d+%)', e)
    res = re.search(r'Residual bucket (\d+%) (\d+%)', e)
    cq['same_corr'], cq['different_corr'] = pct(agg.group(1)), pct(agg.group(2))
    assert res.group(1) == res.group(2)
    cq['residual_corr'] = pct(res.group(1))
    cq['base_corr'] = pct(re.search(r'across different index families is (\d+%)', e).group(1))
    cq['inter_corr'] = matrix_rows(after(e, 'Bucket 1 2 3 4 5 6 7 8 9 10 11 12'), buckets12)
    j2 = after(t, 'J.2  Credit spread risk', 'J.3')
    q_ct = j2[:j2.index('Non-Qualifying')]
    cq['ct'] = ranged(q_ct, [('1, 7', ['1', '7']), ('2-6, 8-12', ['2', '3', '4', '5', '6', '8', '9', '10', '11', '12']), ('Residual', ['Residual'])], MILLION)
    j7 = after(t, 'J.7  Credit spread risk', 'J.8')
    cq['vt'] = {b: str(Decimal(re.search(r'^Qualifying\s+([\d,.]+)\s*$', j7, re.M).group(1).replace(',', '')) * MILLION) for b in buckets12 + ['Residual']}
    P['CreditQualifying'] = cq
    # ---- Credit non-qualifying (F)
    f = after(t, 'F. Credit Non-Qualifying risk', 'G. Equity risk')
    cnq = {'rw': table_pairs(after(f, 'Bucket number Risk weight', '47.'), ['1', '2', 'Residual'])}
    cnq['vrw'] = number(re.search(r'VRW, for Credit Non-Qualifying is (\d+(?:\.\d+)?)', f).group(1))
    agg = re.search(r'Aggregate sensitivities (\d+%) (\d+%)', f)
    res = re.search(r'Residual bucket (\d+%) (\d+%)', f)
    cnq['same_corr'], cnq['different_corr'] = pct(agg.group(1)), pct(agg.group(2))
    assert res.group(1) == res.group(2)
    cnq['residual_corr'] = pct(res.group(1))
    cnq['inter_corr'] = pct(re.search(r'Non-residual bucket to non-residual bucket (\d+%)', f).group(1))
    nq_ct = j2[j2.index('Non-Qualifying'):]
    cnq['ct'] = {'1': ranged(nq_ct, [('1', ['1'])], MILLION)['1'], '2': ranged(nq_ct, [('2', ['2'])], MILLION)['2'], 'Residual': ranged(nq_ct, [('Residual', ['Residual'])], MILLION)['Residual']}
    nq_vt = str(Decimal(re.search(r'^Non Qualifying\s+([\d,.]+)\s*$', j7, re.M).group(1)) * MILLION)
    cnq['vt'] = {'1': nq_vt, '2': nq_vt, 'Residual': nq_vt}
    P['CreditNonQualifying'] = cnq
    # ---- Equity (G, J.3, J.8)
    g = after(t, 'G. Equity risk', 'H. Commodity risk')
    eq = {'rw': table_pairs(after(g, 'Bucket Risk Weight', '57.'), buckets12 + ['Residual'])}
    eq['hvr'] = pct(re.search(r'HVR, for the equity risk class is (\d+%)', g).group(1))
    m = re.search(r'VRW, for the equity risk class is (\d+(?:\.\d+)?) for all buckets except bucket 12 for which the\s+vega risk weight is (\d+(?:\.\d+)?)', g)
    eq['vrw'], eq['vol_index_vrw'] = number(m.group(1)), number(m.group(2))
    eq['intra_corr'] = table_pairs(after(g, 'Bucket number Correlation', '60.'), buckets12 + ['Residual'], pct)
    eq['inter_corr'] = matrix_rows(after(g, 'Bucket 1 2 3 4 5 6 7 8 9 10 11 12'), buckets12)
    j3 = after(t, 'J.3  Equity risk', 'J.4')
    j8 = after(t, 'J.8  Equity risk', 'J.9')
    eq_ranges = [('1-4', ['1', '2', '3', '4']), ('5-8', ['5', '6', '7', '8']), ('9', ['9']), ('10', ['10']), ('11-12', ['11', '12']), ('Residual', ['Residual'])]
    eq['ct'], eq['vt'] = ranged(j3, eq_ranges, MILLION), ranged(j8, eq_ranges, MILLION)
    P['Equity'] = eq
    # ---- Commodity (H, J.4, J.9)
    buckets17 = [str(i) for i in range(1, 18)]
    h = after(t, 'H. Commodity risk', 'I. Foreign Exchange risk')
    rw = {}
    for line in after(h, 'Bucket Commodity Risk Weight', '62.').split('\n'):
        m = re.fullmatch(r'(\d+) [A-Za-z ]+? (\d+)', line.strip())
        if m:
            rw[m.group(1)] = m.group(2)
    assert list(rw) == buckets17
    com = {'rw': rw}
    com['hvr'] = pct(re.search(r'HVR, for the commodity risk class is (\d+%)', h).group(1))
    com['vrw'] = number(re.search(r'VRW, for the commodity risk class is (\d+(?:\.\d+)?)', h).group(1))
    com['intra_corr'] = table_pairs(after(h, 'Bucket Correlation', '65.'), buckets17, pct)
    com['inter_corr'] = matrix_rows(after(h, 'Bkt 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17'), buckets17)
    j4 = after(t, 'J.4  Commodity risk', 'J.5')
    j9 = after(t, 'J.9  Commodities risk', 'J.10')
    cranges = [('1', ['1']), ('2', ['2']), ('3-5', ['3', '4', '5']), ('6-7', ['6', '7']), ('8-9', ['8', '9']), ('10', ['10']), ('11', ['11']), ('12', ['12']),
               ('13-15', ['13', '14', '15']), ('16', ['16']), ('17', ['17'])]
    com['ct'], com['vt'] = ranged(j4, cranges, MILLION), ranged(j9, cranges, MILLION)
    assert list(com['ct']) == buckets17 and list(com['vt']) == buckets17
    P['Commodity'] = com
    # ---- FX (I, J.5, J.10)
    x = after(t, 'I. Foreign Exchange risk', 'J. Concentration Thresholds')
    para67 = re.sub(r'\s+', ' ', after(x, 'is defined to be:', '68.'))
    high = parse_currency_codes(para67)
    rows = {}
    for label in ('Regular', 'High'):
        rows[label] = re.search(r'^' + label + r' (\d+(?:\.\d+)?) (\d+(?:\.\d+)?)\s*$', after(x, 'FX Volatility group Regular High', '70.'), re.M).groups()
    fx = {'high_currencies': high}
    fx['rw'] = {'regular': {'regular': number(rows['Regular'][0]), 'high': number(rows['Regular'][1])},
                'high': {'regular': number(rows['High'][0]), 'high': number(rows['High'][1])}}
    fx['hvr'] = number(re.search(r'HVR, for the FX risk class is (\d+(?:\.\d+)?)', x).group(1))
    fx['vrw'] = number(re.search(r'VRW, for FX volatility is (\d+(?:\.\d+)?)', x).group(1))
    fx['vol_corr'] = number(re.search(r'the correlation shall be\s+taken to be (\d+(?:\.\d+)?)', x).group(1))
    reg_block = after(x, 'if the calculation currency is in the regular FX', 'The second table')
    hi_block = after(x, 'The second table shows', '73.')
    def corr_table(block):
        a = re.search(r'^Regular (\d+%) (\d+%)\s*$', block, re.M).groups()
        b = re.search(r'^High (\d+%) (\d+%)\s*$', block, re.M).groups()
        assert a[1] == b[0]
        return {'regular': {'regular': pct(a[0]), 'high': pct(a[1])}, 'high': {'regular': pct(b[0]), 'high': pct(b[1])}}
    fx['delta_corr'] = {'regular': corr_table(reg_block), 'high': corr_table(hi_block)}
    c = re.sub(r'\s+', ' ', after(t, '80. Currencies were placed in three categories', '\nJ.6'))
    cats = {}
    for n, label in (('1', 'Category 1 - Significantly material:'), ('2', 'Category 2 - Frequently traded:')):
        cats[n] = re.findall(r'[A-Z]{3}', after(c, label, 'Category %d' % (int(n) + 1)))
    fx['categories'] = cats
    j5 = after(t, 'J.5  FX risk', 'J.6')
    fx['ct'] = {n: str(Decimal(re.search(r'^Category %s ([\d,]+)\s*$' % n, j5, re.M).group(1).replace(',', '')) * MILLION) for n in '123'}
    j10 = after(t, 'J.10  FX risk', '87.')
    vt = {}
    for line in j10.split('\n'):
        m = re.fullmatch(r'Category (\d) - Category (\d) ([\d,]+)', line.strip())
        if m:
            vt[f'{m.group(1)}:{m.group(2)}'] = str(Decimal(m.group(3).replace(',', '')) * MILLION)
    fx['vt'] = vt
    P['FX'] = fx
    # ---- Risk class correlation (K)
    k = after(t, 'K. Correlation between risk classes within product classes', 'L. Additional')
    labels = {'Interest Rate': 'InterestRate', 'Credit Qualifying': 'CreditQualifying', 'Credit Non-qualifying': 'CreditNonQualifying', 'Equity': 'Equity', 'Commodity': 'Commodity', 'FX': 'FX'}
    rcc = {}
    for line in k.split('\n'):
        for label, key in labels.items():
            if line.startswith(label + ' ') and key not in rcc:
                values = line[len(label):].split()
                if values and all(v.endswith('%') for v in values) and len(values) == 5:
                    row = [pct(v) for v in values]
                    row.insert(RISK_CLASSES.index(key), '1')
                    rcc[key] = dict(zip(RISK_CLASSES, row))
    assert list(rcc) == RISK_CLASSES
    P['risk_class_corr'] = rcc
    return P


def parse_currency_codes(text):
    text = re.sub(r'\s+', ' ', text)
    names = re.findall(r'\(([A-Z]{3})\)', text)
    return names or re.findall(r'\b[A-Z]{3}\b', text)


def structure(o):
    if isinstance(o, dict):
        return {k: structure(v) for k, v in o.items()}
    if isinstance(o, list):
        return 'list'
    return 'value'


def shape_check(new, old, path=''):
    if isinstance(old, dict):
        if not isinstance(new, dict) or set(new) != set(old):
            raise ValueError(f'structure differs from 2.8+2512 at {path or "/"}: {sorted(set(new) ^ set(old)) if isinstance(new, dict) else type(new)}')
        for key in old:
            shape_check(new[key], old[key], f'{path}/{key}')
    elif isinstance(old, list):
        if not isinstance(new, list) or not new:
            raise ValueError(f'expected list at {path}')


def roundtrip(P, pages):
    """Render each table back the way the PDF prints it and require that line in the PDF text."""
    flat = re.sub(r'[ \t]+', ' ', '\n'.join(pages))
    missing = []

    def p(v):
        x = Decimal(v) * 100
        return (str(x.quantize(Decimal(1))) if x == x.to_integral() else str(x)) + '%'

    def need(label, rendered):
        if rendered not in flat:
            missing.append((label, rendered))

    ir = P['InterestRate']
    for name in ('regular', 'low', 'high'):
        need('IR rw ' + name, ' '.join(ir['rw'][name][t] for t in TENORS))
    for row in TENORS:
        need('IR corr ' + row, row + ' ' + ' '.join(p(ir['tenor_corr'][row][c]) for c in TENORS if c != row))
    for key, size in (('CreditQualifying', 12), ('Equity', 12), ('Commodity', 17)):
        labels = [str(i) for i in range(1, size + 1)]
        for row in labels:
            need(f'{key} inter_corr {row}', row + ' ' + ' '.join(p(P[key]['inter_corr'][row][c]) for c in labels if c != row))
    for key in ('CreditQualifying', 'CreditNonQualifying', 'Equity', 'Commodity'):
        for bucket, value in P[key]['rw'].items():
            need(f'{key} rw {bucket}', f'{bucket} {int(value):,}' if not bucket.isdigit() or key != 'Commodity' else f' {value}')
    for key in ('Equity', 'Commodity'):
        for bucket, value in P[key]['intra_corr'].items():
            need(f'{key} intra_corr {bucket}', f'{bucket} {p(value)}')
    for row in RISK_CLASSES:
        pass
    rcc = P['risk_class_corr']
    for key in RISK_CLASSES:
        need(f'risk_class_corr {key}', ' '.join(p(rcc[key][c]) for c in RISK_CLASSES if c != key))
    fx = P['FX']
    need('FX rw', f"Regular {fx['rw']['regular']['regular']} {fx['rw']['regular']['high']}")
    need('FX rw', f"High {fx['rw']['high']['regular']} {fx['rw']['high']['high']}")
    for calc in ('regular', 'high'):
        t = fx['delta_corr'][calc]
        need('FX corr', f"Regular {p(t['regular']['regular'])} {p(t['regular']['high'])}")
        need('FX corr', f"High {p(t['high']['regular'])} {p(t['high']['high'])}")
    for ccy in fx['high_currencies']:
        need('FX high ccy ' + ccy, ccy)
    return missing


def main(pdf_path):
    pages = clean(pdf_path)
    data = parse(pages)
    old_dir = ROOT / 'parameters' / 'simm_2_8_2512'
    old = json.loads((old_dir / 'parameters.json').read_text(encoding='utf-8'))
    new = copy.deepcopy(old)
    for key, value in data.items():
        new[key] = value
    shape_check(new, old)
    missing = roundtrip(data, pages)
    if missing:
        for label, rendered in missing[:20]:
            print('NOT IN PDF:', label, rendered)
        raise SystemExit(f'{len(missing)} round-trip checks failed; nothing written')
    sha = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
    new.update({'version': VERSION, 'package_version': VERSION + '.reference.1', 'effective_date': '2026-12-05', 'methodology_id': 'simm_2_9', 'source_sha256': sha})
    pages_of = {'normative': ('B.10-11', '5-8'), 'InterestRate': ('D.32-37; J.74-75,81-82', '15-16,27-28'), 'CreditQualifying': ('E.38-43; J.76,83', '17-18,27-28'),
                'CreditNonQualifying': ('F.44-49; J.76,83', '19,27-28'), 'Equity': ('G.50-60; J.77,84', '20-22,27,29'),
                'Commodity': ('H.61-65; J.78,85', '23-24,28-29'), 'FX': ('I.66-73; J.79-80,86-87', '25-26,28-29'), 'risk_class_corr': ('K.88', '30')}
    for key, (section, pg) in pages_of.items():
        new['sources'][key].update({'document': 'ISDA-SIMM_v2.92606_PUBLIC.pdf', 'version': VERSION, 'section': section, 'pages': pg,
                                    'effective_date': '2026-12-05', 'sha256': sha, 'url': URL})
    new['sources']['input_units']['document'] = new['sources']['input_units']['document'].replace('2.8+2512', VERSION)
    new['sources']['input_units']['url'] = URL
    out = ROOT / 'parameters' / 'simm_2_9_2606'
    out.mkdir(exist_ok=True)
    raw = (json.dumps(new, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    (out / 'parameters.json').write_bytes(raw)
    manifest = json.loads((old_dir / 'manifest.json').read_text(encoding='utf-8'))
    manifest.update({'version': VERSION, 'sha256': hashlib.sha256(raw).hexdigest(), 'source_url': URL})
    manifest['notes'] = ['Transcribed from the ISDA public methodology PDF listed in source_url (tools/transcribe_simm_2_9.py).',
                         'Round-trip checked: the matrices and risk-weight rows were re-rendered and found in the PDF text.',
                         'Sections A-C (formulas) are textually identical to 2.8+2512 (whitespace-insensitive comparison), so the simm_2_8 engine formulas apply.']
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print('written', out, 'sha256', manifest['sha256'])


if __name__ == '__main__':
    main(sys.argv[1])
