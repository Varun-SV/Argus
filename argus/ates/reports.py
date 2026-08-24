"""Public ATES report API.

The implementation lives in :mod:`argus.ates.reports_runtime`; keeping this
module as a small stable facade avoids duplicate renderer paths.
"""
from .audit_round2 import install as _install_audit_round2

# Install detached-ledger transaction semantics before reports_runtime imports
# approval/audit validators so report regeneration observes the same authority
# rules as the public approval API.
_install_audit_round2()

from .reports_round2 import install as _install_reports_round2

_install_reports_round2()

from .reports_runtime import *  # noqa: F401,F403,E402
from .reports_runtime import __all__  # noqa: E402
