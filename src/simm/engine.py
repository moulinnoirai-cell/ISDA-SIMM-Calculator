"""ISDA SIMM formula engine for methodology versions 2.5 to 2.8+2512 (identical formula sections). Decimal arithmetic."""
from collections import defaultdict
from dataclasses import dataclass, asdict
from decimal import localcontext
from time import perf_counter
from .numeric import D, ZERO, ONE, PRECISION, sqrt, clamp, quadratic, normal_quantile
from .models import plain
from .crif import TYPES

@dataclass
class Factor:
    key: tuple
    bucket: str
    qualifier: str
    risk_type: str
    label1: str
    label2: str
    payment_currency: str
    group_name: str
    amount: D = ZERO
    concentration: D = ONE
    weight: D = ONE
    weighted: D = ZERO
    children: list = None
    node_id: str = ''
    def __post_init__(self):
        if self.children is None: self.children=[]

class Trace:
    def __init__(self): self.nodes=[]
    def add(self,level,value,children,formula,source,**metadata):
        node={'id':f'trace:{len(self.nodes)+1}','level':level,'value':value,'children':list(children),'formula':formula,'source':source,**metadata}
        self.nodes.append(node)
        return node['id']

CALCULATION_CURRENCY='USD'  # FX risk factors are defined against USD; amounts are CRIF AmountUSD. No FX conversion.

