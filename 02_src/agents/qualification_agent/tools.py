import os

from dotenv import load_dotenv
from langchain_core.tools import tool

try:
    from langchain_tavily import TavilySearch
except ImportError:  # pragma: no cover - optional dependency in some envs
    TavilySearch = None


load_dotenv()


api_key = (os.getenv("TAVILY_API_KEY") or "").strip()
web_search = None

if TavilySearch is not None and api_key:
    web_search = TavilySearch(
        max_results=5,
        topic="general",
        search_depth="advanced",
        tavily_api_key=api_key,
    )


@tool
def search_instagram_benchmark(query: str) -> str:
    """
    Search the web for relevant and current Instagram
    marketing benchmarks.

    Use this tool when an external benchmark is needed
    to evaluate a restaurant's Instagram performance.

    Do not use this tool to retrieve the restaurant's
    own Instagram data.

    The results may contain external sources, benchmarks,
    and industry findings. Verify relevance before using
    them in the qualification report.
    """

    if web_search is None:
        return (
            "No external benchmark could be fetched because TAVILY_API_KEY "
            "is not configured. Use the restaurant's own evidence only."
        )

    try:
        results = web_search.invoke({
            "query": query
        })
    except Exception as exc:  # pragma: no cover - defensive fallback
        return (
            "No external benchmark could be fetched because Tavily is unavailable: "
            f"{type(exc).__name__}. Use the restaurant's own evidence only."
        )

    return str(results)