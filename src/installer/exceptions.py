"""Errors raised from this package."""


class InstallerError(Exception):
    """All exceptions raised from this package's code."""


class InvalidWheelSource(InstallerError):
    """When a wheel source violates a contract, or is not supported."""


class InvalidWheelTags(InvalidWheelSource):
    """When a wheel's compatibility tags are missing or syntactically invalid.

    This covers malformed compressed tags, both in the wheel filename and in
    the ``Tag`` fields of the ``WHEEL`` metadata file.
    """


class WheelTagMismatch(InvalidWheelSource):
    """When the wheel filename's tags don't match the ``WHEEL`` ``Tag`` fields.

    The Cartesian expansion of the compressed interpreter/ABI/platform tag in
    the wheel filename must be exactly equal to the expanded set of all
    ``Tag`` fields found in the ``WHEEL`` file.
    """


class IncompatibleWheel(InstallerError):
    """When a wheel is otherwise valid but incompatible with the given target."""


class NoCompatibleTags(IncompatibleWheel):
    """When none of the target's wheel tags match any of the wheel's tags."""


class UnsupportedPythonVersion(IncompatibleWheel):
    """When the target's Python version doesn't satisfy ``Requires-Python``."""


class InvalidRequiresPython(InvalidWheelSource):
    """When the wheel's ``Requires-Python`` constraint is not a valid specifier set."""


class InvalidTarget(InstallerError):
    """When the target description is invalid or self-contradictory.

    This includes malformed target wheel tags, an incomplete or invalid exact
    target Python version, and a launcher kind that is incompatible with the
    target's platform tags.
    """
