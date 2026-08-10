"""Execution-location abstractions for Argus sessions."""

from argus.execution.base import (
    ExecutionEnvironment,
    ExecutionEnvironmentError,
    ExecutionEnvironmentInfo,
    LocalExecutionEnvironment,
    create_execution_environment,
)
from argus.execution.capsule import CapsuleExecutionEnvironment
from argus.execution.secure_capsule import SecureCapsuleExecutionEnvironment

__all__ = [
    "CapsuleExecutionEnvironment",
    "SecureCapsuleExecutionEnvironment",
    "ExecutionEnvironment",
    "ExecutionEnvironmentError",
    "ExecutionEnvironmentInfo",
    "LocalExecutionEnvironment",
    "create_execution_environment",
]
