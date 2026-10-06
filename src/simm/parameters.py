from pathlib import Path
from decimal import Decimal
import hashlib, json, re

ROOT = Path(__file__).resolve().parents[2]
# Sections B-C (formulas) of SIMM 2.5, 2.5A, 2.6, 2.7, 2.7+2412, 2.8+2506 and 2.8+2512 are textually identical
# (word, formula-glyph and page-image comparison, 2026-10-06), so one engine plugin serves these packages.
SUPPORTED_METHODOLOGIES = {'simm_2_5', 'simm_2_6', 'simm_2_7', 'simm_2_8'}

class ParameterPackage:
    def __init__(self, version='2.8+2512', root=ROOT, data=None):
        if data is None:
            if not re.fullmatch(r'[0-9]+\.[0-9]+[A-Z]?(?:\+[0-9]+)?',version): raise ValueError('Invalid version identifier')
            path=Path(root)/'parameters'/('simm_'+version.replace('.','_').replace('+','_'))
            raw=(path/'parameters.json').read_bytes()
            self.manifest=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
            self.hash=hashlib.sha256(raw).hexdigest()
            if self.manifest['sha256'] != self.hash: raise ValueError('Parameter package hash mismatch')
            self.data=json.loads(raw)
        else:
            self.data=data
            self.manifest={'status':'REPLAY_SNAPSHOT'}
            self.hash=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
        if self.data['version'] != version: raise ValueError('Parameter version mismatch')
        if self.data['methodology_id'] not in SUPPORTED_METHODOLOGIES: raise ValueError('Unsupported methodology plugin')
        self.version=version
        self.validate()

    def validate(self):
        expected=['InterestRate','CreditQualifying','CreditNonQualifying','Equity','Commodity','FX']
        if self.data['risk_classes']!=expected: raise ValueError('Risk class package coverage')
        for name,labels in [('InterestRate',self.data['tenors']),('CreditQualifying',[str(i) for i in range(1,13)]),('Equity',[str(i) for i in range(1,13)]),('Commodity',[str(i) for i in range(1,18)])]:
            matrix=self.data[name]['tenor_corr' if name=='InterestRate' else 'inter_corr']
            self._matrix(matrix,labels)
        self._matrix(self.data['risk_class_corr'],expected)
        for rc in expected:
            for field in ('ct','vt'):
                if any(Decimal(v)<=0 for v in self.data[rc][field].values()): raise ValueError('Non-positive concentration threshold')
        def numeric(v):
            if isinstance(v,dict):
                for key,val in v.items():
                    if key.endswith('corr') and isinstance(val,str) and not -1<=Decimal(val)<=1: raise ValueError('Invalid correlation')
                    numeric(val)
        numeric(self.data)

    @staticmethod
    def _matrix(m,labels):
        if set(m)!=set(labels): raise ValueError('Matrix dimensions')
        for a in labels:
            if set(m[a])!=set(labels): raise ValueError('Matrix row dimensions')
            for b in labels:
                v=Decimal(m[a][b])
                if not v.is_finite() or not -1<=v<=1 or m[a][b]!=m[b][a]: raise ValueError('Invalid/asymmetric correlation')
                if a==b and v!=1: raise ValueError('Correlation diagonal')

    def ir_group(self,currency,threshold=False):
        p=self.data['InterestRate']
        if currency in p['low_currencies']: return 'low'
        if currency in p['regular_currencies']:
            if threshold: return 'well_traded' if currency in p['well_traded'] else 'less_traded'
            return 'regular'
        return 'high'

    def fx_group(self,currency):
        return 'high' if currency in self.data['FX']['high_currencies'] else 'regular'

    def fx_category(self,currency):
        for k,v in self.data['FX']['categories'].items():
            if currency in v: return k
        return '3'

    def source(self,risk_class):
        return self.data['sources'][risk_class]
