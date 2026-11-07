"""A library for installing Python wheels."""

__version__ = "1.1.0"
__all__ = ["Components", "install"]

from installer._core import install
from installer.components import Components
