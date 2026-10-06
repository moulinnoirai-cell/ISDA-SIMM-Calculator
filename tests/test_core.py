"""Synthetic mathematical checks; not official ISDA unit tests."""
import copy
import csv
import json
import math
import tempfile
import unittest
from decimal import Decimal as D, localcontext
from pathlib import Path
from statistics import NormalDist

from simm.crif import REQUIRED, read_crif, validate
from simm.engine import Engine
from simm.models import Issue, Scope, RiskRow, ValidationResult
from simm.numeric import normal_quantile, decimal, quadratic, sqrt
from simm.parameters import ParameterPackage

ROOT=Path(__file__).resolve().parents[1]
SCOPE=Scope('SYNTHETIC_CP','SYNTHETIC_ENTITY','SYNTHETIC_NETTING','SYNTHETIC_CSA','collect','SYNTHETIC_REG')

def row(rt='Risk_Commodity',amount='100',bucket='1',q='SYNTHETIC_COAL',tenor='',label2='',pc='Commodity',rid='row:2',scope=SCOPE,payment='',group=''):
    return RiskRow(rid,scope,pc,rt,q,bucket,tenor,label2,D(amount),payment,group,{'synthetic':True,'AmountUSD':amount})

def raw(**changes):
    data=dict(zip(REQUIRED,['Commodity','Risk_Commodity','SYNTHETIC_COAL','1','','','100','USD','100']))
    data.update(changes);return data

def context(**changes):
    data={'direction':'collect','regulation':'SYNTHETIC_REG','crif_profile':'public_rds_1_36_methodology_2_8',
          'currency_codes':['USD','EUR','JPY','KRW','ARS'],'counterparty':'SYNTHETIC_CP','legal_entity':'SYNTHETIC_ENTITY',
          'netting_set':'SYNTHETIC_NETTING','csa':'SYNTHETIC_CSA','valuation_date':'2026-10-02'}
    data['unit_declarations']={k:{'unit':v,'source':'SYNTHETIC_FIXTURE conforms to sourced shadow unit conventions'} for k,v in ParameterPackage().data['input_units'].items()}
    data.update(changes);return data

def run(rows,**config):
    return Engine(ParameterPackage()).calculate(ValidationResult(rows=rows),config)

def component(result,rc,name):
    return D(next(r['components'][name] for c in result['counterparties'] for s in c['netting_sets']
                  for p in s['products'] for r in p['risk_classes'] if r['risk_class']==rc))

class NumericTests(unittest.TestCase):
    def test_finite_numbers(self):
        for value in ('NaN','Infinity','-Infinity','1e101','1e-101'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):decimal(value)
        self.assertEqual(decimal('1.2345678901234567890123456789'),D('1.2345678901234567890123456789'))

    def test_quantiles_independent_float_reference(self):
        for probability in ('0.99','0.995'):
            self.assertLess(abs(float(normal_quantile(probability))-NormalDist().inv_cdf(float(probability))),2e-14)

    def test_negative_quadratic_not_clipped(self):
        with self.assertRaises(ArithmeticError):sqrt(D('-1e-40'))

