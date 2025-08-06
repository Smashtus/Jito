"""Minimal protobuf-like structures for the Jito auth service.

These classes mimic the small portion of the real generated protobuf code
required by :mod:`jito_mempool_listener`.  They are *not* a full replacement for
protobuf generated types but allow the examples in this repository to run
without the optional `mev-protos` dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(Enum):
    """Enumeration of auth roles.

    Only the ``SEARCHER`` role is implemented as it is the only one used by the
    sample scripts.
    """

    ROLE_UNSPECIFIED = 0
    SEARCHER = 1

    @classmethod
    def Value(cls, name: str) -> int:
        """Return the numeric value for *name* similar to protobuf enums."""
        return cls[name].value


@dataclass
class GenerateAuthTokenRequest:
    """Simplified request message for :class:`AuthService`.

    Parameters mirror the real protobuf message but no validation is performed.
    """

    role: int
    pubkey: bytes
    signature: bytes


@dataclass
class GenerateAuthTokenResponse:
    token: str

