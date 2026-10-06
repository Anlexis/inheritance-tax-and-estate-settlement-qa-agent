# PB-6: Invoke Execution Order Verification
# Verifies BaseNode.__call__() enforces: trust gate -> node_start ->
# _security_gate_input() -> execute() -> _security_gate_output() ->
# node_complete, for every concrete node under src/nodes/.
#
# PB-6b: Full-graph backbone slot order
# InheritanceTaxQAAgent.invoke() must visit:
#   initialize -> pre_process -> main -> post_process -> finalize
# (by class name in node_history) and return AgentStatus.SUCCESS.

import importlib
import inspect
import pkgutil

import pytest

# ---------------------------------------------------------------------------
# Constants used by PB-6b (graph-level test)
# ---------------------------------------------------------------------------

# The node classes filling the three domain backbone slots.
# Must match the classes registered in src/graph/graph.py.
_PRE_PROCESS_NODE = "PreProcessNode"
_MAIN_SLOT_NODE = "MainNode"
_POST_PROCESS_NODE = "PostProcessNode"

# A valid domain request that:
#   - passes PreProcessNode input validation (non-empty, no injection patterns)
#   - produces a SUCCESS-yielding pipeline run
# A realistic domain query that passes input validation end-to-end.
_VALID_PAYLOAD = "相続税の申告期限と必要な手続きを教えてください。"


# ---------------------------------------------------------------------------
# Node discovery (PB-6a)
# ---------------------------------------------------------------------------


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    from framework.nodes.base_node import BaseNode

    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError:
        return []

    seen: set[type] = set()
    discovered: list[type] = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
                and attr not in seen
            ):
                seen.add(attr)
                discovered.append(attr)
    return discovered


# ---------------------------------------------------------------------------
# PB-6a: per-node __call__ invoke order
# ---------------------------------------------------------------------------


class TestInvokeOrder:
    """PB-6a: __call__ must run trust-gate -> node_start -> input-gate -> execute() -> output-gate -> node_complete."""

    def test_call_order_for_every_node(self, monkeypatch):
        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state, _o=order: _o.append(f"event:{event_type}"),
            )

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            instance = node_cls()
            state = {
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
            }
            instance(state)

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\n" f"expected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)


# ---------------------------------------------------------------------------
# PB-6b: Full graph invoke backbone order
# ---------------------------------------------------------------------------


class TestGraphInvokeOrder:
    """PB-6b: Full graph invoke must visit every backbone slot in order and return SUCCESS.

    Verifies:
      - Graph compiles without error.
      - agent.invoke(_VALID_PAYLOAD) visits:
            InitializeNode -> PreProcessNode -> MainNode -> PostProcessNode -> FinalizeNode
        (by class name in node_history).
      - Final status is AgentStatus.SUCCESS.
      - Each domain backbone slot holds the expected node type.

    Trust level note:
      Uses TrustLevel.VERIFIED_EXTERNAL (the production external caller trust
      level). Downstream domain nodes declare TrustLevel.ANONYMOUS, so the
      external-facing trust gate lives on PreProcessNode (VERIFIED_EXTERNAL),
      which correctly enforces authentication for production callers.
    """

    def test_backbone_node_history(self, monkeypatch):
        """PB-6b: graph.invoke() must produce SUCCESS with full backbone node_history."""
        # Silence framework-level lifecycle emit_trace_event
        monkeypatch.setattr(
            "framework.nodes.base_node.emit_trace_event",
            lambda *a, **k: None,
        )

        from framework.schemas.agent_status import AgentStatus
        from framework.schemas.invocation_context import InvocationContext
        from framework.schemas.trust_level import TrustLevel

        from src.graph.graph import InheritanceTaxQAAgent

        agent = InheritanceTaxQAAgent()
        agent.compile()

        ctx = InvocationContext(
            session_id="pb6b-graph-test",
            caller_id="pb6b-test",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        )
        result = agent.invoke(_VALID_PAYLOAD, ctx=ctx)

        node_history = result.get("node_history", [])

        # Every backbone class must appear — failure means a slot was skipped.
        expected_backbone = (
            "InitializeNode",
            _PRE_PROCESS_NODE,
            _MAIN_SLOT_NODE,
            _POST_PROCESS_NODE,
            "FinalizeNode",
        )
        for cls_name in expected_backbone:
            assert cls_name in node_history, (
                f"Backbone class '{cls_name}' missing from node_history; " f"got: {node_history}"
            )

        # Order: each backbone node must appear after the previous one.
        indices = [node_history.index(n) for n in expected_backbone]
        assert indices == sorted(indices), f"Backbone nodes out of order in node_history; got: {node_history}"

        # Final status must be SUCCESS for a valid payload.
        status = result.get("status")
        assert (
            status == AgentStatus.SUCCESS or status == AgentStatus.SUCCESS.value
        ), f"Expected SUCCESS, got {status!r}; node_history: {node_history}"

    def test_domain_slots_hold_the_expected_node_types(self):
        """The pre_process / main / post_process slots must hold this template's nodes."""
        from src.graph.graph import InheritanceTaxQAAgent
        from src.nodes.main_node import MainNode
        from src.nodes.post_process_node import PostProcessNode
        from src.nodes.pre_process_node import PreProcessNode

        agent = InheritanceTaxQAAgent()
        agent.compile()
        for slot, expected_cls in (
            ("pre_process", PreProcessNode),
            ("main", MainNode),
            ("post_process", PostProcessNode),
        ):
            node = agent._nodes.get(slot)
            assert node is not None, f"No node registered at `{slot}` slot"
            assert isinstance(
                node, expected_cls
            ), f"Expected {expected_cls.__name__} at `{slot}` slot, got {type(node).__name__}"
