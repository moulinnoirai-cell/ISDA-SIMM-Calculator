from decimal import Decimal, localcontext
from functools import lru_cache

D = Decimal
ZERO, ONE = D(0), D(1)
PRECISION = 50

def decimal(value):
    result = D(str(value))
    if not result.is_finite():
        raise ValueError('Non-finite number')
    if result.adjusted() > 100 or result.adjusted() < -100:
        if result != 0: raise ValueError('Number outside supported exponent range [-100,100]')
    return result

def sqrt(value):
    if value < 0:
        raise ArithmeticError('Negative quadratic form; inspect inputs/correlation/precision')
    return value.sqrt()

def clamp(value, bound):
    return max(-bound,min(value,bound))

@lru_cache(maxsize=4)
def normal_quantile(probability):
    """Decimal inverse CDF, bisection and convergent erf series. B.10/11."""
    with localcontext() as ctx:
        ctx.prec = PRECISION + 12
        p = D(probability)
        if not D('0.5') < p < ONE: raise ValueError('Supported quantile interval (0.5,1)')
        # Pi via Gauss-Legendre, not a binary floating-point constant.
        a,b,t,n = ONE,ONE/D(2).sqrt(),D('0.25'),ONE
        for _ in range(8):
            an=(a+b)/2; b=(a*b).sqrt(); t-=n*(a-an)**2; a=an; n*=2
        pi=(a+b)**2/(4*t)
        root_pi=pi.sqrt()
        def cdf(x):
            z=x/D(2).sqrt(); term=z; total=z; k=0
            while True:
                k+=1; term*=-(z*z)/k; add=term/(2*k+1); total+=add
                if abs(add) < D('1e-60'): break
                if k > 1000: raise ArithmeticError('CDF did not converge')
            return (ONE+2*total/root_pi)/2
        lo,hi=ZERO,D(6)
        for _ in range(210):
            mid=(lo+hi)/2
            if cdf(mid)<p: lo=mid
            else: hi=mid
        ctx.prec=PRECISION
        return +(lo+hi)/2

def quadratic(items, value, correlation):
    total=sum((value(x)**2 for x in items),ZERO)
    for i,a in enumerate(items):
        for b in items[i+1:]:
            total+=2*correlation(a,b)*value(a)*value(b)
    return total
