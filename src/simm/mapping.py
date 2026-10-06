"""Column mapping into CRIF: mappings must be explicit, versioned and approved; ambiguous rules do not apply."""
from .models import Issue

def map_columns(rows,rules):
    if not rules.get('version') or not rules.get('approval_id') or not rules.get('source'):
        return [],[Issue('Review Required','MAPPING_APPROVAL_REQUIRED','Mapping has no approved version and evidence')]
    mapping=rules['columns']
    if len(set(mapping.values()))!=len(mapping):raise ValueError('Multiple source columns map to the same CRIF target')
    output=[];issues=[]
    for i,row in enumerate(rows,2):
        missing=set(mapping)-set(row)
        if missing:issues.append(Issue('Critical','MAPPING_SOURCE_MISSING',f'Missing mapping source columns: {sorted(missing)}',f'row:{i}'));continue
        unknown=set(row)-set(mapping)
        if unknown:issues.append(Issue('Review Required','UNMAPPED_COLUMNS',f'Explicitly account for unmapped columns: {sorted(unknown)}',f'row:{i}'));continue
        out={mapping[k]:v for k,v in row.items()}
        output.append(out)
    return output,issues