class Engine:
    def __init__(self,package,concentration=True):
        self.package=package;self.p=package.data;self.use_concentration=concentration;self.calc_ccy=CALCULATION_CURRENCY
        self.trace=Trace();self.timings={}

    def rw(self,rc,row):
        p=self.p[rc]
        if rc=='InterestRate':
            if row.risk_type=='Risk_Inflation': return D(p['inflation_rw'])
            if row.risk_type=='Risk_XCcyBasis': return D(p['xccy_rw'])
            return D(p['rw'][self.package.ir_group(row.qualifier)][row.label1])
        if rc=='FX':
            if row.risk_type=='Risk_FX':
                if row.qualifier==self.calc_ccy: return ZERO
                return D(p['rw'][self.package.fx_group(row.qualifier)][self.package.fx_group(self.calc_ccy)])
            q=row.qualifier
            return D(p['rw'][self.package.fx_group(q[:3])][self.package.fx_group(q[3:])])
        return D(p['rw'][row.bucket])

    def sigma(self,rc,row):
        n=self.p['normative']
        if rc in ('InterestRate','CreditQualifying','CreditNonQualifying'): return ONE
        return self.rw(rc,row)*sqrt(D(n['calendar_days_year'])/D(n['horizon_days']))/normal_quantile(n['alpha_probability'])

    def sf(self,tenor):
        n=self.p['normative']
        length=D(tenor[:-1]);unit=tenor[-1]
        days=length*(D(7) if unit=='w' else D(n['calendar_days_year'])/D(n['calendar_days_months']) if unit=='m' else D(n['calendar_days_year']))
        return D(n['curvature_scaling'])*min(ONE,D(n['horizon_days'])/days)

    def threshold(self,rc,f,component):
        p=self.p[rc];field='ct' if component=='Delta' else 'vt'
        if rc=='InterestRate': key=self.package.ir_group(f.qualifier,threshold=True)
        elif rc=='FX':
            if component=='Delta': key=self.package.fx_category(f.qualifier)
            else: key=':'.join(sorted([self.package.fx_category(f.qualifier[:3]),self.package.fx_category(f.qualifier[3:])]))
        else: key=f.bucket
        return D(p[field][key])

    def rho(self,rc,a,b):
        p=self.p[rc]
        if rc=='InterestRate':
            if a.risk_type=='Risk_XCcyBasis' or b.risk_type=='Risk_XCcyBasis': return D(p['xccy_corr'])
            ia=a.risk_type in ('Risk_Inflation','Risk_InflationVol');ib=b.risk_type in ('Risk_Inflation','Risk_InflationVol')
            if ia!=ib:return D(p['inflation_corr'])
            if a.risk_type=='Risk_Inflation' and b.risk_type=='Risk_Inflation':return ONE
            rho=D(p['tenor_corr'][a.label1][b.label1])
            if a.risk_type=='Risk_IRCurve' and b.risk_type=='Risk_IRCurve' and a.label2!=b.label2:rho*=D(p['subcurve_corr'])
            return rho
        if rc=='CreditQualifying':
            if a.bucket=='Residual':return D(p['residual_corr'])
            return D(p['same_corr'] if a.qualifier==b.qualifier else p['different_corr'])
        if rc=='CreditNonQualifying':
            if a.bucket=='Residual':return D(p['residual_corr'])
            return D(p['same_corr'] if a.group_name==b.group_name else p['different_corr'])
        if rc=='FX':
            if a.risk_type=='Risk_FXVol':return D(p['vol_corr'])
            return D(p['delta_corr'][self.package.fx_group(self.calc_ccy)][self.package.fx_group(a.qualifier)][self.package.fx_group(b.qualifier)])
        return D(p['intra_corr'][a.bucket])

    def gamma(self,rc,a,b):
        value=self.p[rc].get('inter_corr','0')
        return D(value[a][b] if isinstance(value,dict) else value)

    def factors(self,rows,rc,component,meta):
        factors={}
        for row in rows:
            kind=TYPES[row.risk_type][1]
            if component=='Delta' and kind!='Delta' or component in ('Vega','Curvature') and kind!='Vega': continue
            bucket=row.qualifier if rc=='InterestRate' else 'FX' if rc=='FX' else row.bucket
            qualifier=''.join(sorted([row.qualifier[:3],row.qualifier[3:]])) if row.risk_type=='Risk_FXVol' else row.qualifier
            # Expiry weighting precedes netting. Non-IR/non-credit risk factors are spots/pairs.
            label=row.label1 if rc in ('InterestRate','CreditQualifying','CreditNonQualifying') else ''
            key=(bucket,qualifier,row.risk_type,label,row.label2,row.payment_currency,row.group_name)
            if key not in factors:factors[key]=Factor(key,bucket,qualifier,row.risk_type,label,row.label2,row.payment_currency,row.group_name)
            f=factors[key]
            amount=row.amount_usd;scale=ONE
            if component=='Vega':
                scale=self.sigma(rc,row)
                if rc in ('Equity','Commodity','FX'):scale*=D(self.p[rc]['hvr'])
            elif component=='Curvature':
                scale=self.sigma(rc,row)*self.sf(row.label1)
                if rc=='Equity' and row.bucket=='12':scale=ZERO
            transformed=amount*scale;f.amount+=transformed
            child=self.trace.add('SensitivityTransform',transformed,[row.row_id],
                'rawUSD' if component=='Delta' else 'rawUSD * sigma * HVR (HVR only EQ/Commodity/FX)' if component=='Vega' else 'rawUSD * sigma * SF(expiry); no VRW/CR/HVR',
                f'SIMM {self.package.version} B.7-11 pp3-8; RDS 1.36 2.8 pp10-11',
                **meta,component=component,bucket=bucket,qualifier=qualifier,raw_usd=amount,scale=scale,row_id=row.row_id)
            f.children.append(child)
        # Net amounts entering concentration: bucket-wide IR, issuer-wide credit, spot/pair otherwise.
        totals=defaultdict(lambda:ZERO)
        for f in factors.values():
            if component=='Delta' and f.risk_type=='Risk_XCcyBasis':continue
            if component=='Delta' and rc=='FX' and f.qualifier==self.calc_ccy:continue
            group=f.bucket if rc=='InterestRate' else (f.bucket,f.qualifier)
            totals[group]+=f.amount
        for f in factors.values():
            group=f.bucket if rc=='InterestRate' else (f.bucket,f.qualifier)
            excluded=component=='Delta' and (f.risk_type=='Risk_XCcyBasis' or rc=='FX' and f.qualifier==self.calc_ccy)
            threshold=None
            if component!='Curvature' and not excluded:
                threshold=self.threshold(rc,f,component)
                if self.use_concentration:f.concentration=max(ONE,sqrt(abs(totals[group])/threshold))
            if component=='Delta':f.weight=self.rw(rc,f)
            elif component=='Vega':f.weight=D(self.p[rc]['vol_index_vrw'] if rc=='Equity' and f.bucket=='12' else self.p[rc]['vrw'])
            else:f.weight=ONE
            f.weighted=f.amount*f.weight*f.concentration
            f.node_id=self.trace.add('RiskFactor',f.weighted,f.children,
                'net * RW * CR' if component=='Delta' else 'net VR * VRW * VCR' if component=='Vega' else 'net scaled curvature',
                f'SIMM {self.package.version} B.7-11 pp3-8',**meta,component=component,bucket=f.bucket,qualifier=f.qualifier,
                risk_type=f.risk_type,label1=f.label1,label2=f.label2,payment_currency=f.payment_currency,group_name=f.group_name,
                net=f.amount,weight=f.weight,concentration=f.concentration,concentration_net=totals[group],threshold=threshold,
                parameters=self.package.source(rc),excluded_from_concentration=excluded,concentration_enabled=self.use_concentration)
        return list(factors.values())

    def component(self,rows,rc,component,meta):
        if component=='BaseCorr':
            amounts=defaultdict(lambda:ZERO);children=defaultdict(list)
            for row in rows:
                if row.risk_type=='Risk_BaseCorr':amounts[row.qualifier]+=row.amount_usd;children[row.qualifier].append(row.row_id)
            weight=D(self.p[rc]['base_rw']);corr=D(self.p[rc]['base_corr'])
            values={k:v*weight for k,v in amounts.items()}
            nodes=[self.trace.add('RiskFactor',v,children[k],'net family BC01 * RW','SIMM E.41-42 p17; B.13 p8',**meta,component=component,qualifier=k,weight=weight,concentration=ONE,net=amounts[k],parameters=self.package.source(rc)) for k,v in values.items()]
            value=sqrt(quadratic(list(values),lambda k:values[k],lambda a,b:corr))
            return value,self.trace.add('RiskType',value,nodes,'sqrt(sum WS^2 + ordered cross-products rho * WS_k * WS_l)','SIMM B.13 p8',**meta,component=component)
        fs=self.factors(rows,rc,component,meta)
        buckets={}
        for bucket in sorted({f.bucket for f in fs}):
            bf=[f for f in fs if f.bucket==bucket]
            def corr(a,b):
                rho=self.rho(rc,a,b)
                if component=='Curvature':return rho**2
                adjustment=ONE if rc=='InterestRate' else min(a.concentration,b.concentration)/max(a.concentration,b.concentration)
                return rho*adjustment
            q=quadratic(bf,lambda f:f.weighted,corr);k=sqrt(q);s=clamp(sum((f.weighted for f in bf),ZERO),k)
            node=self.trace.add('Bucket',k,[f.node_id for f in bf],'sqrt(quadratic weighted exposure); S=clamp(sum exposure,-K,K)',
                f'SIMM B.{"11(c) p7" if component=="Curvature" else "7(c) p3" if rc=="InterestRate" and component=="Delta" else "8(c) p4" if component=="Delta" else "10(e) p6"}',
                **meta,component=component,bucket=bucket,K=k,S=s,quadratic_form=q,concentration=bf[0].concentration if rc=='InterestRate' else None)
            buckets[bucket]={'K':k,'S':s,'node':node,'factors':bf,'cr':bf[0].concentration if bf else ONE}
        nonres=[b for b in buckets if b!='Residual']
        def corr_b(a,b):
            gamma=self.gamma(rc,a,b)
            if component=='Curvature':return gamma**2
            if rc=='InterestRate':gamma*=min(buckets[a]['cr'],buckets[b]['cr'])/max(buckets[a]['cr'],buckets[b]['cr'])
            return gamma
        rad=sum((buckets[b]['K']**2 for b in nonres),ZERO)
        for i,a in enumerate(nonres):
            for b in nonres[i+1:]:rad+=2*corr_b(a,b)*buckets[a]['S']*buckets[b]['S']
        k=sqrt(rad)
        if component=='Curvature':
            def curvature(selected,k):
                amounts=[f.weighted for b in selected for f in buckets[b]['factors']]
                total=sum(amounts,ZERO);absolute=sum(map(abs,amounts),ZERO)
                theta=min(total/absolute,ZERO) if absolute else ZERO
                z=normal_quantile(self.p['normative']['lambda_probability'])
                lam=(z*z-ONE)*(ONE+theta)-theta
                return max(total+lam*k,ZERO),{'theta':theta,'lambda':lam,'sum':total,'absolute_sum':absolute,'K':k}
            value,details=curvature(nonres,k)
            residual,rd=curvature(['Residual'],buckets['Residual']['K']) if 'Residual' in buckets else (ZERO,None)
            value+=residual
            ir_scale=ONE/D(self.p['InterestRate']['hvr'])**2 if rc=='InterestRate' else ONE
            value*=ir_scale
            metadata={'non_residual':details,'residual':rd,'ir_hvr_scale':ir_scale}
        else:
            value=k+(buckets['Residual']['K'] if 'Residual' in buckets else ZERO)
            metadata={'non_residual_K':k,'residual_K':buckets.get('Residual',{}).get('K',ZERO)}
        node=self.trace.add('RiskType',value,[x['node'] for x in buckets.values()],
            'max(sum CVR + lambda * interbucket squared-correlation K,0) + residual; IR HVR^-2' if component=='Curvature' else 'sqrt(sum K_b^2 + ordered gamma*g*S_b*S_c) + residual K',
            'SIMM B.11(d) pp7-8' if component=='Curvature' else 'SIMM B.7(d) p3 / B.8(d) p4 / B.10(f) p6',**meta,component=component,details=metadata)
        return value,node

    def calculate(self,validation,configuration=None):
        if validation.blocked:raise ValueError('Calculation blocked by CRIF validation/review issues')
        self.trace=Trace();self.timings={};configuration=configuration or {}
        with localcontext() as ctx:
            ctx.prec=PRECISION
            for row in validation.rows:
                self.trace.nodes.append({'id':row.row_id,'level':'CRIFRow','value':row.amount_usd,'children':[],'formula':'Immutable CRIF source row AmountUSD','source':'Input original','original':row.original,'scope':plain(row.scope),'product_class':row.product_class,'risk_type':row.risk_type})
            scopes=defaultdict(list)
            for row in validation.rows:scopes[row.scope].append(row)
            scope_results=[]
            for scope,rows in sorted(scopes.items(),key=lambda x:tuple(asdict(x[0]).values())):
                products=[];params=[r for r in rows if r.risk_type not in TYPES]
                for pc in self.p['product_classes']:
                    pcrows=[r for r in rows if r.product_class==pc and r.risk_type in TYPES]
                    if not pcrows:continue
                    risks=[]
                    for rc in self.p['risk_classes']:
                        rcrows=[r for r in pcrows if TYPES[r.risk_type][0]==rc]
                        if not rcrows:continue
                        start=perf_counter();components={};nodes=[];meta={'scope':plain(scope),'product_class':pc,'risk_class':rc}
                        for component in ['Delta','Vega','Curvature']+(['BaseCorr'] if rc=='CreditQualifying' else []):
                            value,node=self.component(rcrows,rc,component,meta);components[component]=value;nodes.append(node)
                        im=sum(components.values(),ZERO)
                        node=self.trace.add('RiskClass',im,nodes,'Delta+Vega+Curvature+BaseCorr','SIMM B.5 p2',**meta)
                        risks.append({'risk_class':rc,'margin':im,'components':components,'trace_id':node})
                        self.timings[rc]=self.timings.get(rc,0)+perf_counter()-start
                    def rho(a,b):return D(self.p['risk_class_corr'][a['risk_class']][b['risk_class']])
                    base=sqrt(quadratic(risks,lambda r:r['margin'],rho))
                    mr=[r for r in params if r.risk_type=='Param_ProductClassMultiplier' and r.qualifier==pc]
                    if len({r.amount_usd for r in mr})>1:raise ValueError('Conflicting product class multipliers')
                    multiplier=mr[0].amount_usd if mr else ONE
                    value=base*multiplier
                    for r in risks:
                        coeff=(r['margin']+sum((rho(r,other)*other['margin'] for other in risks if other is not r),ZERO))/base if base else ZERO
                        r['contribution']=r['margin']*coeff*multiplier
                        r['component_contributions']={c:v*coeff*multiplier for c,v in r['components'].items()}
                    node=self.trace.add('ProductClass',value,[r['trace_id'] for r in risks]+[r.row_id for r in mr],
                        'MS * sqrt(sum IM_rc^2 + ordered psi*IM_rc*IM_other)','SIMM B.6 pp2-3; K.88 p29; L.89 p30',scope=plain(scope),product_class=pc,multiplier=multiplier,base_margin=base)
                    products.append({'product_class':pc,'margin':value,'base_margin':base,'multiplier':multiplier,'risk_classes':risks,'trace_id':node})
                addon=ZERO;addon_nodes=[]
                factors={r.qualifier:r.amount_usd for r in params if r.risk_type=='Param_AddOnNotionalFactor'}
                for name in factors:
                    if len({r.amount_usd for r in params if r.risk_type=='Param_AddOnNotionalFactor' and r.qualifier==name})>1:raise ValueError('Conflicting notional factors')
                notionals=defaultdict(list)
                for r in params:
                    if r.risk_type=='Notional':notionals[r.qualifier].append(r)
                if set(notionals)!=set(factors):raise ValueError('Notional/factor product coverage differs; do not silently omit add-on rows')
                for name,nr in notionals.items():
                    gross=sum((abs(r.amount_usd) for r in nr),ZERO);charge=gross*factors[name]/100;addon+=charge
                    addon_nodes.append(self.trace.add('AddOn',charge,[r.row_id for r in nr]+[r.row_id for r in params if r.risk_type=='Param_AddOnNotionalFactor' and r.qualifier==name],
                        'factor percentage /100 * sum abs current trade notionals','SIMM L.89 p30; RDS 1.36 2.10 pp12-13',scope=plain(scope),qualifier=name,gross_notional=gross,factor_percent=factors[name]))
                for fixed in configuration.get('fixed_addons',[]):
                    if fixed.get('scope')==plain(scope):
                        if not fixed.get('source'):raise ValueError('Fixed add-on requires a source/agreement reference')
                        amount=D(fixed['amount_usd'])
                        if not amount.is_finite() or amount<0:raise ValueError('Invalid fixed add-on')
                        addon+=amount
                        addon_nodes.append(self.trace.add('AddOn',amount,[],'Fixed add-on','SIMM L.89 p30',scope=plain(scope),agreement_source=fixed['source']))
                total=sum((p['margin'] for p in products),ZERO)+addon
                node=self.trace.add('NettingSet',total,[p['trace_id'] for p in products]+addon_nodes,'sum product margins + add-ons; no cross-CSA offset','SIMM B.6 pp2-3; L.89 p30',scope=plain(scope))
                scope_results.append({'scope':plain(scope),'total_simm':total,'addon':addon,'products':products,'trace_id':node})
            matched_scopes=[s['scope'] for s in scope_results]
            if any(f.get('scope') not in matched_scopes for f in configuration.get('fixed_addons',[])):raise ValueError('Fixed add-on scope does not match a calculated scope')
            counterparty_results=[]
            for cp in sorted({s['scope']['counterparty'] for s in scope_results}):
                ss=[s for s in scope_results if s['scope']['counterparty']==cp];total=sum((s['total_simm'] for s in ss),ZERO)
                nid=self.trace.add('Counterparty',total,[s['trace_id'] for s in ss],'sum independent netting-set/CSA margins','Internal reporting aggregation; no legal netting inferred',counterparty=cp)
                counterparty_results.append({'counterparty':cp,'total_simm':total,'netting_sets':ss,'trace_id':nid})
            total=sum((c['total_simm'] for c in counterparty_results),ZERO)
            node=self.trace.add('Portfolio',total,[c['trace_id'] for c in counterparty_results],'sum counterparty margins','Internal reporting aggregation; no cross-counterparty netting')
            return plain({'mode':'SHADOW','status':'UNVALIDATED','currency':'USD','amount_unit':'USD (CRIF AmountUSD)','calculation_currency':self.calc_ccy,
                'simm_version':self.package.version,'parameter_hash':self.package.hash,
                'total_simm':total,'counterparties':counterparty_results,'trace':self.trace.nodes,'trace_root':node,'risk_class_seconds':self.timings,
                'contribution_definition':'Additive risk-class and component allocation of product-class margin, based on quadratic aggregation gradient. Risk factor weighted exposures are not additive margin contributions.',
                'limitations':['Latest RDS and official golden tests pending',
                    'Checked against independent parameter transcriptions and an independent re-implementation on synthetic portfolios only',
                    'USD only: no FX conversion or reporting-currency output','Not a substitute for an ISDA SIMM licence or for independent model validation']})
