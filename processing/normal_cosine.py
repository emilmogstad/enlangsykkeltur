from scipy.optimize import fmin_powell


from numpy import array, squeeze, reshape, ones, linspace, sqrt, cos
from numpy import nonzero, ceil, mean, std, pi, matrix, concatenate
from numpy import sum, zeros, cov, sin, exp, log, argmin


from hessian import hessian, gradient
from scipy.stats import norm
from scipy.integrate import quad


def pdf(y, p):
    ''' pdf(y, p) -> value
    Probability density function.'''
    try:
        y = array([y]).squeeze().reshape((1,))
    except:
        y = array(y).squeeze()


    n = y.size
    xi = p[0]; sigma = sqrt(p[1]**2)


    if (len(p) == 2):
        a = zeros(1)
    else:
        a = p[2:]
    
    u = (y - xi)/sigma
    m = len(a)
    j = ones((n, m)) * linspace(1, m, m)


    p1 = norm.pdf(u)/sigma
    p2 = a * (sqrt(2) * cos(j.T * pi * norm.cdf(u))).T


    return p1 * exp(sum(p2, axis=1)) / _km(a)


def _km(a, N=10000):
    '''_km(a) -> value
    Normalizing constant'''
    m = len(a)
    uvec = (linspace(0, 1, N) * ones((m, N))).T
    uvec = exp(sum(a * sqrt(2) * cos(linspace(1, m, m) * pi * uvec), axis=1))
    return N**(-1) * (sum(uvec) - (uvec[0] + uvec[-1])/2)


def loglik(p, *args):
    ''' loglik(p, *args) -> value
    Returns the negative log likelihood. Data vector must be in *args.'''
    dvec = pdf(array(args).squeeze(), p)
    return -sum(log(dvec))


def startvalue(data, m):
    ''' startvalue(data, m) -> vector
    Tries to give a good starting point for optimizing algorithms.'''
    x0 = array([mean(data), std(data)])
    fits = [x0]
    for i in range(1, m+1):
        x0 = concatenate([fits[-1], zeros(1)])
        fits.append(fmin_powell(loglik, x0, args=[data], disp=0))


    return fits


def mle(data, m):
    ''' mle(data, m) -> (fit, log likelihood)
    Fits model to data with m parameters in the log expansion.'''
    if (m == 0):
        fit = array([mean(data), std(data)])
        return fit, -loglik(fit, data)
    else:
        fit0 = startvalue(data, m)[-1]
        ll = loglik(fit0, data)
        augll = lambda p: (loglik(p, data) - ll) * 1e7
        output = fmin_powell(augll, fit0, disp=0, xtol=1e-10, ftol=1e-5, maxiter=1000)
        return output, -loglik(output, data)


def Jwide(p):
    ''' Jwide2(data, p) -> matrix
    Fisher information matrix, evaluated at a = 0'''
    sigma = p[1]; m = len(p) - 2
    J = eye(m+2)
    c, d = zeros(m), zeros(m)
    for j in range(1, m+1):
        covfunc1 = lambda u: u * sqrt(2) * cos(j*pi*norm.cdf(u)) * norm.pdf(u)
        covfunc2 = lambda u: (u**2-1) * sqrt(2) * cos(j*pi*norm.cdf(u)) * norm.pdf(u)


        c[j-1] = quad(covfunc1, -10, 10, epsabs=1e-10)[0]
        d[j-1] = quad(covfunc2, -10, 10, epsabs=1e-10)[0]


    J[0,2:] = c/sigma; J[1,2:] = d/sigma
    J[2:,0] = c/sigma; J[2:,1] = d/sigma
    J[0, 0] = 1.0/sigma**2
    J[1, 1] = 2.0/sigma**2


    return J


def Jwide2(data, p):
    ''' Jwide2(data, p) -> matrix
    Empirical hessian of the log likelihood function'''
    return hessian(loglik, p, args=data, h=1e-4) / len(data)


def Kwide(data, p):
    '''Kwide(data, p) -> matrix
    Empirical covariance matrix of the score function'''
    return cov(dmu_logdens(data, p))


def mode(p, I):
    ''' mode(xi, sigma, a, I) -> vector
    Gives the global maximum of f given a set of parameters.'''
    fneg = lambda y: -pdf(y, p)
    xvec = linspace(I[0], I[1], 200)
    start = xvec[argmin(fneg(xvec))]
    return fmin_powell(fneg, start, full_output=1, disp=0, xtol=1e-8)[0]


def dmu_mode(y, p):
    ''' dmu_mode(y, p) -> value
    The derivative of the mode, with respect to p'''
    n = y.size
    a = p[2:]
    m = len(p) - 2
    j = linspace(1, m, m)
    u1 = (((y - p[0])/p[1]) * ones((m, n))).T
    u2 = (y - p[0]) / p[1]
    h = 1e-4
    
    phi = norm.pdf
    Phi = norm.cdf
    dphi = lambda v: (phi(v+h) - phi(v-h))/(2*h)


    P = sum(a*j*sin(j*pi*Phi(u1)), axis=1)
    denom = 1/sqrt(2) + pi*dphi(u2)*P + (pi*phi(u2))**2 * sum(a*j**2*cos(j*pi*Phi(u1)), axis=1)
    du = ones((m+2, n))
    du[1] = u2
    for k in range(1, m+1):
        du[k+1] = -k*p[1]*pi*phi(u2)*sin(k*pi*Phi(u2)) / denom


    return du


def dmu_dens(y, p):
    ''' dmu_mode(y, p) -> value
    The derivative of the density function at a point y, with respect to p'''
    n = y.size
    a = p[2:]
    m = len(p) - 2
    k = linspace(1, m, m)
    du = zeros((m+2, n))
    
    f = lambda p: pdf(y, p)
    for k in range(m+2):
        h = zeros(m+2); h[k] = 1e-4
        du[k] = (f(p+h) - f(p-h)) / sum(2*h)


    return du


def dmu_logdens(y, p):
    ''' dmu_mode(y, p) -> value
    The derivative of the log density function at a point y, with respect to p'''
    return dmu_dens(y, p) / pdf(y, p)


def rgen(p, n):
    ''' rgen(p, n) -> value
    Random number generator (acceptance-rejection algorithm)'''
    Phi = norm.pdf
    c = exp(sqrt(2)*sum(abs(p[2:]))) / _km(p[2:])


    x = random.normal(p[0], p[1], 2*ceil(c)*n)
    u = random.uniform(0, c*norm.pdf(x), 2*ceil(c)*n)
    b = x[nonzero([u < pdf(x, p)][0] * x)]


    return b[:n]
