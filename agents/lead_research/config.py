"""Research limits as a plain value object.

WHAT: `ResearchConfig`, the budget/retry/size limits for one research run.
WHERE: Built from `Settings` by the handler; tests construct it directly.
WHY: Passing limits explicitly (instead of reading global settings inside nodes) keeps the
graph a pure function of its inputs, so golden tests can run with tiny budgets.
"""
from dataclasses import dataclass

from agents.config import Settings


@dataclass(frozen=True)
class ResearchConfig:
    max_searches: int = 8
    max_llm_calls: int = 8
    max_refine_loops: int = 1
    max_sources: int = 8
    source_char_limit: int = 6000
    max_total_chars: int = 40000
    retry_attempts: int = 2
    retry_backoff_seconds: float = 1.0
    injection_scan_enabled: bool = True

    @classmethod
    def from_settings(cls, s: Settings) -> "ResearchConfig":
        return cls(
            max_searches=s.research_max_searches, max_llm_calls=s.research_max_llm_calls,
            max_refine_loops=s.research_max_refine_loops, max_sources=s.research_max_sources,
            source_char_limit=s.research_source_char_limit,
            max_total_chars=s.research_max_total_chars, retry_attempts=s.research_retry_attempts,
            retry_backoff_seconds=s.research_retry_backoff_seconds,
            injection_scan_enabled=s.injection_scan_enabled,
        )
