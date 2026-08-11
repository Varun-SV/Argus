"""Typed identifiers for the Argus Test Evidence Specification (ATES)."""

from __future__ import annotations

import re
import uuid
from typing import ClassVar, TypeVar

_SUFFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
TAtesId = TypeVar("TAtesId", bound="AtesId")


class AtesId(str):
    """A validated, prefixed ATES identifier."""

    prefix: ClassVar[str] = ""

    def __new__(cls, value: str) -> "AtesId":
        if not isinstance(value, str):
            raise TypeError(f"{cls.__name__} must be created from a string")
        expected = f"{cls.prefix}-"
        if not cls.prefix or not value.startswith(expected):
            raise ValueError(f"{cls.__name__} must start with {expected!r}")
        suffix = value[len(expected):]
        if not _SUFFIX_RE.fullmatch(suffix):
            raise ValueError(
                f"{cls.__name__} suffix must be 1-96 ASCII letters, digits, '_' or '-'"
            )
        return str.__new__(cls, value)

    @classmethod
    def new(cls: type[TAtesId]) -> TAtesId:
        return cls(f"{cls.prefix}-{uuid.uuid4().hex}")


class RunId(AtesId): prefix = "RUN"
class EventId(AtesId): prefix = "EVT"
class StepId(AtesId): prefix = "STEP"
class StepAttemptId(AtesId): prefix = "STEPATT"
class ActionId(AtesId): prefix = "ACTION"
class ActionOperationId(AtesId): prefix = "ACTOP"
class ObservationId(AtesId): prefix = "OBS"
class AssertionId(AtesId): prefix = "ASSERT"
class ArtifactId(AtesId): prefix = "ART"
class FindingId(AtesId): prefix = "FINDING"
class CorrectionId(AtesId): prefix = "CORR"
class FinalizationId(AtesId): prefix = "FINAL"
