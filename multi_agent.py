"""Multi-agent decision-memo system using only LangChain + LangGraph (no Langflow).

Graph (mirrors "Multi agent system.json"):

    START ─► planner ─► researcher ─┬► analyst_a ─┐
                                    └► analyst_b ─┴► synthesis ─► END

Analyst A and B run in parallel (fan-out) and synthesis waits for both (fan-in).
"""
import ast
import operator
import os
import sys
from datetime import datetime, timezone
from typing import TypedDict

from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

import prompts

MODEL_NAME = "gpt-4o-mini"
KICKOFF = "Produce the required output."  # user turn for agents whose task lives in the system prompt


# ---------------------------------------------------------------- tools (Langflow Agent's built-ins)
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("Unsupported expression")


@tool
def calculator(expression: str) -> str:
    """Evaluate an arithmetic expression, e.g. '(120 * 0.15) / 4'."""
    try:
        return str(_eval(ast.parse(expression, mode="eval").body))
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@tool
def current_date() -> str:
    """Return the current date and time in UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


TOOLS = [calculator, current_date]


# ---------------------------------------------------------------- agents
def _llm() -> ChatOpenAI:
    return ChatOpenAI(model=MODEL_NAME)


def run_agent(system_prompt: str, user_input: str) -> str:
    agent = create_agent(_llm(), tools=TOOLS, system_prompt=system_prompt)
    result = agent.invoke(
        {"messages": [{"role": "user", "content": user_input}]},
        config={"recursion_limit": 15 * 2 + 1},  # ~ Langflow max_iterations=15
    )
    return result["messages"][-1].content


# ---------------------------------------------------------------- state & nodes
class State(TypedDict, total=False):
    brief: str
    plan: str
    research: str
    memo_a: str
    memo_b: str
    final_memo: str


def planner(state: State) -> State:
    system = prompts.PLANNER_SYSTEM.format(brief=state["brief"])
    return {"plan": run_agent(system, state["brief"])}


def researcher(state: State) -> State:
    system = prompts.RESEARCHER_TEMPLATE.format(brief=state["brief"], plan=state["plan"])
    return {"research": run_agent(system, KICKOFF)}


def _analyzer_prompt(state: State) -> str:
    return prompts.ANALYZER_TEMPLATE.format(
        brief=state["brief"], plan=state["plan"], research=state["research"]
    )


def analyst_a(state: State) -> State:
    return {"memo_a": run_agent(prompts.ANALYST_A_SYSTEM, _analyzer_prompt(state))}


def analyst_b(state: State) -> State:
    return {"memo_b": run_agent(prompts.ANALYST_B_SYSTEM, _analyzer_prompt(state))}


def synthesis(state: State) -> State:
    system = prompts.SYNTHESIS_TEMPLATE.format(memo_a=state["memo_a"], memo_b=state["memo_b"])
    return {"final_memo": run_agent(system, KICKOFF)}


# ---------------------------------------------------------------- graph
def build_graph():
    g = StateGraph(State)
    g.add_node("planner", planner)
    g.add_node("researcher", researcher)
    g.add_node("analyst_a", analyst_a)
    g.add_node("analyst_b", analyst_b)
    g.add_node("synthesis", synthesis)

    g.add_edge(START, "planner")
    g.add_edge("planner", "researcher")
    g.add_edge("researcher", "analyst_a")  # fan-out: both analysts run in parallel
    g.add_edge("researcher", "analyst_b")
    g.add_edge(["analyst_a", "analyst_b"], "synthesis")  # fan-in: wait for both
    g.add_edge("synthesis", END)
    return g.compile()


SECTIONS = [
    ("plan", "PLANNER"), ("research", "RESEARCHER"), ("memo_a", "ANALYST A (customer lens)"),
    ("memo_b", "ANALYST B (risk lens)"), ("final_memo", "FINAL SYNTHESIZED MEMO"),
]


def main() -> None:
    brief = " ".join(sys.argv[1:]) or sys.stdin.read()
    if not brief.strip():
        sys.exit('Usage: python multi_agent.py "<decision brief>"')
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY first.")
    state = build_graph().invoke({"brief": brief})
    for key, title in SECTIONS:
        print(f"\n===== {title} =====\n{state[key]}")


if __name__ == "__main__":
    main()
