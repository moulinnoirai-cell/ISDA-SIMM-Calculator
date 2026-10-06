"""Strict local ingestion; no silent repairs, currency inference, or dropped risk."""
from pathlib import Path
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
import csv, io, json, re
from .models import Issue, Scope, RiskRow, ValidationResult
from .numeric import D, decimal

REQUIRED = ['ProductClass','RiskType','Qualifier','Bucket','Label1','Label2','Amount','AmountCurrency','AmountUSD']
TYPES = {
 'Risk_IRCurve':('InterestRate','Delta'),'Risk_Inflation':('InterestRate','Delta'),'Risk_XCcyBasis':('InterestRate','Delta'),
 'Risk_IRVol':('InterestRate','Vega'),'Risk_InflationVol':('InterestRate','Vega'),
 'Risk_CreditQ':('CreditQualifying','Delta'),'Risk_CreditVol':('CreditQualifying','Vega'),'Risk_BaseCorr':('CreditQualifying','BaseCorr'),
 'Risk_CreditNonQ':('CreditNonQualifying','Delta'),'Risk_CreditVolNonQ':('CreditNonQualifying','Vega'),
 'Risk_Equity':('Equity','Delta'),'Risk_EquityVol':('Equity','Vega'),
 'Risk_Commodity':('Commodity','Delta'),'Risk_CommodityVol':('Commodity','Vega'),
 'Risk_FX':('FX','Delta'),'Risk_FXVol':('FX','Vega')}
PARAM_TYPES={'Param_ProductClassMultiplier','Param_AddOnNotionalFactor','Notional','AddOnFixedAmount'}
SOURCE='Public Risk Data Standards 1.36 sections 2.1-2.10, with methodology 2.8+2512 priority'

def read_crif(path):
    path=Path(path)
    if path.suffix.lower()=='.xlsx':
        from openpyxl import load_workbook
        wb=load_workbook(path,read_only=True,data_only=False)
        try:
            if len(wb.worksheets)!=1: raise ValueError('Select one CRIF worksheet explicitly; multi-sheet workbook is ambiguous')
            values=list(wb.worksheets[0].iter_rows(values_only=True))
            if not values: raise ValueError('Empty workbook')
            headers=[str(x) if x is not None else '' for x in values[0]]
            rows=[]
            for row in values[1:]:
                if all(x is None for x in row): continue
                if any(isinstance(x,str) and x.startswith('=') for x in row): raise ValueError('Excel formula cells are not accepted as CRIF source amounts')
                rows.append({h: x.isoformat()[:10] if isinstance(x,(date,datetime)) else '' if x is None else str(x) for h,x in zip(headers,row)})
        finally: wb.close()
    elif path.suffix.lower() in ('.csv','.tsv','.txt','.crif'):
        text=path.read_text(encoding='utf-8-sig')
        if not text.strip(): raise ValueError('Empty CRIF')
        first=text.splitlines()[0]
        delim='\t' if '\t' in first else ','
        reader=csv.DictReader(io.StringIO(text),delimiter=delim)
        headers=reader.fieldnames or []
        rows=list(reader)
        if any(None in row or any(v is None for v in row.values()) for row in rows): raise ValueError('Inconsistent CSV field count')
    else: raise ValueError('Supported inputs: UTF-8 CSV/TSV/CRIF and single-sheet XLSX. XLS needs explicit conversion.')
    if len(headers)!=len(set(headers)): raise ValueError('Duplicate column names')
    if any(h!=h.strip() for h in headers): raise ValueError('Column whitespace requires source correction/approved mapping')
    return headers,rows