class ParameterTests(unittest.TestCase):
    def test_package_integrity(self):
        package=ParameterPackage()
        self.assertEqual(package.manifest['sha256'],package.hash)
        self.assertEqual(package.data['effective_date'],'2026-07-11')
        self.assertFalse(package.manifest['production_approved'])
        self.assertEqual(package.manifest['official_isda_unit_tests'],'NOT_RUN')
        self.assertTrue(package.manifest['source_url'].startswith('https://www.isda.org/'))

    def test_source_transcribed_risk_class_table(self):
        # Independent manually checked transcription from supplied K.88 p29.
        expected=[['1','.09','.07','.11','.34','.15'],['.09','1','.62','.61','.35','.25'],
                  ['.07','.62','1','.47','.28','.14'],['.11','.61','.47','1','.41','.25'],
                  ['.34','.35','.28','.41','1','.28'],['.15','.25','.14','.25','.28','1']]
        p=ParameterPackage().data
        for i,a in enumerate(p['risk_classes']):
            for j,b in enumerate(p['risk_classes']):
                self.assertEqual(D(p['risk_class_corr'][a][b]),D(expected[i][j]))

    def test_source_transcribed_weights_and_thresholds(self):
        # Supplied D.32-37 / E.39 / F.45 / G.52 / H.62 / I.66 / J.74-87.
        p=ParameterPackage().data
        self.assertEqual([D(p['InterestRate']['rw']['regular'][t]) for t in p['tenors']],list(map(D,[107,101,90,69,68,69,66,61,60,58,58,66])))
        self.assertEqual(D(p['CreditQualifying']['rw']['Residual']),D(423))
        self.assertEqual(D(p['CreditNonQualifying']['rw']['2']),D(2700))
        self.assertEqual(D(p['Equity']['rw']['12']),D(15))
        self.assertEqual(D(p['Commodity']['rw']['17']),D(16))
        self.assertEqual(D(p['FX']['rw']['regular']['regular']),D('7.4'))
        self.assertEqual(D(p['InterestRate']['ct']['well_traded']),D(220000000))
        self.assertEqual(D(p['Equity']['vt']['12']),D(3800000000))
        self.assertEqual(D(p['CreditNonQualifying']['vt']['Residual']),D(2100000))

    def test_asymmetric_matrix_rejected(self):
        p=copy.deepcopy(ParameterPackage().data);p['risk_class_corr']['FX']['Equity']='.9'
        with self.assertRaises(ValueError):ParameterPackage(data=p)

    def test_threshold_zero_rejected(self):
        p=copy.deepcopy(ParameterPackage().data);p['FX']['ct']['1']='0'
        with self.assertRaises(ValueError):ParameterPackage(data=p)

    def test_unsupported_methodology(self):
        p=copy.deepcopy(ParameterPackage().data);p['methodology_id']='simm_unknown'
        with self.assertRaises(ValueError):ParameterPackage(data=p)
        with self.assertRaises((ValueError,FileNotFoundError)):ParameterPackage('2.4')

    def test_historical_packages_load(self):
        for version,effective in [('2.5','2022-12-03'),('2.5A','2023-07-15'),('2.6','2023-12-02'),('2.7','2024-12-07'),('2.7+2412','2025-07-12'),('2.8+2506','2025-12-06')]:
            with self.subTest(version=version):
                package=ParameterPackage(version)
                self.assertEqual(package.manifest['sha256'],package.hash)
                self.assertEqual(package.data['effective_date'],effective)
                self.assertFalse(package.manifest['production_approved'])

