"""Argus — universal application testing driven by multimodal LLMs.

Argus watches an application the way a person would — through screenshots,
the OS accessibility tree, terminal output — then drives it to satisfy tests
written as a mix of natural-language steps and structured assertions.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("argus-app-testing")
except PackageNotFoundError:
    # Source trees that have not been installed do not have distribution
    # metadata. Release and packaged builds always install/build the project
    # first, so this fallback is intentionally descriptive rather than a fake
    # release version.
    __version__ = "0+unknown"
