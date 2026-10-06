"""AgentCore Platform v1.0"""

# FIN-C2-017 InheritanceTaxQAAgent — graph composition.
#
# Cat 2 fixed pipeline on the 5-node backbone (AgentBaseGraph):
#   START → initialize → pre_process → main → post_process → finalize → END
#
# Three domain slots encapsulate the 6 business steps:
#   pre_process  → QueryNormalize (+ caller case-parameter validation)
#   main         → EstateContextExtract + InheritanceTaxKBRetrieve (MainNode orchestrates)
#   post_process → DeadlineCalculate + BasicDeductionCalculate +
#                  ProcedureChecklistGenerate + ResponseValidate
#
# initialize/finalize are injected by super().register_nodes(); no add_edges()
# override (the backbone is fixed, no conditional routing). Design: docs/02_design.md.

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import MainNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class InheritanceTaxQAAgent(AgentBaseGraph):
    """InheritanceTaxQAAgent fixed pipeline (Cat 2, direct AgentBaseGraph inheritance)."""

    @property
    def name(self) -> str:
        return "InheritanceTaxQAAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = MainNode()
        self._nodes["post_process"] = PostProcessNode()
