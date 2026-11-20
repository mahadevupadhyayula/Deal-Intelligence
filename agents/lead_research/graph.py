"""LangGraph assembly for the Lead Research agent.

WHAT: `build_graph` wires the nodes into load_icp -> resolve_entity -> plan_queries -> search
-> synthesize -> validate -> score -> (refine loop | persist).
WHERE: Compiled once per job by `handler.py` and invoked with a `thread_id`.
WHY: An explicit graph (vs a free-roaming agent) makes the trajectory fixed and testable and
bounds cost: the only loop is score -> plan_queries, taken at most `max_refine_loops` times and
only when finishing the unknowns could change the tier. It accepts an optional checkpointer
(Spec 008 supplies PostgresSaver for HITL); none is used here.
"""
from langgraph.graph import END, START, StateGraph

from agents.lead_research.nodes import ResearchDeps, build_nodes
from agents.lead_research.state import ResearchState


def build_graph(deps: ResearchDeps, checkpointer=None):
    nodes, route = build_nodes(deps)
    g = StateGraph(ResearchState)
    for name, fn in nodes.items():
        g.add_node(name, fn)
    g.add_edge(START, "load_icp")
    g.add_edge("load_icp", "resolve_entity")
    g.add_edge("resolve_entity", "plan_queries")
    g.add_edge("plan_queries", "search")
    g.add_edge("search", "synthesize")
    g.add_edge("synthesize", "validate")
    g.add_edge("validate", "score")
    g.add_conditional_edges("score", route, {"plan_queries": "plan_queries", "persist": "persist"})
    g.add_edge("persist", END)
    return g.compile(checkpointer=checkpointer)
