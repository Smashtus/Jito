"""Minimal protobuf-like classes for the searcher service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class Packet:
    """A simplified representation of a transaction packet."""

    data: bytes = b""


@dataclass
class PendingTxNotification:
    """Notification containing zero or more transaction packets."""

    transactions: List[Packet] = field(default_factory=list)


@dataclass
class PendingTxSubscriptionRequest:
    """Subscription request message.

    Only the ``accounts`` field is required by the example script.
    """

    accounts: List[bytes]

