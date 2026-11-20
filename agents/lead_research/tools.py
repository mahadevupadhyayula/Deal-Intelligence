"""Search interface and adapters.

WHAT: `SearchTool` protocol, `SearchResult`, the Tavily adapter and a fixture adapter.
WHERE: Injected into the research graph via `JobContext.search`; the graph never imports
Tavily directly.
WHY: A tiny interface means swapping providers is a one-file change and tests replay recorded
results deterministically (no network, no cost). Errors are classified `transient`
(timeouts, 5xx, network) vs permanent (bad key, bad request, exhausted plan) so the retry
helper retries only what can succeed on a second try.
"""
import asyncio
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    content: str


class SearchError(Exception):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class SearchTool(Protocol):
    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]: ...


class FixtureSearch:
    """Replays recorded results by exact query; unknown queries return `default`."""

    def __init__(self, by_query: dict[str, list[SearchResult]],
                 default: list[SearchResult] | None = None):
        self.by_query = by_query
        self.default = default or []
        self.queries: list[str] = []  # every query received, for assertions

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self.queries.append(query)
        return list(self.by_query.get(query, self.default))[:max_results]


class TavilySearch:
    def __init__(self, api_key: str, timeout: float = 20.0):
        from tavily import AsyncTavilyClient  # lazy: only needed when actually searching

        self._client = AsyncTavilyClient(api_key=api_key)
        self._timeout = timeout

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        import httpx
        from tavily import errors as tavily_errors

        try:
            resp = await asyncio.wait_for(
                self._client.search(query=query, max_results=max_results, search_depth="basic",
                                    timeout=self._timeout),
                timeout=self._timeout + 5,
            )
        except (TimeoutError, tavily_errors.TimeoutError, httpx.TransportError) as exc:
            raise SearchError(f"search timed out or network error: {exc}", transient=True) from exc
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            raise SearchError(f"search HTTP {code}", transient=code == 429 or code >= 500) from exc
        except Exception as exc:
            raise SearchError(f"search failed: {type(exc).__name__}", transient=False) from exc
        return [
            SearchResult(url=r["url"], title=r.get("title") or "", content=r["content"])
            for r in resp.get("results", []) if r.get("url") and r.get("content")
        ]
