
import logging
from collections import defaultdict

import ailment

from ...errors import AngrVariableRecoveryError
from ...knowledge_plugins import Function
from ...sim_variable import SimStackVariable
from ..forward_analysis import ForwardAnalysis, FunctionGraphVisitor
from .variable_recovery_base import VariableRecoveryBase, VariableRecoveryStateBase
from .engine_vex import SimEngineVRVEX
from .engine_ail import SimEngineVRAIL

l = logging.getLogger(name=__name__)


class ProcessorState:

    __slots__ = ['_arch', 'sp_adjusted', 'sp_adjustment', 'bp_as_base', 'bp']

    def __init__(self, arch):
        self._arch = arch
        # whether we have met the initial stack pointer adjustment
        self.sp_adjusted = None
        # how many bytes are subtracted from the stack pointer
        self.sp_adjustment = arch.bytes if arch.call_pushes_ret else 0
        # whether the base pointer is used as the stack base of the stack frame or not
        self.bp_as_base = None
        # content of the base pointer
        self.bp = None

    def copy(self):
        s = ProcessorState(self._arch)
        s.sp_adjusted = self.sp_adjusted
        s.sp_adjustment = self.sp_adjustment
        s.bp_as_base = self.bp_as_base
        s.bp = self.bp
        return s

    def merge(self, other):
        if not self == other:
            l.warning("Inconsistent merge: %s %s ", self, other)

        # FIXME: none of the following logic makes any sense...
        if other.sp_adjusted is True:
            self.sp_adjusted = True
        self.sp_adjustment = max(self.sp_adjustment, other.sp_adjustment)
        if other.bp_as_base is True:
            self.bp_as_base = True
        if self.bp is None:
            self.bp = other.bp
        elif other.bp is not None:  # and self.bp is not None
            if self.bp == other.bp:
                pass
            else:
                if type(self.bp) is int and type(other.bp) is int:
                    self.bp = max(self.bp, other.bp)
                else:
                    self.bp = None
        return self

    def __eq__(self, other):
        if not isinstance(other, ProcessorState):
            return False
        return (self.sp_adjusted == other.sp_adjusted and
                self.sp_adjustment == other.sp_adjustment and
                self.bp == other.bp and
                self.bp_as_base == other.bp_as_base)

    def __repr__(self):
        return "<ProcessorState %s%#x%s %s>" % (self.bp, self.sp_adjustment,
            " adjusted" if self.sp_adjusted else "", self.bp_as_base)


class VariableRecoveryFastState(VariableRecoveryStateBase):
    """
    The abstract state of variable recovery analysis.

    :ivar KeyedRegion stack_region: The stack store.
    :ivar KeyedRegion register_region:  The register store.
    """

    def __init__(self, block_addr, analysis, arch, func, stack_region=None, register_region=None,
                 typevars=None, type_constraints=None, processor_state=None):

        super().__init__(block_addr, analysis, arch, func, stack_region=stack_region, register_region=register_region,
                         typevars=typevars, type_constraints=type_constraints)

        self.processor_state = ProcessorState(self.arch) if processor_state is None else processor_state

    def __repr__(self):
        return "<VRAbstractState: %d register variables, %d stack variables>" % (len(self.register_region), len(self.stack_region))

    def __eq__(self, other):
        if type(other) is not VariableRecoveryFastState:
            return False
        return self.stack_region == other.stack_region and self.register_region == other.register_region

    def copy(self):

        state = VariableRecoveryFastState(
            self.block_addr,
            self._analysis,
            self.arch,
            self.function,
            stack_region=self.stack_region.copy(),
            register_region=self.register_region.copy(),
            typevars=self.typevars.copy(),
            type_constraints=self.type_constraints.copy(),
            processor_state=self.processor_state.copy(),
        )

        return state

    def merge(self, other, successor=None):
        """
        Merge two abstract states.

        For any node A whose dominance frontier that the current node (at the current program location) belongs to, we
        create a phi variable V' for each variable V that is defined in A, and then replace all existence of V with V'
        in the merged abstract state.

        :param VariableRecoveryState other: The other abstract state to merge.
        :return:                            The merged abstract state.
        :rtype:                             VariableRecoveryState
        """

        replacements = {}
        if successor in self.dominance_frontiers:
            replacements = self._make_phi_variables(successor, self, other)

        merged_stack_region = self.stack_region.copy().replace(replacements).merge(other.stack_region,
                                                                                   replacements=replacements)
        merged_register_region = self.register_region.copy().replace(replacements).merge(other.register_region,
                                                                                         replacements=replacements)
        merged_typevars = self.typevars.merge(other.typevars)
        merged_typeconstraints = self.type_constraints.copy() | other.type_constraints

        state = VariableRecoveryFastState(
            successor,
            self._analysis,
            self.arch,
            self.function,
            stack_region=merged_stack_region,
            register_region=merged_register_region,
            typevars=merged_typevars,
            type_constraints=merged_typeconstraints,
            processor_state=self.processor_state.copy().merge(other.processor_state),
        )

        return state

    #
    # Util methods
    #

    def _normalize_register_offset(self, offset):  #pylint:disable=no-self-use

        # TODO:

        return offset

    def _to_signed(self, n):

        if n >= 2 ** (self.arch.bits - 1):
            # convert it to a negative number
            return n - 2 ** self.arch.bits

        return n


