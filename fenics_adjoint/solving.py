import backend
import ufl
from pyadjoint.tape import get_working_tape
from pyadjoint.block import Block
from .types import Function, DirichletBC


def solve(*args, **kwargs):
    annotate_tape = kwargs.pop("annotate_tape", True)

    if annotate_tape:
        tape = get_working_tape()
        block = SolveBlock(*args, **kwargs)
        tape.add_block(block)

        block_output = args[1].create_block_output()
        block.add_output(block_output)

    output = backend.solve(*args, **kwargs)
    return output


class SolveBlock(Block):
    def __init__(self, *args, **kwargs):
        super(SolveBlock, self).__init__()
        if isinstance(args[0], ufl.equation.Equation):
            # Variational problem.
            eq = args[0]
            self.lhs = eq.lhs
            self.rhs = eq.rhs
            self.func = args[1]

            # Store boundary conditions in a list.
            if len(args) > 2:
                if isinstance(args[2], list):
                    self.bcs = args[2]
                else:
                    self.bcs = [args[2]]
            else:
                self.bcs = []

            if isinstance(self.lhs, ufl.Form) and isinstance(self.rhs, ufl.Form):
                self.linear = True
                # Add dependence on coefficients on the right hand side.
                for c in self.rhs.coefficients():
                    self.add_dependency(c.get_block_output())
            else:
                self.linear = False

            for bc in self.bcs:
                self.add_dependency(bc.get_block_output())

            for c in self.lhs.coefficients():
                self.add_dependency(c.get_block_output())

            #self.add_output(self.func.create_block_output())
        else:
            # Linear algebra problem.
            raise NotImplementedError

    def __str__(self):
        return "{} = {}".format(str(self.lhs), str(self.rhs))

    def evaluate_adj(self):
        fwd_block_output = self.get_outputs()[0]
        u = fwd_block_output.get_output()
        V = u.function_space()
        adj_var = Function(V)

        if self.linear:
            tmp_u = Function(self.func.function_space()) # Replace later? Maybe save function space on initialization.
            F_form = backend.action(self.lhs, tmp_u) - self.rhs
        else:
            tmp_u = self.func
            F_form = self.lhs

        replaced_coeffs = {}
        for block_output in self.get_dependencies():
            coeff = block_output.get_output()
            if coeff in F_form.coefficients():
                replaced_coeffs[coeff] = block_output.get_saved_output()

        replaced_coeffs[tmp_u] = fwd_block_output.get_saved_output()

        F_form = backend.replace(F_form, replaced_coeffs)

        # Obtain (dFdu)^T.
        dFdu = backend.derivative(F_form, fwd_block_output.get_saved_output(), backend.TrialFunction(u.function_space()))

        dFdu = backend.assemble(dFdu)

        # Get dJdu from previous calculations.
        dJdu = fwd_block_output.get_adj_output()

        # TODO: It might make sense to move this so we don't have to do the computations above.
        if isinstance(dJdu, (int, float)) and dJdu == 0:
            return

        # Homogenize and apply boundary conditions on adj_dFdu and dJdu.
        bcs = []
        for bc in self.bcs:
            if isinstance(bc, backend.DirichletBC):
                bc = backend.DirichletBC(bc)
                bc.homogenize()
            bcs.append(bc)
            bc.apply(dFdu)

        dFdu_mat = backend.as_backend_type(dFdu).mat()
        dFdu_mat.transpose(dFdu_mat)

        # Solve the adjoint equations.
        backend.solve(dFdu, adj_var.vector(), dJdu)

        for block_output in self.get_dependencies():
            c = block_output.get_output()
            if c != self.func:
                if isinstance(c, backend.Function):
                    if c in replaced_coeffs:
                        c_rep = replaced_coeffs[c]
                    else:
                        c_rep = c

                    dFdm = -backend.derivative(F_form, c_rep, backend.TrialFunction(c.function_space()))
                    dFdm = backend.assemble(dFdm)

                    dFdm_mat = backend.as_backend_type(dFdm).mat()

                    import numpy as np
                    bc_rows = []
                    for bc in bcs:
                        for key in bc.get_boundary_values():
                            bc_rows.append(key)

                    dFdm.zero(np.array(bc_rows, dtype=np.intc))

                    dFdm_mat.transpose(dFdm_mat)

                    block_output.add_adj_output(dFdm*adj_var.vector())
                elif isinstance(c, backend.Constant):
                    dFdm = -backend.derivative(F_form, c, backend.Constant(1))
                    dFdm = backend.assemble(dFdm)

                    [bc.apply(dFdm) for bc in bcs]

                    block_output.add_adj_output(dFdm.inner(adj_var.vector()))
                elif isinstance(c, backend.DirichletBC):
                    tmp_bc = backend.DirichletBC(V, adj_var, c.user_sub_domain())
                    adj_output = Function(V)
                    tmp_bc.apply(adj_output.vector())

                    block_output.add_adj_output(adj_output.vector())
                elif isinstance(c, backend.Expression):
                    if c in replaced_coeffs:
                        c_rep = replaced_coeffs[c]
                    else:
                        c_rep = c
                    
                    dFdm = -backend.derivative(F_form, c_rep, backend.TrialFunction(V)) # TODO: What space to use?
                    dFdm = backend.assemble(dFdm)

                    dFdm_mat = backend.as_backend_type(dFdm).mat()

                    import numpy as np
                    bc_rows = []
                    for bc in bcs:
                        for key in bc.get_boundary_values():
                            bc_rows.append(key)

                    dFdm.zero(np.array(bc_rows, dtype=np.intc))

                    dFdm_mat.transpose(dFdm_mat)

                    block_output.add_adj_output([[dFdm*adj_var.vector(), V]])

    def recompute(self):
        func = self.func
        replace_lhs_coeffs = {}
        replace_rhs_coeffs = {}
        for block_output in self.get_dependencies():
            c = block_output.output
            c_rep = block_output.get_saved_output()

            if c != c_rep:
                if c in self.lhs.coefficients():
                    replace_lhs_coeffs[c] = c_rep
                    if c == self.func:
                        func = c_rep
                
                if self.linear and c in self.rhs.coefficients():
                    replace_rhs_coeffs[c] = c_rep

        lhs = backend.replace(self.lhs, replace_lhs_coeffs)
        
        rhs = 0
        if self.linear:
            rhs = backend.replace(self.rhs, replace_rhs_coeffs)

        backend.solve(lhs == rhs, func, self.bcs)
        # Save output for use in later re-computations.
        # TODO: Consider redesigning the saving system so a new deepcopy isn't created on each forward replay.
        self.get_outputs()[0].checkpoint = func._ad_create_checkpoint()
