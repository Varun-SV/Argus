"""Public ATES report API.

The implementation lives in :mod:`argus.ates.reports_runtime`; keeping this
module as a small stable facade avoids duplicate renderer paths.
"""
from .reports_runtime import *  # noqa: F401,F403
from .reports_runtime import __all__
