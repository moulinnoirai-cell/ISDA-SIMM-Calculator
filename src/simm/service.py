"""Calculation service shared by the command line (`python -m simm run`) and the local web app (`python -m simm serve`).

One CRIF -> validation -> version selection -> engine pass. Every input transformation (column mapping, Excel serial
dates, version override) is explicit and recorded in the returned metadata. No network access and no FX conversion.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
import hashlib

from .crif import read_crif, validate
from .engine import Engine
from .parameters import ParameterPackage
from .versioning import load_schedule, select_version

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


def prepare_rows(headers, rows, netting_set_column=None, excel_serial_dates=False):
    """Apply the explicit input transformations; returns (headers, rows, list of what was applied)."""
    applied = []
    if excel_serial_dates:
        if 'ValuationDate' not in headers:
            raise ValueError('--excel-serial-dates given but the CRIF has no ValuationDate column')
        for r in rows:
            r['ValuationDate'] = excel_serial_to_iso(r['ValuationDate'])
        applied.append('ValuationDate: Excel serial numbers converted to ISO dates (--excel-serial-dates)')
    if netting_set_column:
        col = netting_set_column
        if col not in headers:
            raise ValueError(f'--netting-set-column {col} is not a CRIF column')
        if 'NettingSet' in headers:
            raise ValueError('CRIF already has NettingSet; refusing to overwrite it from another column')
        for r in rows:
            r['NettingSet'] = r.get(col, '')
        headers = headers + ['NettingSet']
        applied.append(f'NettingSet := {col} (--netting-set-column); blank values fall back to context netting_set')
    return headers, rows, applied


def choose_version(version, valuation_date):
    """Version selection record: 'auto' follows the schedule, anything else is a recorded override."""
    if version in (None, '', 'auto'):
        return select_version(valuation_date)
    known = [v['version'] for v in load_schedule()['versions']]
    if version not in known:
        raise ValueError(f"Unknown SIMM version {version!r}; available: {', '.join(known)}")
    selection = {'version': version, 'valuation_date': valuation_date, 'override': True,
                 'scheduled_version': select_version(valuation_date)['version']}
    if selection['scheduled_version'] != version:
        selection['warning'] = f"Version override {version} differs from scheduled {selection['scheduled_version']}"
    return selection


@dataclass
class Outcome:
    meta: dict
    validation: object
    result: dict | None

    @property
    def blocked(self):
        return self.validation.blocked


def run_crif(crif_path, context, valuation_date, version='auto', netting_set_column=None, excel_serial_dates=False, crif_label=None):
    """Run one calculation. `context` is copied; `crif_label` names the file in the metadata (default: its path)."""
    crif_path = Path(crif_path)
    context = dict(context)
    ensure_no_secrets(context)
    context['valuation_date'] = valuation_date
    headers, rows = read_crif(crif_path)
    headers, rows, applied = prepare_rows(headers, rows, netting_set_column, excel_serial_dates)
    selection = choose_version(version, valuation_date)
    package = ParameterPackage(selection['version'])
    validation = validate(headers, rows, package, context)
    issues = Counter(f'{i.severity}:{i.code}' for i in validation.issues)
    meta = {'crif_file': crif_label or str(crif_path), 'crif_sha256': hashlib.sha256(crif_path.read_bytes()).hexdigest(),
            'valuation_date': valuation_date, 'version_selection': selection, 'parameter_hash': package.hash,
            'input_transformations': applied, 'validation_status': validation.status, 'issue_counts': dict(issues),
            'rows_read': len(rows), 'rows_used': len(validation.rows), 'rows_excluded': len(validation.excluded)}
    result = None if validation.blocked else Engine(package).calculate(validation, {})
    return Outcome(meta, validation, result)
