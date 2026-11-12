"""Entry point for the packaged desktop app (PyInstaller): opens the Argus window."""

from argus2.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
