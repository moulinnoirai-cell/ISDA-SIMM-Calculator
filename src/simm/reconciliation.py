from decimal import localcontext
from .numeric import D, ZERO, PRECISION, decimal
from .models import plain

ROOT_CAUSES=['Input','CRIF','Portfolio Scope','Counterparty/Netting Set','SIMM Version','Parameter','Mapping','Formula','Concentration','Aggregation','Rounding','Currency/FX','Other']

def compare(internal,benchmark,tolerance=None):
    with localcontext() as ctx:
        ctx.prec=PRECISION
        a,b=decimal(internal),decimal(benchmark)
        difference=a-b;absolute=abs(difference)
        relative=absolute/abs(b) if b else ZERO if a==0 else None
        status='EXACT_MATCH_UNAPPROVED' if absolute==0 else 'REVIEW_TOLERANCE_NOT_APPROVED'
        if tolerance is not None:
            required={'absolute','relative','approval_id','approved_by','evidence_hash','metric','currency'}
            if not required<=set(tolerance) or not all(tolerance[k] for k in ('approval_id','approved_by','evidence_hash')): raise ValueError('Tolerance has no complete approval/evidence record')
            at,rt=decimal(tolerance['absolute']),decimal(tolerance['relative'])
            if at<0 or rt<0:raise ValueError('Tolerance must be non-negative')
            status='MATCH' if absolute<=at or relative is not None and relative<=rt else 'MISMATCH'
        return plain({'internal':a,'benchmark':b,'signed_difference':difference,'absolute_difference':absolute,'relative_difference':relative,'status':status,'tolerance':tolerance})

def reconcile(result,benchmarks,configuration):
    """Counterparty-level rows must identify their complete scope and version explicitly."""
    output=[];seen=set();expected={c['counterparty']:c for c in result['counterparties']}
    for b in benchmarks:
        key=(b['Date'],b['Counterparty'],b.get('Direction'),b.get('Regulation'))
        if key in seen:raise ValueError('Duplicate benchmark counterparty/date/direction/regulation')
        seen.add(key);cp=b['Counterparty'];c=expected.get(cp)
        metadata={'Date':b['Date'],'Counterparty':cp,'BenchmarkSource':b.get('Source'),'BenchmarkOriginalHash':b.get('OriginalHash'),
                  'RootCause':None,'RootCauseCandidates':[],'USD':None,'Status':'REVIEW_REQUIRED'}
        if c is None:
            metadata.update(Status='MISSING_INTERNAL_SCOPE',RootCauseCandidates=['Counterparty/Netting Set']);output.append(metadata);continue
        mismatch=[]
        if b['Date']!=configuration['valuation_date']:mismatch.append('Input')
        if b.get('SIMMVersion')!=result['simm_version']:mismatch.append('SIMM Version')
        if b.get('Direction')!=configuration['direction'] or b.get('Regulation')!=configuration['regulation']:mismatch.append('Portfolio Scope')
        if b.get('ScopeVerified') is not True:mismatch.append('Portfolio Scope')
        if mismatch:
            metadata.update(Status='NOT_COMPARABLE',RootCauseCandidates=sorted(set(mismatch)));output.append(metadata);continue
        tolerance=configuration.get('tolerances',{}).get('USD')
        if tolerance and (tolerance['metric']!='SIMM' or tolerance['currency']!='USD'):raise ValueError('Tolerance metric/currency mismatch')
        metadata['USD']=compare(c['total_simm'],b['USD_IM_Exposure'],tolerance)
        metadata['Status']=metadata['USD']['status']
        if metadata['USD']['absolute_difference']!='0':metadata['RootCauseCandidates']+=ROOT_CAUSES.copy()
        metadata['RootCauseCandidates']=sorted(set(metadata['RootCauseCandidates']))
        output.append(metadata)
    covered={x['Counterparty'] for x in output}
    for cp in expected.keys()-covered:
        output.append({'Date':configuration['valuation_date'],'Counterparty':cp,'USD':None,'Status':'MISSING_BENCHMARK','RootCause':None,'RootCauseCandidates':['Portfolio Scope']})
    return output

def tolerance_candidates(rows):
    """Descriptive candidates only, never auto-applied or labelled approved."""
    amounts=sorted(decimal(x['absolute_difference']) for x in rows)
    relative=sorted(decimal(x['relative_difference']) for x in rows if x.get('relative_difference') is not None)
    def stats(values):
        if not values:return None
        return plain({'minimum':values[0],'median':values[len(values)//2] if len(values)%2 else (values[len(values)//2-1]+values[len(values)//2])/2,'maximum':values[-1]})
    return {'status':'CANDIDATE_NOT_APPROVED','sample_count':len(amounts),'absolute':stats(amounts),'relative':stats(relative),'decision':'User must assess root causes and approve metric-specific tolerances with evidence.'}
