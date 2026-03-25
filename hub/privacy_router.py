"""Privacy router — sensitivity classification for hub messages.

Phase 2b: logging/warning only. The cloud backend decides which
local agent to target; this router classifies and logs but does NOT
block or re-route relay-dispatched messages.
"""

from __future__ import annotations

import logging
import re
from enum import Enum

logger = logging.getLogger(__name__)

# Built-in PII patterns
_BUILTIN_PATTERNS = [
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "email"),
    (r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "phone_us"),
    (r"\b\d{3}-\d{2}-\d{4}\b", "ssn"),
    (r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13})\b", "credit_card"),
    (r"\b(?:sk|pk|api|key|token|secret|password)[-_]?(?:[a-zA-Z0-9_]+[-_]?){0,3}[a-zA-Z0-9]{12,}\b", "api_key"),
]


class SensitivityLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class PrivacyRouter:
    """Classifies message sensitivity and logs the result.

    In Phase 2b, this does NOT gate or re-route messages.
    """

    def __init__(
        self,
        sensitive_keywords: list[str] | None = None,
        sensitive_patterns: list[str] | None = None,
    ) -> None:
        self._keywords = [k.lower() for k in (sensitive_keywords or [])]
        self._user_patterns = [re.compile(p, re.IGNORECASE) for p in (sensitive_patterns or [])]
        self._builtin_patterns = [
            (re.compile(p, re.IGNORECASE), label) for p, label in _BUILTIN_PATTERNS
        ]

    def classify(self, text: str) -> SensitivityLevel:
        """Classify text sensitivity.

        Returns:
            HIGH if PII or sensitive keywords detected.
            LOW otherwise.
            MEDIUM is reserved for Phase 3 LLM classification.
        """
        lower = text.lower()

        # Keyword matching
        for kw in self._keywords:
            if kw in lower:
                logger.warning(
                    "Sensitive keyword '%s' detected — classification: HIGH", kw
                )
                return SensitivityLevel.HIGH

        # User-defined patterns
        for pat in self._user_patterns:
            if pat.search(text):
                logger.warning(
                    "Sensitive pattern matched — classification: HIGH"
                )
                return SensitivityLevel.HIGH

        # Built-in PII patterns
        for pat, label in self._builtin_patterns:
            if pat.search(text):
                logger.warning(
                    "PII detected (%s) — classification: HIGH", label
                )
                return SensitivityLevel.HIGH

        return SensitivityLevel.LOW

    def check_and_log(self, text: str, agent_name: str = "") -> SensitivityLevel:
        """Classify and log the result. Does NOT block dispatch."""
        level = self.classify(text)
        if level == SensitivityLevel.HIGH:
            logger.warning(
                "Message to '%s' classified as HIGH sensitivity. "
                "In Phase 2b this is logged only — message will still be dispatched.",
                agent_name,
            )
        else:
            logger.debug("Message to '%s' classified as %s", agent_name, level.value)
        return level
