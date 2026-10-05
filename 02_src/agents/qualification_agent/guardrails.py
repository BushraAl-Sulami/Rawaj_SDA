"""Wrap the benchmark search tool with a per-run call limit."""

import logging
from threading import Lock

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS = 3
LIMIT_MESSAGE = "Search limit reached for this run. Do not search again: use the evidence and the benchmark results you already have."


def guarded_benchmark_tool(max_calls: int = MAX_TOOL_CALLS, *, audit=None):
    """Create a wrapped tool for each run, allowing at most max_calls searches."""
    from langchain_core.tools import StructuredTool

    from agents.qualification_agent.tools import search_instagram_benchmark

    count = 0
    lock = Lock()

    def guarded_search(query: str) -> str:
        nonlocal count
        with lock:
            count += 1
            blocked = count > max_calls
        if blocked:
            if audit is not None:
                audit.append({"status": "blocked", "reason": "call_limit"})
            logger.warning("Qualification tool guardrail: search limit reached (%d per run)", max_calls)
            return LIMIT_MESSAGE

        result = search_instagram_benchmark.invoke({"query": query})
        if audit is not None:
            audit.append({"status": "unavailable" if result.startswith("No external benchmark") else "completed",
                          "query": query, "result": result})
        return result

    return StructuredTool.from_function(
        func=guarded_search,
        name=search_instagram_benchmark.name,
        description=search_instagram_benchmark.description,
    )
