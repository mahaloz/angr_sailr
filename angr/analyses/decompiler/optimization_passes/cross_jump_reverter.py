from collections import defaultdict
from typing import Any, Tuple, Dict, List, Optional
from itertools import count
import copy
import logging
import inspect

import networkx
import networkx as nx

import ailment
from ailment.statement import Jump, ConditionalJump
from ailment.expression import Const
from .. import RegionIdentifier

from ..condition_processor import ConditionProcessor, EmptyBlockNotice
from .optimization_pass import OptimizationPass, OptimizationPassStage
from ..goto_manager import GotoManager
from ..structuring import RecursiveStructurer, PhoenixStructurer
from ..utils import to_ail_supergraph

l = logging.getLogger(__name__)


class CrossJumpReverter(OptimizationPass):
    """
    Copies bad blocks
    """

    # TODO: This optimization pass may support more architectures and platforms
    ARCHES = [
        "X86",
        "AMD64",
        "ARMCortexM",
        "ARMHF",
        "ARMEL",
    ]
    PLATFORMS = ["cgc", "linux"]
    STAGE = OptimizationPassStage.DURING_REGION_IDENTIFICATION
    NAME = "Duplicate blocks destroyed with gotos"
    DESCRIPTION = "DUPLICATE"

    def __init__(
        self,
        func,
        blocks_by_addr=None,
        blocks_by_addr_and_idx=None,
        graph=None,
        # internal parameters that should be used by Clinic
        node_idx_start=0,
        # settings
        max_level=10,
        min_indegree=2,
        reaching_definitions=None,
        region_identifier=None,
        max_level_goto_check=2,
        **kwargs,
    ):
        super().__init__(
            func, blocks_by_addr=blocks_by_addr, blocks_by_addr_and_idx=blocks_by_addr_and_idx, graph=graph, **kwargs
        )

        self.max_level = max_level
        self.min_indegree = min_indegree
        self.max_level_goto_check = max_level_goto_check
        self.node_idx = count(start=node_idx_start)
        self._rd = reaching_definitions
        self.ri = region_identifier

        self.goto_manager: Optional[GotoManager] = None
        self.initial_gotos = None

        self.func_name = self._func.name
        self.binary_name = self.project.loader.main_object.binary_basename
        self.target_name = f"{self.binary_name}.{self.func_name}"
        self.graph_copy = None
        self.analyze()

    def _check(self):
        return True, None

    def _analyze(self, cache=None):
        # for each block with no successors and more than 1 predecessors, make copies of this block and link it back to
        # the sources of incoming edges
        self.graph_copy = to_ail_supergraph(networkx.DiGraph(self._graph))
        self.last_graph = None
        graph_updated = False

        # attempt at most N levels
        for _ in range(self.max_level):
            success, graph_has_gotos = self._structure_graph()
            if not success:
                self.graph_copy = self.last_graph
                break

            if not graph_has_gotos:
                l.debug("Graph has no gotos. Leaving analysis...")
                break

            # make a clone of graph copy to recover in the event of failure
            self.last_graph = self.graph_copy.copy()
            r = self._analyze_core(self.graph_copy)
            if not r:
                break
            graph_updated = True

        # the output graph
        if graph_updated and self.graph_copy is not None:
            if self.goto_manager is not None and not (len(self.initial_gotos) < len(self.goto_manager.gotos)):
                self.out_graph = self.graph_copy


    #
    # taken from deduplicator
    #

    def _structure_graph(self):
        # reset gotos
        self.goto_manager = None

        # do structuring
        self.ri = self.project.analyses[RegionIdentifier].prep(kb=self.kb)(
            self._func, graph=self.graph_copy, cond_proc=self.ri.cond_proc, force_loop_single_exit=False,
            complete_successors=True
        )
        rs = self.project.analyses[RecursiveStructurer].prep(kb=self.kb)(
            copy.deepcopy(self.ri.region),
            cond_proc=self.ri.cond_proc,
            func=self._func,
            structurer_cls=PhoenixStructurer
        )
        if not rs.result.nodes:
            l.critical(f"Failed to redo structuring on {self.target_name}")
            return False, False

        rs = self.project.analyses.RegionSimplifier(self._func, rs.result, kb=self.kb, variable_kb=self._variable_kb)
        self.goto_manager = rs.goto_manager
        if self.initial_gotos is None:
            self.initial_gotos = self.goto_manager.gotos

        return True, len(self.goto_manager.gotos) != 0 if self.goto_manager else False

    def _analyze_core(self, graph: networkx.DiGraph):
        # collect all nodes that have a goto
        to_update = {}
        for node in graph.nodes:
            gotos = self.goto_manager.gotos_in_block(node)
            if not gotos or len(gotos) >= 2:
                continue

            # only single reaching gotos
            goto = list(gotos)[0]
            for goto_target in graph.successors(node):
                if goto_target.addr == goto.target_addr:
                    break
            else:
                goto_target = None

            if goto_target is None:
                continue

            if graph.out_degree(goto_target) != 1:
                continue

            # og_block -> suc_block (goto target)
            to_update[node] = goto_target

        if not to_update:
            return False

        for target_node, goto_node in to_update.items():
            # always make a copy if there is a goto edge
            cp = copy.deepcopy(goto_node)
            cp.idx = next(self.node_idx)

            # remove this goto edge from original
            graph.remove_edge(target_node, goto_node)

            # add a new edge to the copy
            graph.add_edge(target_node, cp)

            # make sure the copy has the same successor as before!
            suc = list(graph.successors(goto_node))[0]
            graph.add_edge(cp, suc)

            # kill the original if we made enough copies to drain in-degree
            if graph.in_degree(goto_node) == 0:
                graph.remove_node(goto_node)

        # TODO: add single chain later:
        # i.e., we need to copy the entire chain of single successor nodes in
        # this goto chain.
        return True