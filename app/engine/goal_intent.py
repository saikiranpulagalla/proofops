from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


def _norm_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").strip()


def normalize_subject(value: str) -> str:
    """Canonical comparison form for an explicitly stated company subject.

    This is deliberately not entity resolution: it removes presentation differences
    only, preserving internal words so ``ACME``, ``ACME Ltd``, and ``ACME Holdings``
    remain distinct.
    """
    text = _norm_text(value).casefold()
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" \t\r\n.,;:!?()[]{}\"'‘’")
    return re.sub(r"(?:['’]s)$", "", text).strip()


# Deliberately narrow, high-precision prohibitions.  We do not treat phrases such as
# "without contacting them unnecessarily" as a blanket prohibition because that means
# "avoid unnecessary contact", not "never contact".
_NO_CONTACT_PATTERNS = (
    re.compile(r"\b(?:do\s+not|don't|dont|never|must\s+not|mustn't|mustnt)\s+(?:contact|email|message)\b", re.I),
    re.compile(r"\b(?:do\s+not|don't|dont|never|must\s+not|mustn't|mustnt)\s+send\s+(?:an?\s+|any\s+)?(?:email|message)\b", re.I),
    re.compile(r"\b(?:no|zero)\s+(?:external\s+)?(?:email|message|contact)\b", re.I),
)


class GoalSubjectState(StrEnum):
    NO_SUBJECT_ASSERTION = "NO_SUBJECT_ASSERTION"
    MATCHES_STRUCTURED_SUBJECT = "MATCHES_STRUCTURED_SUBJECT"
    CONFLICTS_WITH_STRUCTURED_SUBJECT = "CONFLICTS_WITH_STRUCTURED_SUBJECT"
    AMBIGUOUS_SUBJECT = "AMBIGUOUS_SUBJECT"
    UNRESOLVED_PLAUSIBLE_SUBJECT = "UNRESOLVED_PLAUSIBLE_SUBJECT"

_NAME = r"[A-Za-z0-9][A-Za-z0-9&._-]*(?:\s+[A-Za-z0-9&._-]+){0,3}"
_SUBJECT_PATTERNS = (
    re.compile(rf"\b(?P<company>{_NAME}?)[\'’]s\s+renewal\b", re.I),
    re.compile(rf"\b(?:handle|check|manage|review|process|ensure|make\s+sure)\s+(?:the\s+)?renewal\s+(?:for|of)\s+(?:our\s+)?(?P<company>{_NAME}?)(?:\s+account)?(?=[.,;:!?]|$)", re.I),
    re.compile(rf"\b(?:handle|check|manage|review|process|ensure)\s+(?P<company>{_NAME}?)\s+renewals?\b", re.I),
    re.compile(rf"\brenew\s+(?P<company>{_NAME}?)(?=[.,;:!?]|$)", re.I),
    re.compile(rf"\b(?:follow\s+up|contact)\s+(?:with\s+)?(?P<company>{_NAME}?)\s+(?:about|regarding)\s+(?:the\s+)?renewal\b", re.I),
    re.compile(rf"\b(?:make\s+sure|ensure)\s+(?P<company>{_NAME}?)\s+renewal\s+(?:is\s+)?handled\b", re.I),
)
_MULTI_SUBJECT_PATTERNS = (
    re.compile(rf"\b(?:handle|check|manage|review|compare|renew)\s+(?P<left>{_NAME}?)\s+(?:and|,)\s+(?P<right>{_NAME}?)(?:\s+(?:before\s+handling\s+)?)?\s*renewals?\b", re.I),
    re.compile(rf"\bcompare\s+(?P<left>{_NAME}?)\s+and\s+(?P<right>{_NAME}?)(?:\s+before)?\b.*\brenewal\b", re.I),
)


@dataclass(frozen=True)
class GoalIntent:
    forbid_external_contact: bool = False
    subject_hint: str | None = None
    subject_state: GoalSubjectState = GoalSubjectState.NO_SUBJECT_ASSERTION

    @property
    def subject_conflict(self) -> bool:
        return self.subject_state == GoalSubjectState.CONFLICTS_WITH_STRUCTURED_SUBJECT

    @property
    def constraints(self) -> tuple[str, ...]:
        values: list[str] = []
        if self.forbid_external_contact:
            values.append("NO_EXTERNAL_CONTACT")
        if self.subject_state == GoalSubjectState.CONFLICTS_WITH_STRUCTURED_SUBJECT:
            values.append("SUBJECT_CONFLICT")
        elif self.subject_state == GoalSubjectState.AMBIGUOUS_SUBJECT:
            values.append("AMBIGUOUS_GOAL_SUBJECT")
        elif self.subject_state == GoalSubjectState.UNRESOLVED_PLAUSIBLE_SUBJECT:
            values.append("UNRESOLVED_GOAL_SUBJECT")
        return tuple(values)

    def as_json(self) -> dict[str, object]:
        return {
            "forbid_external_contact": self.forbid_external_contact,
            "subject_hint": self.subject_hint,
            "subject_state": self.subject_state.value,
            "subject_conflict": self.subject_conflict,
            "constraints": list(self.constraints),
        }


def _subjects(goal: str) -> tuple[str, ...]:
    def clean(values: list[str]) -> tuple[str, ...]:
        result: list[str] = []
        normalized: set[str] = set()
        for candidate in values:
            value = _norm_text(candidate).strip(" .,-")
            value = re.sub(r"^(?:handle|manage|ensure|process|review|check|make\s+sure)\s+", "", value, flags=re.I)
            key = normalize_subject(value)
            if value and key and key not in normalized and key not in {"this", "that", "the", "our", "customer", "account"}:
                result.append(value)
                normalized.add(key)
        return tuple(result)

    for pattern in _MULTI_SUBJECT_PATTERNS:
        match = pattern.search(goal)
        if match:
            return clean([match.group("left"), match.group("right")])
    for pattern in _SUBJECT_PATTERNS:
        match = pattern.search(goal)
        if match:
            return clean([match.group("company")])
    return ()


def compile_goal_intent(goal_text: str, company: str) -> GoalIntent:
    """Compile only high-confidence user intent into deterministic constraints.

    The compiler is intentionally conservative.  Anything it cannot understand remains
    available to the planner as untrusted natural language, while safety-critical explicit
    prohibitions and obvious subject contradictions are enforced deterministically.
    """
    goal = _norm_text(goal_text)
    expected_company = _norm_text(company)
    forbid = any(pattern.search(goal) for pattern in _NO_CONTACT_PATTERNS)

    subjects = _subjects(goal)
    expected_normalized = normalize_subject(expected_company)
    hint: str | None = None
    if len(subjects) > 1:
        state = GoalSubjectState.AMBIGUOUS_SUBJECT
    elif len(subjects) == 1:
        hint = subjects[0]
        state = (
            GoalSubjectState.MATCHES_STRUCTURED_SUBJECT
            if expected_normalized and normalize_subject(hint) == expected_normalized
            else GoalSubjectState.CONFLICTS_WITH_STRUCTURED_SUBJECT
        )
    else:
        state = GoalSubjectState.NO_SUBJECT_ASSERTION
    return GoalIntent(
        forbid_external_contact=forbid,
        subject_hint=hint,
        subject_state=state,
    )
