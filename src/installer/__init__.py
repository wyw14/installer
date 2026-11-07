"""A library for installing Python wheels."""

__version__ = "1.1.0"
__all__ = [
    "Target",
    "check_compatibility",
    "install",
    "install_for_target",
]

from installer._compat import Target, check_compatibility, install_for_target
from installer._core import install