class ValidationTests(unittest.TestCase):
    def setUp(self):self.p=ParameterPackage()
    def checked(self,data=None,ctx=None,headers=None):
        data=data if data is not None else [raw()]
        return validate(headers or list(data[0]),data,self.p,ctx or context())
    def test_canonical_statuses(self):
        self.assertEqual(ValidationResult().status,'VALID')
        self.assertEqual(self.checked().status,'WARNING')
        self.assertFalse(self.checked().blocked)
        for old,current in [('Critical','CRITICAL'),('Warning','WARNING'),('Review Required','REVIEW_REQUIRED')]:
            self.assertEqual(Issue(old,'synthetic','synthetic').severity,current)
    def test_missing_column(self):
        v=self.checked(headers=[h for h in REQUIRED if h!='AmountUSD'])
        self.assertTrue(v.blocked);self.assertEqual(v.status,'CRITICAL')
    def test_invalid_numbers(self):
        for value in ('NaN','Infinity','bad'):
            self.assertTrue(self.checked([raw(Amount=value,AmountUSD=value)]).blocked)
    def test_unknown_type_and_bad_bucket(self):
        for changes in [{'RiskType':'Risk_unknown'},{'Bucket':'18'},{'ProductClass':'Unknown'}]:
            self.assertTrue(self.checked([raw(**changes)]).blocked)
    def test_unit_mismatch_and_missing_provenance(self):
        v=self.checked([raw(Unit='DELTA_PER_1PERCENT_RELATIVE')]);self.assertFalse(v.blocked)
        v=self.checked([raw(Unit='RAW_DERIVATIVE_PER_UNIT')]);self.assertTrue(v.blocked)
        self.assertIn('UNIT_MISMATCH',[x.code for x in v.issues])
        v=self.checked(ctx=context(unit_declarations={}));self.assertTrue(v.blocked)
        self.assertEqual(v.status,'REVIEW_REQUIRED')
        self.assertIn('UNIT_EVIDENCE_REQUIRED',[x.code for x in v.issues])
    def test_whitespace_and_version(self):
        self.assertTrue(self.checked([raw(Qualifier=' SYNTHETIC_COAL')]).blocked)
        self.assertTrue(self.checked([raw(SIMMVersion='2.6')]).blocked)
    def test_exact_duplicate_requires_review(self):
        v=self.checked([raw(),raw()]);self.assertTrue(v.blocked)
        self.assertIn('EXACT_DUPLICATE',[x.code for x in v.issues])
    def test_same_factor_distinct_trade_sums(self):
        v=self.checked([raw(TradeID='SYNTHETIC_1'),raw(TradeID='SYNTHETIC_2')])
        self.assertFalse(v.blocked);self.assertEqual(len(v.rows),2)
        self.assertIn('NETTED_ROWS',[x.code for x in v.issues])
    def test_currency_sign_and_explicit_fx(self):
        self.assertTrue(self.checked([raw(Amount='100',AmountUSD='101')]).blocked)
        self.assertTrue(self.checked([raw(AmountCurrency='ZZZ')]).blocked)
        self.assertTrue(self.checked([raw(AmountCurrency='EUR',AmountUSD='-100')]).blocked)
        v=self.checked([raw(AmountCurrency='EUR',AmountUSD='110')])
        self.assertFalse(v.blocked)
        self.assertIn('FX_RATE_UNVERIFIED',[x.code for x in v.issues])
        self.assertEqual(v.rows[0].amount_usd,D('110'))
    def test_scope_direction_and_profile_required(self):
        for changes in [{'counterparty':''},{'direction':'unknown'},{'crif_profile':''},{'currency_codes':[]}]:
            self.assertTrue(self.checked(ctx=context(**changes)).blocked)
    def test_credit_payment_not_amount_currency(self):
        credit=raw(ProductClass='Credit',RiskType='Risk_CreditQ',Qualifier='ISIN:US0000000001',Label1='5y')
        v=self.checked([credit]);self.assertTrue(v.blocked)
        self.assertIn('PAYMENT_CURRENCY_REQUIRED',[x.code for x in v.issues])
        credit['PaymentCurrency']='USD';self.assertFalse(self.checked([credit]).blocked)
    def test_nonqualifying_group_required(self):
        credit=raw(ProductClass='Credit',RiskType='Risk_CreditNonQ',Qualifier='SYNTHETIC_TRANCHE',Label1='5y',PaymentCurrency='USD')
        self.assertTrue(self.checked([credit]).blocked)
        credit['GroupName']='SYNTHETIC_GROUP';self.assertFalse(self.checked([credit]).blocked)
    def test_curve_bucket_and_tenor(self):
        ir=raw(ProductClass='RatesFX',RiskType='Risk_IRCurve',Qualifier='USD',Label1='1y',Label2='OIS')
        self.assertFalse(self.checked([ir]).blocked)
        for changes in [{'Bucket':'2'},{'Label1':'4y'},{'Label2':'Unknown'}]:
            self.assertTrue(self.checked([{**ir,**changes}]).blocked)
    def test_regulation_exclusion_preserved(self):
        v=self.checked([raw(CollectRegulations='OTHER_REG')])
        self.assertEqual(len(v.excluded),1);self.assertEqual(len(v.rows),0)
        self.assertEqual(v.excluded[0]['original']['AmountUSD'],'100')
    def test_csv_schema_rejects_duplicates_and_truncated_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'synthetic.csv'
            for text in ('Amount,Amount\n1,2\n','Amount,AmountUSD\n1\n'):
                path.write_text(text,encoding='utf-8')
                with self.assertRaises(ValueError):read_crif(path)
    def test_csv_original_values_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'synthetic.csv';r=raw(Amount='1.12345678901234567890',AmountUSD='1.12345678901234567890')
            with path.open('w',encoding='utf-8',newline='') as f:
                w=csv.DictWriter(f,list(r));w.writeheader();w.writerow(r)
            headers,rows=read_crif(path);self.assertEqual(rows[0],r)
            self.assertFalse(validate(headers,rows,self.p,context()).blocked)

