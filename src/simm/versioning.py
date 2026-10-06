from datetime import date
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[2]


def load_schedule(root=ROOT):
    schedule = json.loads((Path(root) / 'parameters' / 'version_schedule.json').read_text(encoding='utf-8'))
    starts = [v['use_from_cob'] for v in schedule['versions']]
    if starts != sorted(starts) or len(set(starts)) != len(starts):
        raise ValueError('Version schedule must be strictly ordered by use_from_cob')
    return schedule


def select_version(valuation_date, root=ROOT):
    """SIMM version for a valuation (COB) date by the ISDA 'use from COB' rule; never guesses before the first entry."""
    day = date.fromisoformat(str(valuation_date))
    schedule = load_schedule(root)
    applicable = [v for v in schedule['versions'] if date.fromisoformat(v['use_from_cob']) <= day]
    if not applicable:
        raise ValueError(f'No SIMM package scheduled for {day.isoformat()}; earliest is {schedule["versions"][0]["use_from_cob"]}')
    entry = applicable[-1]
    beyond = day > date.fromisoformat(schedule['verified_through'])
    return {'version': entry['version'], 'use_from_cob': entry['use_from_cob'], 'valuation_date': day.isoformat(),
            'beyond_verified_schedule': beyond, 'schedule_verified_through': schedule['verified_through'],
            'warning': (f'Valuation date after {schedule["verified_through"]}: re-check ISDA for versions newer than {entry["version"]}' if beyond else None)}

def package_diff(old,new):
    out=[]
    def walk(a,b,path):
        if isinstance(a,dict) and isinstance(b,dict):
            for key in sorted(set(a)|set(b)):walk(a.get(key),b.get(key),path+'/'+key)
        elif a!=b:out.append({'path':path,'old':a,'new':b,'result_impact_possible':not path.startswith('/sources/')})
    walk(old,new,'')
    return out

def list_versions(root):
    result=[]
    for folder in sorted((Path(root)/'parameters').glob('simm_*')):
        if (folder/'manifest.json').exists():result.append(json.loads((folder/'manifest.json').read_text(encoding='utf-8')))
    return result

def activate_production(*args,**kwargs):
    raise PermissionError('Production activation is not supported by this reference implementation.')
