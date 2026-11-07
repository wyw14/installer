"""Errors raised from this package."""


class InstallerError(Exception):
    """All exceptions raised from this package's code."""


class InvalidWheelSource(InstallerError):
    """When a wheel source violates a contract, or is not supported."""


class InvalidBytecodeOptimizationLevel(ValueError, InstallerError):
    """When a bytecode optimization level other than 0, 1 or 2 is provided."""


class InvalidSourceDateEpoch(ValueError, InstallerError):
    """When ``SOURCE_DATE_EPOCH`` has an invalid or unsupported value.

    This includes non-integer values and timestamps outside of the range that
    can be represented in ZIP archive member metadata.
    """


class BytecodeCompilationError(InstallerError):
    """When bytecode compilation of an installed source file fails.

    The error always refers to the logical (final runtime) source path, never
    to any staging directory.
    """
