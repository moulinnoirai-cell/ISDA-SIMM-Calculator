from decimal import localcontext
from .numeric import D, decimal, PRECISION, sqrt
from .models import plain

def standardized_shortfall(raw_shortfall,raw_simm,full_simm,horizon_days):
    """Remediation Annex section 6 pp4-5. Inputs must use concentration-disabled SIMM."""
    with localcontext() as ctx:
        ctx.prec=PRECISION
        short,raw,full=map(decimal,(raw_shortfall,raw_simm,full_simm))
        if raw<0 or full<0 or horizon_days not in (1,10):raise ValueError('Unsupported/negative SIMM or horizon')
        return short*full/raw if raw else short*(sqrt(D(10)) if horizon_days==1 else 1)

def monitoring_thresholds(simm_eur,shortfall_eur,nonzero_collected_collateral,traffic_light,rnis=False):
    with localcontext() as ctx:
        ctx.prec=PRECISION
        simm,short=map(decimal,(simm_eur,shortfall_eur))
        if simm<0 or traffic_light not in ('Green','Amber','Red'):raise ValueError('Invalid governance inputs')
        monitor=D(0) if nonzero_collected_collateral else D(25000000)
        red=max(D(10000000),D('0.15')*simm);amber=D(100000000);remediation=max(D(25000000),D('0.15')*simm)
        return plain({'monitoring_required':simm>monitor,'monitoring_threshold_eur':monitor,'red_reporting_threshold_eur':red,'amber_reporting_threshold_eur':amber,
            'report_shortfall':traffic_light=='Red' and short>red or traffic_light=='Amber' and short>amber,
            'remediation_required':traffic_light=='Red' and short>remediation,'remediation_threshold_eur':remediation,'source':'Remediation Annex 2025-10-22 sections 2-3 p2; section 5.2.2 p4',
            'traffic_light_origin':'Caller-supplied independent 1+3/Actual PnL test. This function does not perform backtesting.','rnis':rnis})
