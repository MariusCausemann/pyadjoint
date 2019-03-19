"""
Implementation of Burger's equation with nonlinear solve in each
timestep
"""

from fenics import *
from fenics_adjoint import *

from numpy.random import rand, seed
seed(21)

n = 30
mesh = UnitIntervalMesh(n)
V = FunctionSpace(mesh, "CG", 1)

def Dt(u, u_, timestep):
    return (u - u_)/timestep

def main(ic, annotate=False):

    u_ = ic.copy(deepcopy=True)
    u = Function(V, name="VelocityNext")
    v = TestFunction(V)

    nu = Constant(0.0001)

    timestep = Constant(1.0/n)

    F = (Dt(u, u_, timestep)*v
         + u*u.dx(0)*v + nu*u.dx(0)*v.dx(0))*dx
    bc = DirichletBC(V, 0.0, "on_boundary")

    t = 0.0
    end = 0.2
    while (t <= end):
        solve(F == 0, u, bc, annotate=annotate)
        u_.assign(u, annotate=annotate)

        t += float(timestep)

    return u_

if __name__ == "__main__":

    ic = project(Expression("sin(2*pi*x[0])", degree=2),  V)
    forward = main(ic, annotate=True)

    J = assemble(forward*forward*dx + ic*ic*dx)

    Jhat = ReducedFunctional(J, Control(ic))


    h = Function(V)
    h.vector()[:] = 0.1*rand(V.dim())

    Jhat.derivative()
    HJic = Jhat.hessian(h)._ad_dot(h)

    minconv = taylor_test(Jhat, ic, h, Hm=HJic)
    assert minconv > 2.7
