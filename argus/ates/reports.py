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

from .audit_round6 import install as _install_audit_round6

# Chain verification validates the complete canonical audit-row shape before
# representing a detached ledger as locally chain verified.
_install_audit_round6()

from .audit_round7 import install as _install_audit_round7

# Round 7 distinguishes unsafe ledger entries from true absence, validates
# approval chronology, and rejects ambiguous duplicate audit dedupe keys.
_install_audit_round7()

from .audit_round8 import install as _install_audit_round8

# Round 8 validates the complete approval record envelope, including privacy-
# classified reason data, authentication/request-generation containers, and
# rejects unclassified extension fields before authentication grants authority.
_install_audit_round8()

from .reports_round2 import install as _install_reports_round2

_install_reports_round2()

from .reports_round3 import install as _install_reports_round3

# Derived files disclose their exact approval/audit snapshot and never
# self-certify regenerated_verified; that state belongs to the active verifier.
_install_reports_round3()

from .reports_round4 import install as _install_reports_round4

# A report generation is one trust unit: all members are staged first and an
# existing verified generation is restored if commit or regeneration fails.
_install_reports_round4()

from .reports_round5 import install as _install_reports_round5

# An externally trusted report-manifest digest authenticates a point-in-time
# derived bundle independently of later detached-ledger freshness.
_install_reports_round5()

from .reports_round6 import install as _install_reports_round6

# Retained checkpoint projections preserve the validated capture context and
# explicit Finding relationship instead of dropping canonical provenance.
_install_reports_round6()

from .reports_runtime import *  # noqa: F401,F403,E402
from .reports_runtime import __all__  # noqa: E402