class VariableRecoveryFast(ForwardAnalysis, VariableRecoveryBase):  #pylint:disable=abstract-method
    """
    Recover "variables" from a function by keeping track of stack pointer offsets and pattern matching VEX statements.

    If calling conventions are recovered prior to running VariableRecoveryFast, variables can be recognized more
    accurately. However, it is not a requirement.
    """

    def __init__(self, func, max_iterations=1, clinic=None, low_priority=False, track_sp=True):
        """

        :param knowledge.Function func:  The function to analyze.
        :param int max_iterations:
        :param clinic:
        """

        function_graph_visitor = FunctionGraphVisitor(func)

        # Make sure the function is not empty
        if not func.block_addrs_set or func.startpoint is None:
            raise AngrVariableRecoveryError("Function %s is empty." % repr(func))

        VariableRecoveryBase.__init__(self, func, max_iterations)
        ForwardAnalysis.__init__(self, order_jobs=True, allow_merging=True, allow_widening=False,
                                 graph_visitor=function_graph_visitor)

        self._clinic = clinic
        self._low_priority = low_priority
        self._job_ctr = 0
        self._track_sp = track_sp

        self._ail_engine = SimEngineVRAIL(self.project)
        self._vex_engine = SimEngineVRVEX(self.project)

        self._node_iterations = defaultdict(int)

        self._node_to_cc = { }

        self._analyze()

        # cleanup (for cpython pickle)
        self._ail_engine = None
        self._vex_engine = None

    #
    # Main analysis routines
    #

    def _pre_analysis(self):

        self.initialize_dominance_frontiers()

        # initialize node_to_cc map
        function_nodes = [n for n in self.function.transition_graph.nodes() if isinstance(n, Function)]

        if self._track_sp:
            for func_node in function_nodes:
                for callsite_node in self.function.transition_graph.predecessors(func_node):
                    if func_node.calling_convention is None:
                        l.warning("Unknown calling convention for %r.", func_node)
                    else:
                        self._node_to_cc[callsite_node.addr] = func_node.calling_convention

    def _pre_job_handling(self, job):
        self._job_ctr += 1
        if self._low_priority:
            self._release_gil(self._job_ctr, 5, 0.0001)

    def _initial_abstract_state(self, node):

        # annotate the stack pointer
        # concrete_state.regs.sp = concrete_state.regs.sp.annotate(StackLocationAnnotation(8))

        # give it enough stack space
        # concrete_state.regs.bp = concrete_state.regs.sp + 0x100000

        state = VariableRecoveryFastState(node.addr, self, self.project.arch, self.function,
                                          )
        # put a return address on the stack if necessary
        if self.project.arch.call_pushes_ret:
            ret_addr_offset = self.project.arch.bytes
            ret_addr_var = SimStackVariable(ret_addr_offset, self.project.arch.bytes, base='bp', name='ret_addr',
                                            region=self.function.addr, category='return_address',
                                            )
            state.stack_region.add_variable(ret_addr_offset, ret_addr_var)

        return state

    def _merge_states(self, node, *states):

        return states[0].merge(states[1], successor=node.addr)

    def _run_on_node(self, node, state):
        """


        :param angr.Block node:
        :param VariableRecoveryState state:
        :return:
        """

        input_state = state  # make it more meaningful

        if self._clinic:
            # AIL mode
            block = self._clinic.block(node.addr, node.size)
        else:
            # VEX mode
            block = self.project.factory.block(node.addr, node.size, opt_level=0)

        if node.addr in self._instates:
            prev_state = self._instates[node.addr]
            if input_state == prev_state:
                l.debug('Skip node %#x as we have reached a fixed-point', node.addr)
                return False, input_state
            else:
                l.debug('Merging input state of node %#x with the previous state.', node.addr)
                input_state = prev_state.merge(input_state, successor=node.addr)

        state = input_state.copy()
        state.block_addr = node.addr
        self._instates[node.addr] = input_state

        if self._node_iterations[node.addr] >= self._max_iterations:
            l.debug('Skip node %#x as we have iterated %d times on it.', node.addr, self._node_iterations[node.addr])
            return False, state

        self._process_block(state, block)

        self._outstates[node.addr] = state

        self._node_iterations[node.addr] += 1

        return True, state

    def _intra_analysis(self):
        pass

    def _post_analysis(self):
        self.variable_manager[self.function.addr].assign_variable_names()

        for addr, state in self._outstates.items():
            self.variable_manager[self.function.addr].set_live_variables(addr,
                                                                         state.register_region,
                                                                         state.stack_region
                                                                         )

    #
    # Private methods
    #

    def _process_block(self, state, block):  # pylint:disable=no-self-use
        """
        Scan through all statements and perform the following tasks:
        - Find stack pointers and the VEX temporary variable storing stack pointers
        - Selectively calculate VEX statements
        - Track memory loading and mark stack and global variables accordingly

        :param angr.Block block:
        :return:
        """

        l.debug('Processing block %#x.', block.addr)

        processor = self._ail_engine if isinstance(block, ailment.Block) else self._vex_engine
        processor.process(state, block=block, fail_fast=self._fail_fast)

        if self._track_sp and block.addr in self._node_to_cc:
        # readjusting sp at the end for blocks that end in a call
            cc = self._node_to_cc[block.addr]
            state.processor_state.sp_adjusted = False

            if cc is not None and cc.sp_delta is not None:
                state.processor_state.sp_adjustment += cc.sp_delta
                state.processor_state.sp_adjusted = True
                l.debug('Adjusting stack pointer at end of block %#x with offset %+#x.',
                        block.addr, state.processor_state.sp_adjustment)
            else:
                # make a guess
                # of course, this will fail miserably if the function called is not cdecl
                if self.project.arch.call_pushes_ret:
                    state.processor_state.sp_adjustment += self.project.arch.bytes
                    state.processor_state.sp_adjusted = True


from angr.analyses import AnalysesHub
AnalysesHub.register_default('VariableRecoveryFast', VariableRecoveryFast)
