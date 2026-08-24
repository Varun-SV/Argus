"""Public ATES report API.

The implementation lives in :mod:`argus.ates.reports_runtime`; keeping this
module as a small stable facade avoids duplicate renderer paths.
"""
from .audit_round2 import install as _install_audit_round2

# Install detached-ledger transaction semantics before reports_runtime imports
# approval/audit validators so report regeneration observes the same authority
# rules as the public approval API.
_install_audit_round2()

from .audit_round3 import install as _install_audit_round3

# Round 3 strengthens dedupe semantics and approval retry identity while
# preserving the round-2 transaction/consumer-visibility protocol.
_install_audit_round3()

from .audit_round4 import install as _install_audit_round4

# Round 4 gives only held writer transactions a narrow trailing-partial repair
# contract; ordinary/read-only detached-ledger validation remains strict.
_install_audit_round4()

from .audit_round5 import install as _install_audit_round5

# Round 5 separates crash-retry identity from approval lifecycle generation so
# approve -> revoke -> approve again creates a new effective operation while
# retries within either generation still converge to the original durable row.
_install_audit_round5()

from .reports_round2 import install as _install_reports_round2

_install_reports_round2()

from .reports_round3 import install as _install_reports_round3

# Derived files disclose their exact approval/audit snapshot and never
# self-certify regenerated_verified; that state belongs to the active verifier.
_install_reports_round3()

from .reports_runtime import *  # noqa: F401,F403,E402
from .reports_runtime import __all__  # noqa: E402
