"""The LLM judge for evals (SPEC.md §10.2): per-insight scores and pairwise deck comparison."""

from evals.judge.judge import (
    DIMENSIONS,
    JudgeConfig,
    JudgeUnavailable,
    Judgment,
    LLMJudge,
    PairwiseResult,
)

__all__ = [
    "DIMENSIONS",
    "JudgeConfig",
    "JudgeUnavailable",
    "Judgment",
    "LLMJudge",
    "PairwiseResult",
]
