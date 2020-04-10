
import logging

from ...engines.light import SimEngineLight, SpOffset, ArithmeticExpression
from ...errors import SimEngineError
from ...sim_variable import SimStackVariable, SimRegisterVariable
from ..code_location import CodeLocation
from ..typehoon import typevars

#
# The base engine used in VariableRecoveryFast
#

l = logging.getLogger(name=__name__)


class RichR:
    """
    A rich representation of calculation results.
    """

    __slots__ = ('data', 'variable', 'typevar', 'type_constraints', )

    def __init__(self, data, variable=None, typevar=None, type_constraints=None):
        self.data = data
        self.variable = variable
        self.typevar = typevar
        self.type_constraints = type_constraints


class SimEngineVRBase(SimEngineLight):
    def __init__(self, project):
        super().__init__()

        self.project = project
        self.processor_state = None
        self.variable_manager = None

    @property
    def func_addr(self):
        if self.state is None:
            return None
        return self.state.function.addr

    def process(self, state, *args, **kwargs):  # pylint:disable=unused-argument

        self.processor_state = state.processor_state
        self.variable_manager = state.variable_manager

        try:
            self._process(state, None, block=kwargs.pop('block', None))
        except SimEngineError as e:
            if kwargs.pop('fail_fast', False) is True:
                raise e

    def _process(self, state, successors, block=None, func_addr=None):  # pylint:disable=unused-argument,arguments-differ
        super()._process(state, successors, block=block)

    #
    # Logic
    #

    def _assign_to_register(self, offset, richr, size, src=None, dst=None):
        """

        :param int offset:
        :param RichR data:
        :param int size:
        :return:
        """

        codeloc = self._codeloc()  # type: CodeLocation
        data = richr.data

        if offset == self.arch.sp_offset:
            if type(data) is SpOffset:
                sp_offset = data.offset
                self.processor_state.sp_adjusted = True
                self.processor_state.sp_adjustment = sp_offset
                l.debug('Adjusting stack pointer at %#x with offset %+#x.', self.ins_addr, sp_offset)
            return

        if offset == self.arch.bp_offset:
            if data is not None:
                self.processor_state.bp = data
            else:
                self.processor_state.bp = None
            return

        # handle register writes
        if type(data) is SpOffset:
            # lea
            stack_offset = data.offset
            existing_vars = self.variable_manager[self.func_addr].find_variables_by_stmt(self.block.addr,
                                                                                         self.stmt_idx,
                                                                                         'memory')

            if not existing_vars:
                # TODO: how to determine the size for a lea?
                existing_vars = self.state.stack_region.get_variables_by_offset(stack_offset)
                if not existing_vars:
                    lea_size = 1
                    variable = SimStackVariable(stack_offset, lea_size, base='bp',
                                                ident=self.variable_manager[self.func_addr].next_variable_ident(
                                                    'stack'),
                                                region=self.func_addr,
                                                )

                    self.variable_manager[self.func_addr].add_variable('stack', stack_offset, variable)
                    l.debug('Identified a new stack variable %s at %#x.', variable, self.ins_addr)
                else:
                    variable = next(iter(existing_vars))

            else:
                variable, _ = existing_vars[0]

            self.state.stack_region.add_variable(stack_offset, variable)
            self.state.typevars.add_type_variable(variable, codeloc, typevars.TypeVariable())
            base_offset = self.state.stack_region.get_base_addr(stack_offset)
            for var in self.state.stack_region.get_variables_by_offset(base_offset):
                offset_into_var = stack_offset - base_offset
                if offset_into_var == 0: offset_into_var = None
                self.variable_manager[self.func_addr].reference_at(var, offset_into_var, codeloc,
                                                                   atom=src)

        else:
            pass

        # register writes

        existing_vars = self.variable_manager[self.func_addr].find_variables_by_stmt(self.block.addr, self.stmt_idx,
                                                                                     'register'
                                                                                     )
        if not existing_vars:
            variable = SimRegisterVariable(offset, size,
                                           ident=self.variable_manager[self.func_addr].next_variable_ident(
                                               'register'),
                                           region=self.func_addr
                                           )
            self.variable_manager[self.func_addr].set_variable('register', offset, variable)
        else:
            variable, _ = existing_vars[0]

        self.state.register_region.set_variable(offset, variable)
        self.variable_manager[self.func_addr].write_to(variable, None, codeloc, atom=dst)

        if richr.typevar is not None:
            if not self.state.typevars.has_type_variable_for(variable, codeloc):
                # assign a new type variable to it
                typevar = typevars.TypeVariable()
                self.state.typevars.add_type_variable(variable, codeloc, typevar)
                # create constraints
                self.state.add_type_constraint(typevars.Subtype(richr.typevar, typevar))

    def _store(self, addr, data, size, stmt=None):  # pylint:disable=unused-argument
        """

        :param addr:
        :param data:
        :param int size:
        :return:
        """

        if type(addr) is SpOffset:
            # Storing data to stack
            stack_offset = addr.offset

            if stmt is None:
                existing_vars = self.variable_manager[self.func_addr].find_variables_by_stmt(self.block.addr,
                                                                                             self.stmt_idx,
                                                                                             'memory'
                                                                                             )
            else:
                existing_vars = self.variable_manager[self.func_addr].find_variables_by_atom(self.block.addr,
                                                                                             self.stmt_idx,
                                                                                             stmt
                                                                                             )
            if not existing_vars:
                variable = SimStackVariable(stack_offset, size, base='bp',
                                            ident=self.variable_manager[self.func_addr].next_variable_ident(
                                                'stack'),
                                            region=self.func_addr,
                                            )
                if isinstance(stack_offset, int):
                    self.variable_manager[self.func_addr].set_variable('stack', stack_offset, variable)
                    l.debug('Identified a new stack variable %s at %#x.', variable, self.ins_addr)

            else:
                variable, _ = next(iter(existing_vars))

            if isinstance(stack_offset, int):
                self.state.stack_region.set_variable(stack_offset, variable)
                base_offset = self.state.stack_region.get_base_addr(stack_offset)
                codeloc = CodeLocation(self.block.addr, self.stmt_idx, ins_addr=self.ins_addr)
                for var in self.state.stack_region.get_variables_by_offset(stack_offset):
                    offset_into_var = stack_offset - base_offset
                    if offset_into_var == 0:
                        offset_into_var = None
                    self.variable_manager[self.func_addr].write_to(var,
                                                                   offset_into_var,
                                                                   codeloc,
                                                                   atom=stmt,
                                                                   )

                # create type constraints
                if data.typevar is not None:
                    if not self.state.typevars.has_type_variable_for(variable, codeloc):
                        addr_vartype = typevars.TypeVariable()
                        self.state.typevars.add_type_variable(variable, codeloc, addr_vartype)
                    else:
                        addr_vartype = self.state.typevars.get_type_variable(variable, codeloc)
                    if addr_vartype is not None:
                        self.state.add_type_constraint(
                            typevars.Subtype(
                                typevars.DerivedTypeVariable(
                                    typevars.DerivedTypeVariable(addr_vartype, typevars.Store()),
                                    typevars.HasField(size * 8, 0)
                                ),
                                data.typevar
                            )
                        )

    def _load(self, richr_addr, size, expr=None):
        """

        :param RichR richr_addr:
        :param size:
        :return:
        """

        addr = richr_addr.data

        if type(addr) is SpOffset:
            # Loading data from stack
            stack_offset = addr.offset

            # split the offset into a concrete offset and a dynamic offset
            # the stack offset may not be a concrete offset
            # for example, SP-0xe0+var_1
            if type(stack_offset) is ArithmeticExpression:
                if type(stack_offset.operands[0]) is int:
                    concrete_offset = stack_offset.operands[0]
                    dynamic_offset = stack_offset.operands[1]
                elif type(stack_offset.operands[1]) is int:
                    concrete_offset = stack_offset.operands[1]
                    dynamic_offset = stack_offset.operands[0]
                else:
                    # cannot determine the concrete offset. give up
                    concrete_offset = None
                    dynamic_offset = stack_offset
            else:
                # type(stack_offset) is int
                concrete_offset = stack_offset
                dynamic_offset = None

            # decide which base variable is being accessed using the concrete offset
            if concrete_offset is not None and concrete_offset not in self.state.stack_region:
                variable = SimStackVariable(concrete_offset, size, base='bp',
                                            ident=self.variable_manager[self.func_addr].next_variable_ident(
                                                'stack'),
                                            region=self.func_addr,
                                            )
                self.state.stack_region.add_variable(concrete_offset, variable)

                self.variable_manager[self.func_addr].add_variable('stack', concrete_offset, variable)

                l.debug('Identified a new stack variable %s at %#x.', variable, self.ins_addr)

            base_offset = self.state.stack_region.get_base_addr(concrete_offset)
            codeloc = CodeLocation(self.block.addr, self.stmt_idx, ins_addr=self.ins_addr)

            all_vars = self.state.stack_region.get_variables_by_offset(base_offset)
            if len(all_vars) > 1:
                # overlapping variables
                l.warning("Reading memory with overlapping variables: %s. Ignoring all but the first one.",
                          all_vars)

            var = next(iter(all_vars))
            # calculate variable_offset
            if dynamic_offset is None:
                offset_into_variable = concrete_offset - base_offset
                if offset_into_variable == 0:
                    offset_into_variable = None
            else:
                if concrete_offset == base_offset:
                    offset_into_variable = dynamic_offset
                else:
                    offset_into_variable = ArithmeticExpression(ArithmeticExpression.Add,
                                                                (dynamic_offset, concrete_offset - base_offset,)
                                                                )
            self.variable_manager[self.func_addr].read_from(var,
                                                            offset_into_variable,
                                                            codeloc,
                                                            atom=expr,
                                                            # overwrite=True
                                                            )

            # create type constraints
            if not self.state.typevars.has_type_variable_for(var, codeloc):
                addr_typevar = typevars.TypeVariable()
                self.state.typevars.add_type_variable(var, codeloc, addr_typevar)
            else:
                addr_typevar = self.state.typevars.get_type_variable(var, codeloc)
            self.state.add_type_constraint(
                typevars.DerivedTypeVariable(
                    typevars.DerivedTypeVariable(addr_typevar, typevars.Load()),
                    typevars.HasField(size * 8, 0)
                )
            )

            return RichR(None, variable=var)

        # Loading data from a pointer

        # parse the loading offset
        offset = 0
        if (isinstance(richr_addr.typevar, typevars.DerivedTypeVariable) and
                isinstance(richr_addr.typevar.label, typevars.AddN)):
            offset = richr_addr.typevar.label.n

        # create a type constraint
        self.state.add_type_constraint(typevars.DerivedTypeVariable(
                typevars.DerivedTypeVariable(richr_addr.typevar, typevars.Load()),
                typevars.HasField(size * 8, offset)
            )
        )
        return RichR(None)

    def _read_from_register(self, offset, size, expr=None):
        """

        :param offset:
        :param size:
        :return:
        """

        codeloc = self._codeloc()

        if offset == self.arch.sp_offset:
            # loading from stack pointer
            return RichR(SpOffset(self.arch.bits, self.processor_state.sp_adjustment, is_base=False))
        elif offset == self.arch.bp_offset:
            return RichR(self.processor_state.bp)

        if offset not in self.state.register_region:
            variable = SimRegisterVariable(offset, size,
                                           ident=self.variable_manager[self.func_addr].next_variable_ident(
                                               'register'),
                                           region=self.func_addr,
                                           )
            self.state.register_region.add_variable(offset, variable)
            self.variable_manager[self.func_addr].add_variable('register', offset, variable)

        for var in self.state.register_region.get_variables_by_offset(offset):
            self.variable_manager[self.func_addr].read_from(var, None, codeloc, atom=expr)

        # we accept the precision loss here by only returning the first variable
        var = next(iter(self.state.register_region.get_variables_by_offset(offset)))
        if var not in self.state.typevars:
            typevar = typevars.TypeVariable()
            self.state.typevars.add_type_variable(var, codeloc, typevar)
        else:
            # FIXME: This is an extremely stupid hack. Fix it later.
            # typevar = next(reversed(list(self.state.typevars[var].values())))
            typevar = self.state.typevars[var]

        return RichR(None, variable=var, typevar=typevar)