class FormulaTests(unittest.TestCase):
    def close(self,actual,expected,relative='1e-45'):
        expected=D(expected);self.assertLessEqual(abs(D(actual)-expected),max(D('1e-45'),abs(expected)*D(relative)))

    def test_single_delta_all_six_classes(self):
        fixtures=[('InterestRate',row('Risk_IRCurve',bucket='1',q='USD',tenor='1y',label2='OIS',pc='RatesFX'),'6800'),
                  ('CreditQualifying',row('Risk_CreditQ',q='SYNTHETIC_ISSUER',tenor='5y',pc='Credit',payment='USD'),'6800'),
                  ('CreditNonQualifying',row('Risk_CreditNonQ',q='SYNTHETIC_TRANCHE',tenor='5y',pc='Credit',payment='USD',group='SYNTHETIC_GROUP'),'21000'),
                  ('Equity',row('Risk_Equity',q='SYNTHETIC_EQUITY',pc='Equity'),'2900'),
                  ('Commodity',row(),'2200'),('FX',row('Risk_FX',bucket='',q='EUR',pc='RatesFX'),'740')]
        for rc,r,expected in fixtures:
            with self.subTest(risk_class=rc):self.close(component(run([r]),rc,'Delta'),expected)

    def test_concentration_threshold_boundary(self):
        # Coal bucket1 CT=310 million USD; RW=22 (H.62/J.77).
        with localcontext() as ctx:
            ctx.prec=60
            for amount in ('309999999','310000000','1240000000'):
                expected=D(amount)*22*max(D(1),(D(amount)/310000000).sqrt())
                self.close(run([row(amount=amount)])['total_simm'],expected)

    def test_ir_excludes_cross_currency_basis_from_concentration(self):
        r=row('Risk_XCcyBasis',amount='10000000000',bucket='',q='USD',pc='RatesFX')
        result=run([r]);self.close(component(result,'InterestRate','Delta'),'210000000000')
        factor=next(n for n in result['trace'] if n['level']=='RiskFactor' and n['component']=='Delta')
        self.assertEqual(factor['concentration'],'1');self.assertTrue(factor['excluded_from_concentration'])

    def test_usd_fx_has_zero_margin(self):
        self.close(run([row('Risk_FX',bucket='',q='USD',pc='RatesFX',amount='1e15')])['total_simm'],'0')

    def test_residual_adds_without_correlation(self):
        rows=[row('Risk_Equity',q='SYNTHETIC_A',pc='Equity'),row('Risk_Equity',bucket='Residual',q='SYNTHETIC_B',pc='Equity',rid='row:3')]
        self.close(component(run(rows),'Equity','Delta'),'6700')

    def test_intrabucket_sign_hedge(self):
        rows=[row(q='SYNTHETIC_A'),row(q='SYNTHETIC_B',amount='-100',rid='row:3')]
        with localcontext() as ctx:
            ctx.prec=60
            expected=(2*D(2200)**2*(1-D('.72'))).sqrt()
            self.close(component(run(rows),'Commodity','Delta'),expected)

    def test_credit_payment_currency_separate_factors(self):
        rows=[row('Risk_CreditQ',q='SYNTHETIC_ISSUER',tenor='5y',pc='Credit',payment='USD'),
              row('Risk_CreditQ',q='SYNTHETIC_ISSUER',tenor='5y',pc='Credit',payment='EUR',amount='-100',rid='row:3')]
        with localcontext() as ctx:
            ctx.prec=60
            self.close(component(run(rows),'CreditQualifying','Delta'),(2*D(6800)**2*(1-D('.92'))).sqrt())

    def test_credit_nonqualifying_group_correlation(self):
        r=row('Risk_CreditNonQ',q='SYNTHETIC_A',tenor='5y',pc='Credit',payment='USD',group='SYNTHETIC_GROUP')
        s=row('Risk_CreditNonQ',q='SYNTHETIC_B',tenor='5y',pc='Credit',payment='USD',group='SYNTHETIC_GROUP',rid='row:3')
        with localcontext() as ctx:
            ctx.prec=60
            self.close(component(run([r,s]),'CreditNonQualifying','Delta'),(2*D(21000)**2*(1+D('.86'))).sqrt())

    def test_basecorr_family_netting(self):
        rows=[row('Risk_BaseCorr',bucket='',q='SYNTHETIC_CDX',pc='Credit'),
              row('Risk_BaseCorr',bucket='',q='SYNTHETIC_CDX',pc='Credit',amount='-100',rid='row:3')]
        self.close(component(run(rows),'CreditQualifying','BaseCorr'),'0')
        rows[1].qualifier='SYNTHETIC_ITRAXX'
        with localcontext() as ctx:
            ctx.prec=60
            self.close(component(run(rows),'CreditQualifying','BaseCorr'),(2*D(970)**2*(1-D('.1'))).sqrt())

    def test_vega_all_six_classes(self):
        # Independent Python math oracle. 2e-14 is numerical-test precision, not an IM reconciliation tolerance.
        z=NormalDist().inv_cdf(.99);scale=math.sqrt(365/14)/z
        fixtures=[('InterestRate',row('Risk_IRVol',bucket='',q='USD',tenor='1y',pc='RatesFX'),20),
                  ('CreditQualifying',row('Risk_CreditVol',q='SYNTHETIC_ISSUER',tenor='1y',pc='Credit',payment='USD'),42),
                  ('CreditNonQualifying',row('Risk_CreditVolNonQ',q='SYNTHETIC_TRANCHE',tenor='1y',pc='Credit',payment='USD',group='SYNTHETIC_GROUP'),42),
                  ('Equity',row('Risk_EquityVol',q='SYNTHETIC_A',tenor='1y',pc='Equity'),100*29*scale*.56*.29),
                  ('Commodity',row('Risk_CommodityVol',tenor='1y'),100*22*scale*.81*.34),
                  ('FX',row('Risk_FXVol',bucket='',q='EURUSD',tenor='1y',pc='RatesFX'),100*7.4*scale*.67*.33)]
        for rc,r,expected in fixtures:
            with self.subTest(risk_class=rc):self.assertTrue(math.isclose(float(component(run([r]),rc,'Vega')),expected,rel_tol=2e-14))

    def test_curvature_single_positive_and_negative(self):
        z=NormalDist().inv_cdf(.995)
        r=row('Risk_IRVol',bucket='',q='USD',tenor='1y',pc='RatesFX')
        expected=100*.5*14/365*z*z/(.74*.74)
        self.assertTrue(math.isclose(float(component(run([r]),'InterestRate','Curvature')),expected,rel_tol=2e-14))
        r.amount_usd=D(-100);self.close(component(run([r]),'InterestRate','Curvature'),'0')

    def test_curvature_single_positive_all_six_classes(self):
        z=NormalDist().inv_cdf(.995);alpha=NormalDist().inv_cdf(.99)
        sigma=math.sqrt(365/14)/alpha;sf=.5*14/365
        fixtures=[('InterestRate',row('Risk_IRVol',bucket='',q='USD',tenor='1y',pc='RatesFX'),1/(.74*.74)),
                  ('CreditQualifying',row('Risk_CreditVol',q='SYNTHETIC_ISSUER',tenor='1y',pc='Credit',payment='USD'),1),
                  ('CreditNonQualifying',row('Risk_CreditVolNonQ',q='SYNTHETIC_TRANCHE',tenor='1y',pc='Credit',payment='USD',group='SYNTHETIC_GROUP'),1),
                  ('Equity',row('Risk_EquityVol',q='SYNTHETIC_A',tenor='1y',pc='Equity'),29*sigma),
                  ('Commodity',row('Risk_CommodityVol',tenor='1y'),22*sigma),
                  ('FX',row('Risk_FXVol',bucket='',q='EURUSD',tenor='1y',pc='RatesFX'),7.4*sigma)]
        for rc,r,scale in fixtures:
            with self.subTest(risk_class=rc):
                self.assertTrue(math.isclose(float(component(run([r]),rc,'Curvature')),100*sf*z*z*scale,rel_tol=2e-14))

    def test_equity_vol_index_zero_curvature(self):
        result=run([row('Risk_EquityVol',bucket='12',q='SYNTHETIC_VOL_INDEX',tenor='1y',pc='Equity')])
        self.close(component(result,'Equity','Curvature'),'0')
        self.assertGreater(component(result,'Equity','Vega'),0)

    def test_expiry_scaling_before_netting(self):
        rows=[row('Risk_EquityVol',q='SYNTHETIC_A',tenor='2w',pc='Equity'),
              row('Risk_EquityVol',q='SYNTHETIC_A',tenor='1y',amount='-100',pc='Equity',rid='row:3')]
        result=run(rows);self.close(component(result,'Equity','Vega'),'0')
        self.assertGreater(component(result,'Equity','Curvature'),0)

    def test_fx_pair_order_nets(self):
        rows=[row('Risk_FXVol',bucket='',q='EURUSD',tenor='1y',pc='RatesFX'),
              row('Risk_FXVol',bucket='',q='USDEUR',tenor='1y',pc='RatesFX',amount='-100',rid='row:3')]
        self.close(run(rows)['total_simm'],'0')

    def test_product_class_aggregation_and_contributions(self):
        rows=[row('Risk_IRCurve',bucket='1',q='USD',tenor='1y',label2='OIS',pc='RatesFX'),
              row('Risk_FX',bucket='',q='EUR',pc='RatesFX',rid='row:3'),
              row('Risk_Equity',q='SYNTHETIC_A',pc='Equity',rid='row:4')]
        with localcontext() as ctx:
            ctx.prec=60
            expected=(D(6800)**2+D(740)**2+2*D('.15')*6800*740).sqrt()+2900
            result=run(rows);self.close(result['total_simm'],expected)
            product=result['counterparties'][0]['netting_sets'][0]['products'][0]
            self.close(sum(D(r['contribution']) for r in product['risk_classes']),product['margin'])

    def test_addons_and_multiplier(self):
        rows=[row(),row('Param_ProductClassMultiplier',amount='2',bucket='',q='Commodity',rid='row:3'),
              row('Param_AddOnNotionalFactor',amount='3',bucket='',q='SYNTHETIC_NOTIONAL',pc='',rid='row:4'),
              row('Notional',amount='-1000',bucket='',q='SYNTHETIC_NOTIONAL',pc='',rid='row:5')]
        result=run(rows,fixed_addons=[{'scope':SCOPE.__dict__,'amount_usd':'50','source':'SYNTHETIC_AGREEMENT'}])
        self.close(result['total_simm'],'4480')
        with self.assertRaises(ValueError):run(rows[:-1])

    def test_scope_isolation_and_trace(self):
        other=Scope('SYNTHETIC_CP','SYNTHETIC_ENTITY','SYNTHETIC_NETTING_2','SYNTHETIC_CSA_2','collect','SYNTHETIC_REG')
        result=run([row(),row(amount='-100',scope=other,rid='row:3')]);self.close(result['total_simm'],'4400')
        self.assertEqual(len(result['counterparties'][0]['netting_sets']),2)
        nodes={n['id']:n for n in result['trace']};visited=set()
        def walk(nid):
            self.assertIn(nid,nodes)
            if nid in visited:return
            visited.add(nid)
            for child in nodes[nid]['children']:walk(child)
        walk(result['trace_root']);self.assertTrue({'row:2','row:3'}<=visited)
        self.assertTrue(all(n.get('parameters') for n in nodes.values() if n['level']=='RiskFactor'))

    def test_critical_or_review_blocks_calculation(self):
        for severity in ('CRITICAL','REVIEW_REQUIRED'):
            with self.assertRaises(ValueError):Engine(ParameterPackage()).calculate(ValidationResult(issues=[Issue(severity,'SYNTHETIC','SYNTHETIC')],rows=[row()]))

    def test_calculation_repeatability_without_runtime_fields(self):
        rows=[row(),row('Risk_CommodityVol',tenor='1y',rid='row:3')]
        a=run(rows);b=run(rows);a.pop('risk_class_seconds');b.pop('risk_class_seconds')
        self.assertEqual(a,b)
