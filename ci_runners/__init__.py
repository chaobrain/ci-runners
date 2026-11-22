"""ci-runners: the host layer of chaobrain's shared self-hosted runners (design.md).

Stdlib only. Pure decisions live in their own modules (``policy``, ``claim``,
``part``, ``inventory``, ...); the I/O around them is thin.
"""
from pathlib import Path
from importlib.metadata import PackageNotFoundError, version

_release_version = Path(__file__).resolve().parent.parent / 'VERSION'
if _release_version.is_file():
    __version__ = _release_version.read_text().strip()
else:
    try:
        __version__ = version('ci-runners')
    except PackageNotFoundError:
        # Legacy probes copy individual helpers without a release or distribution.
        __version__ = '0+unknown'
