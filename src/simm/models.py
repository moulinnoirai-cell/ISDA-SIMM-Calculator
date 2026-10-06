from dataclasses import dataclass, field, asdict
from decimal import Decimal

@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    message: str
    row_id: str | None = None
    field: str | None = None
    source: str = ''

    def __post_init__(self):
        aliases = {'Critical':'CRITICAL','Warning':'WARNING','Review Required':'REVIEW_REQUIRED'}
        severity = aliases.get(self.severity,self.severity)
        if severity not in ('CRITICAL','WARNING','REVIEW_REQUIRED'):
            raise ValueError('Unsupported validation severity')
        object.__setattr__(self,'severity',severity)

@dataclass(frozen=True)
class Scope:
    counterparty: str
    legal_entity: str
    netting_set: str
    csa: str
    direction: str
    regulation: str

@dataclass
class RiskRow:
    row_id: str
    scope: Scope
    product_class: str
    risk_type: str
    qualifier: str
    bucket: str
    label1: str
    label2: str
    amount_usd: Decimal
    payment_currency: str
    group_name: str
    original: dict

@dataclass
class ValidationResult:
    issues: list[Issue] = field(default_factory=list)
    rows: list[RiskRow] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    @property
    def blocked(self):
        return any(x.severity in ('CRITICAL','REVIEW_REQUIRED') for x in self.issues)

    @property
    def status(self):
        for severity in ('CRITICAL','REVIEW_REQUIRED','WARNING'):
            if any(x.severity == severity for x in self.issues): return severity
        return 'VALID'

def plain(value):
    if isinstance(value,Decimal): return str(value)
    if hasattr(value,'__dataclass_fields__'): return plain(asdict(value))
    if isinstance(value,dict): return {str(k):plain(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)): return [plain(v) for v in value]
    return value
