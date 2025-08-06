"""Async gRPC stub for the searcher service.

The implementation here is intentionally minimal and yields no pending
transactions.  It exists solely so that example scripts can run without the
real protobuf-generated bindings.
"""

from __future__ import annotations

from . import searcher_service_pb2


class SearcherServiceStub:
    def __init__(self, channel) -> None:  # pragma: no cover - simple storage
        self.channel = channel

    async def SubscribePendingTransactions(self, request: searcher_service_pb2.PendingTxSubscriptionRequest):
        if False:  # pragma: no branch - generator requires a yield
            yield searcher_service_pb2.PendingTxNotification()
        return

