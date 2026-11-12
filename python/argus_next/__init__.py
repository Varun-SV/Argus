"""Python API of the Argus preview build (Rust re-architecture).

This package is the preview counterpart of the documented Argus Python API
(docs/rearchitecture/specification.md section 10, parity inventory section 10). It is shipped in
the ``argus-next`` distribution next to the native ``argus-next`` command, so it can be installed
alongside the supported ``argus`` package from ``argus-app-testing`` without touching it. At
G-SWITCH the same code is published as ``argus`` in ``argus-app-testing``.

Phase P0 exposes only the version of the native core.
"""

from __future__ import annotations

from argus_next import _native
from argus_next._native import version

__all__ = ["__version__", "native_version", "version"]

#: Version of the native core; equal to the distribution version.
__version__: str = _native.version()


def native_version() -> str:
    """Return the version of the native Argus core (the PyO3 extension)."""
    return _native.version()
