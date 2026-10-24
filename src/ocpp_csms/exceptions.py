"""Exceptions raised by :mod:`ocpp_csms`."""

from __future__ import annotations


class CsmsError(Exception):
    """Base class for every error raised by this package."""


class CsmsConnectionError(CsmsError):
    """The charge point is not connected, or its connection dropped before it
    answered."""


class CsmsTimeoutError(CsmsError, TimeoutError):
    """The charge point did not answer a request in time.

    The charge point may still have acted on the request.
    """


class CsmsCallError(CsmsError):
    """A request failed at the OCPP level.

    Either the charge point answered with a ``CallError``, or the request or
    its answer does not match the OCPP 1.6 schema.
    """

    def __init__(self, code: str, description: str) -> None:
        """Create the error from an OCPP error code and description."""
        super().__init__(f"{code}: {description}")
        self.code = code
        """OCPP error code, for example ``NotImplemented`` or
        ``FormatViolation``."""
        self.description = description
        """Text describing the error."""
