"""Execution-location abstractions for Argus sessions."""

from argus.execution.base import (
    ExecutionEnvironment,
    ExecutionEnvironmentError,
    ExecutionEnvironmentInfo,
    LocalExecutionEnvironment,
    create_execution_environment,
)

__all__ = [
    "ExecutionEnvironment",
    "ExecutionEnvironmentError",
    "ExecutionEnvironmentInfo",
    "LocalExecutionEnvironment",
    "create_execution_environment",
]