def validate(headers,raw_rows,package,context):
    result=ValidationResult()
    def issue(sev,code,msg,rid=None,field=None):
        result.issues.append(Issue(sev,code,msg,rid,field,SOURCE))
    missing=set(REQUIRED)-set(headers)
    if missing:
        issue('Critical','MISSING_COLUMNS',f'Missing columns: {sorted(missing)}');return result
    if context.get('direction') not in ('collect','post'):
        issue('Critical','DIRECTION_REQUIRED','Explicit collect/post direction is required');return result
    if not context.get('regulation'):
        issue('Critical','REGULATION_REQUIRED','Explicit regulation is required');return result
    if context.get('crif_profile')!='public_rds_1_36_methodology_2_8':
        issue('Critical','PROFILE_REQUIRED','Select the documented shadow CRIF profile explicitly');return result
    issue('Warning','LATEST_RDS_UNVERIFIED','Current licensed Risk Data Standards/official tests unavailable. SHADOW only.')
    currencies=set(context.get('currency_codes',[]))
    if not currencies:
        issue('Critical','CURRENCY_REGISTRY_REQUIRED','Supply a sourced currency registry');return result
    p=package.data
    seen=set(); qualifier_buckets={}; factor_rows={}; implied_fx={}
    for index,raw in enumerate(raw_rows,2):
        rid=f'row:{index}'
        row={k:str(v) for k,v in raw.items()}
        start=len(result.issues)
        if any(v!=v.strip() for v in row.values()):
            issue('Critical','WHITESPACE','Whitespace requires source correction; no automatic trimming',rid)
        fingerprint=json.dumps(row,sort_keys=True,ensure_ascii=False)
        if fingerprint in seen: issue('Review Required','EXACT_DUPLICATE','Exact duplicate row may double-count risk; confirm at source',rid)
        seen.add(fingerprint)
        if row.get('IMModel','SIMM') != 'SIMM':
            issue('Critical','UNSUPPORTED_MODEL','Schedule or unknown IMModel must be separated explicitly',rid,'IMModel')
        if row.get('SIMMVersion') and row['SIMMVersion']!=package.version:
            issue('Critical','VERSION_MISMATCH','Input SIMM version differs from selected package',rid,'SIMMVersion')
        if row.get('ValuationDate'):
            try: date.fromisoformat(row['ValuationDate'])
            except ValueError: issue('Critical','INVALID_DATE','ValuationDate must be ISO YYYY-MM-DD',rid,'ValuationDate')
            if row['ValuationDate']!=context.get('valuation_date'): issue('Critical','DATE_MISMATCH','Input and requested valuation dates differ',rid)
        rt=row['RiskType']; pc=row['ProductClass']; q=row['Qualifier']; bucket=row['Bucket']; l1=row['Label1']; l2=row['Label2']
        if rt not in TYPES and rt not in PARAM_TYPES: issue('Critical','UNKNOWN_RISK_TYPE',f'Unsupported RiskType {rt}',rid,'RiskType')
        expected_unit=p.get('input_units',{}).get(rt)
        if expected_unit:
            declaration=context.get('unit_declarations',{}).get(rt,{})
            row_units=[row.get(name) for name in ('Unit','SensitivityUnit') if row.get(name)]
            declared=declaration.get('unit') if isinstance(declaration,dict) else None
            provenance=declaration.get('source') if isinstance(declaration,dict) else None
            if not provenance or not declared:
                issue('Review Required','UNIT_EVIDENCE_REQUIRED','Explicit unit declaration and provenance required for this local shadow profile',rid,'Unit')
            elif declared!=expected_unit or any(unit!=expected_unit for unit in row_units):
                issue('Critical','UNIT_MISMATCH','Unit differs from sourced sensitivity convention; no automatic scaling or conversion',rid,'Unit')
        if pc not in p['product_classes'] and not (rt in PARAM_TYPES and pc==''): issue('Critical','INVALID_PRODUCT_CLASS','Product class is not allowed',rid,'ProductClass')
        if not q and rt!='AddOnFixedAmount': issue('Critical','MISSING_QUALIFIER','Qualifier is required',rid,'Qualifier')
        dim=rt.startswith('Param_')
        try:
            amount=decimal(row['Amount']); usd=decimal(row['AmountUSD'])
        except (ValueError,InvalidOperation):
            issue('Critical','INVALID_AMOUNT','Amount and AmountUSD must be finite numeric values',rid); continue
        ac=row['AmountCurrency']
        if dim:
            if amount!=usd: issue('Critical','PARAM_AMOUNT_MISMATCH','Dimensionless AmountUSD must equal Amount',rid)
        else:
            if ac not in currencies: issue('Critical','INVALID_CURRENCY','AmountCurrency not in the sourced registry',rid,'AmountCurrency')
            if ac=='USD' and usd!=amount: issue('Critical','USD_AMOUNT_MISMATCH','USD Amount must exactly equal AmountUSD; no tolerance inferred',rid)
            if ac!='USD':
                if (amount==0)!=(usd==0) or amount*usd<0: issue('Critical','FX_SIGN_OR_ZERO','Amount and USD conversion disagree on sign/zero',rid)
                if amount:
                    issue('Warning','FX_RATE_UNVERIFIED','AmountUSD is used as supplied by the CRIF producer; this model performs and checks no FX conversion',rid)
        if rt in TYPES:
            rc,component=TYPES[rt]
            if rc=='InterestRate' or rt=='Risk_FX':
                if q not in currencies: issue('Critical','INVALID_QUALIFIER_CURRENCY','Qualifier must be a sourced ISO currency',rid,'Qualifier')
            if rt=='Risk_FXVol':
                if len(q)!=6 or q[:3] not in currencies or q[3:] not in currencies or q[:3]==q[3:]: issue('Critical','INVALID_FX_PAIR','FX qualifier needs two distinct ISO currencies',rid)
            if rt=='Risk_IRCurve':
                expected={'regular':'1','low':'2','high':'3'}[package.ir_group(q)]
                if bucket!=expected: issue('Critical','IR_BUCKET_MISMATCH',f'Currency requires CRIF category {expected}',rid,'Bucket')
                allowed=['OIS','Libor1m','Libor3m','Libor6m','Libor12m']+(['Prime','Municipal'] if q=='USD' else [])
                if l2 not in allowed: issue('Review Required','CURVE_MAPPING','Unknown curve requires approved closest-equivalent mapping',rid,'Label2')
            elif rc in ('CreditQualifying','CreditNonQualifying','Equity','Commodity') and component!='BaseCorr':
                if bucket not in p[rc]['rw']: issue('Critical','INVALID_BUCKET',f'Unsupported {rc} bucket',rid,'Bucket')
            elif bucket: issue('Critical','UNUSED_BUCKET','Bucket must be blank for this RiskType',rid,'Bucket')
            tenor_types=('Risk_IRCurve','Risk_IRVol','Risk_InflationVol','Risk_CreditQ','Risk_CreditNonQ','Risk_CreditVol','Risk_CreditVolNonQ','Risk_EquityVol','Risk_CommodityVol','Risk_FXVol')
            if rt in tenor_types:
                allowed=p['credit_tenors'] if rc.startswith('Credit') else p['tenors']
                if l1 not in allowed: issue('Critical','INVALID_TENOR','Non-standard tenor needs explicit approved regridding, not automatic repair',rid,'Label1')
            elif l1: issue('Critical','UNUSED_LABEL1','Label1 must be blank',rid,'Label1')
            if rt!='Risk_IRCurve' and l2:
                if rt=='Risk_CreditQ' and l2=='Sec': pass
                else: issue('Review Required','UNCONFIRMED_LABEL2','Latest Label2/payment-currency/group encoding needs current RDS evidence',rid,'Label2')
            if rc.startswith('Credit') and component!='BaseCorr':
                if row.get('PaymentCurrency') not in currencies: issue('Review Required','PAYMENT_CURRENCY_REQUIRED','Explicit PaymentCurrency extension is required; AmountCurrency is not payment currency',rid)
                if rc=='CreditNonQualifying' and not row.get('GroupName'): issue('Review Required','NQ_GROUP_REQUIRED','Explicit GroupName extension is needed for same/different group correlation',rid)
            if rc=='Equity' and bucket not in ('11','12') or rt=='Risk_CreditQ':
                if not re.fullmatch(r'ISIN:[A-Z]{2}[A-Z0-9]{9}[0-9]',q): issue('Review Required','QUALIFIER_MAPPING','Representative ISIN/issuer mapping needs verification',rid,'Qualifier')
            if rc not in ('InterestRate','FX') and component!='BaseCorr':
                key=(pc,rc,q)
                if key in qualifier_buckets and qualifier_buckets[key]!=bucket: issue('Critical','INCONSISTENT_BUCKET','Same qualifier is assigned conflicting buckets',rid)
                qualifier_buckets[key]=bucket
        if rt in PARAM_TYPES:
            if bucket or l1 or l2: issue('Critical','ADDON_LABELS','Add-on fields Bucket/Label1/Label2 must be blank',rid)
            if rt=='Param_ProductClassMultiplier' and (q not in p['product_classes'] or amount<1): issue('Critical','INVALID_MULTIPLIER','Multiplier product class must be valid and value >= 1',rid)
            if rt not in ('Param_ProductClassMultiplier','Notional') and usd<0: issue('Critical','NEGATIVE_ADDON','Factor/fixed add-on cannot be negative',rid)
            if rt=='Param_ProductClassMultiplier' and pc and pc!=q: issue('Critical','MULTIPLIER_CLASS_MISMATCH','Qualifier and ProductClass disagree',rid)
            if rt=='AddOnFixedAmount': issue('Review Required','FIXED_ADDON_ENCODING','Fixed amount CRIF encoding is not verified; use sourced configuration instead',rid)
        scope_values=[]
        for field,key in [('Counterparty','counterparty'),('LegalEntity','legal_entity'),('NettingSet','netting_set'),('CSA','csa')]:
            value=row.get(field) or context.get(key,'')
            if not value: issue('Review Required','SCOPE_REQUIRED',f'{field} must be supplied explicitly',rid,field)
            scope_values.append(value)
        scope=Scope(*scope_values,context['direction'],context['regulation'])
        reg_col='CollectRegulations' if context['direction']=='collect' else 'PostRegulations'
        excluded=False
        if reg_col in headers:
            regs=row.get(reg_col,'').replace('[','').replace(']','').split(',')
            regs=[x.strip() for x in regs if x.strip()]
            if context['regulation'] not in regs: excluded=True
        if excluded: result.excluded.append({'row_id':rid,'reason':reg_col+' does not include selected regulation','original':row})
        if any(x.severity in ('CRITICAL','REVIEW_REQUIRED') for x in result.issues[start:]): continue
        if excluded: continue
        rr=RiskRow(rid,scope,pc,rt,q,bucket,l1,l2,usd,row.get('PaymentCurrency',''),row.get('GroupName',''),row)
        result.rows.append(rr)
        factor=(scope,pc,rt,q,bucket,l1,l2,rr.payment_currency,rr.group_name)
        factor_rows.setdefault(factor,[]).append(rid)
    for factor,ids in factor_rows.items():
        if len(ids)>1: issue('Warning','NETTED_ROWS',f'{len(ids)} rows share a factor and will be explicitly summed: {ids}',ids[0])
    if not raw_rows: issue('Critical','EMPTY_PORTFOLIO','No input rows')
    return result
