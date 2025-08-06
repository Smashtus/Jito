#!/usr/bin/env python3
"""Jito mempool SPL-token listener.

Streams pending transactions from Jito’s block engine and prints
Dexscreener-style buy/sell flow for a chosen SPL token mint. The mint
address is passed as the first command line argument::

    python jito_mempool_listener.py <MINT_ADDRESS>
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import grpc
from rich.console import Console
from rich.table import Table

# --- Modern Solana SDK imports ---
from solders.pubkey import Pubkey as PublicKey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient

import os

# Jito protobuf stubs (generated from jito_protos)
from jito_protos.auth import auth_service_pb2, auth_service_pb2_grpc
from jito_protos.searcher import searcher_service_pb2, searcher_service_pb2_grpc

# -----------------------------------------------------------------------------
# Configuration – update with your own values
# -----------------------------------------------------------------------------
BLOCK_ENGINE_URL = "https://frankfurt.mainnet.block-engine.jito.wtf:443"
AUTH_URL = "https://frankfurt.mainnet.block-engine.jito.wtf:443"
RPC_URL = "https://api.mainnet-beta.solana.com"
# Pyth SOL/USD price account. ``pythclient`` expects plain base58 strings for
# account keys, so keep this as ``str`` rather than a ``solders`` ``PublicKey``.
SOL_USD_PRICE_ACCOUNT = "J83w4WJZb5tF7rj6vVY3WbDXJ6gwHnhpYkzjX6qT3MrH"
KEYPAIR_PATH = "./auth.json"  # Keypair authorized with Jito
PAIR_ADDRESS = "0x9F5a0AD81Fe7fD5dFb84EE7A0CFb83967359BD90"
SOL_TOKEN_ADDRESS = "0x570A5D26f7765Ecb712C0924E4De545B89fD43dF"
USDT_TOKEN_ADDRESS = "0x55d398326f99059fF775485246999027B3197955"
# -----------------------------------------------------------------------------


class AuthInterceptor(grpc.aio.ClientInterceptor):
    """Adds the bearer token to every gRPC call."""

    def __init__(self, token: str):
        self._token = token

    async def intercept(self, method, request_or_iterator, call_details: grpc.ClientCallDetails):
        metadata = [] if call_details.metadata is None else list(call_details.metadata)
        metadata.append(("authorization", f"Bearer {self._token}"))
        call_details = call_details._replace(metadata=tuple(metadata))
        return await method(request_or_iterator, call_details)


async def fetch_auth_token(auth_channel: grpc.aio.Channel, kp: Keypair) -> str:
    """Request JWT token from the auth service."""
    stub = auth_service_pb2_grpc.AuthServiceStub(auth_channel)
    req = auth_service_pb2.GenerateAuthTokenRequest(
        role=auth_service_pb2.Role.Value("SEARCHER"),
        pubkey=bytes(kp.pubkey()),
        signature=b"",
    )
    resp = await stub.GenerateAuthToken(req)
    return resp.token


async def fetch_sol_price() -> float:
    """Fetch SOL/USD price from a public API.

    The original implementation used ``pythclient`` to query the on-chain Pyth
    price account, but ``pythclient`` is not always available in all
    environments.  In addition, recent versions of the library removed the
    ``get_price`` helper which caused an ``AttributeError`` at runtime.  To
    avoid depending on a heavy Solana client we fetch the price from a simple
    HTTPS endpoint instead.  If the request fails for any reason we default to
    ``0.0`` so the caller can decide how to handle missing price data.
    """

    import aiohttp

    url = "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                data = await resp.json()
                return float(data.get("price", 0.0))
    except Exception:
        return 0.0


@dataclass
class TxRow:
    time: str
    side: str
    sol: float
    token: float
    price_sol: float
    price_usd: float
    sig: str


async def process_tx(
    tx: VersionedTransaction,
    rpc: AsyncClient,
    target_mint: PublicKey,
    sol_price: float,
) -> Optional[TxRow]:
    """Return a Dexscreener-style row if the transaction touches target_mint."""
    keys = list(tx.message.account_keys)
    sig = str(tx.signatures[0])

    sim = await rpc.simulate_transaction(tx, sig_verify=False)
    if sim.value is None:
        return None

    tb = [b for b in sim.value.post_token_balances if b.mint == str(target_mint)]
    if not tb:
        return None

    token_post = tb[0]
    token_pre = next(
        (b for b in sim.value.pre_token_balances if b.owner == token_post.owner),
        None,
    )
    decimals = token_post.ui_token_amount.decimals
    post_amt = int(token_post.ui_token_amount.amount)
    pre_amt = int(token_pre.ui_token_amount.amount) if token_pre else 0
    token_delta = (post_amt - pre_amt) / 10 ** decimals

    owner_index = keys.index(PublicKey.from_string(token_post.owner))
    sol_pre = sim.value.pre_balances[owner_index]
    sol_post = sim.value.post_balances[owner_index]
    sol_delta = (sol_post - sol_pre) / 10 ** 9

    if token_delta == 0 and sol_delta == 0:
        return None

    side = "Buy" if token_delta > 0 else "Sell"
    price_in_sol = abs(sol_delta) / abs(token_delta) if token_delta != 0 else 0.0
    price_in_usd = price_in_sol * sol_price

    return TxRow(
        time=datetime.now(timezone.utc).strftime("%H:%M:%S"),
        side=side,
        sol=abs(sol_delta),
        token=abs(token_delta),
        price_sol=price_in_sol,
        price_usd=price_in_usd,
        sig=sig,
    )


async def stream_mempool(target_mint: PublicKey) -> None:
    if not os.path.exists(KEYPAIR_PATH):
        kp = Keypair()
        with open(KEYPAIR_PATH, "wb") as f:
            f.write(kp.to_bytes())
        print(f"Generated new keypair and stored at {KEYPAIR_PATH}")
    else:
        kp = Keypair.from_bytes(open(KEYPAIR_PATH, "rb").read())
        print(f"Using existing keypair at {KEYPAIR_PATH}")
    rpc = AsyncClient(RPC_URL)
    sol_price = await fetch_sol_price()

    auth_channel = grpc.aio.secure_channel(AUTH_URL, grpc.ssl_channel_credentials())
    token = await fetch_auth_token(auth_channel, kp)
    await auth_channel.close()

    interceptor = AuthInterceptor(token)
    channel = grpc.aio.secure_channel(
        BLOCK_ENGINE_URL,
        grpc.ssl_channel_credentials(),
        interceptors=(interceptor,),
    )
    stub = searcher_service_pb2_grpc.SearcherServiceStub(channel)

    request = searcher_service_pb2.PendingTxSubscriptionRequest(accounts=[])

    console = Console()
    table = Table(
        "Time", "Side", "SOL In/Out", "Token In/Out", "Price (SOL)", "Price (USD)", "Tx Hash"
    )

    async for notification in stub.SubscribePendingTransactions(request):
        for packet in notification.transactions:
            tx = VersionedTransaction.from_bytes(packet.data)
            row = await process_tx(tx, rpc, target_mint, sol_price)
            if row:
                table.add_row(
                    row.time,
                    row.side,
                    f"{row.sol:.6f}",
                    f"{row.token:.4f}",
                    f"{row.price_sol:.8f}",
                    f"${row.price_usd:.5f}",
                    row.sig,
                )
                console.print(table)
                table.rows.clear()


def main() -> None:
    parser = argparse.ArgumentParser(description="Listen to Jito mempool for a token mint")
    parser.add_argument("mint", help="Target SPL token mint address")
    args = parser.parse_args()

    target_mint = PublicKey.from_string(args.mint)
    asyncio.run(stream_mempool(target_mint))


if __name__ == "__main__":
    main()
