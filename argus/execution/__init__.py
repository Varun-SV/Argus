"""Execution-location abstractions for Argus sessions."""

from argus.execution.base import (
    ExecutionEnvironment,
    ExecutionEnvironmentError,
    ExecutionEnvironmentInfo,
    LocalExecutionEnvironment,
    create_execution_environment,
)
from argus.execution.capsule import CapsuleExecutionEnvironment

__all__ = [
    "CapsuleExecutionEnvironment",
    "ExecutionEnvironment",
    "ExecutionEnvironmentError",
    "ExecutionEnvironmentInfo",
    "LocalExecutionEnvironment",
    "create_execution_environment",
]
