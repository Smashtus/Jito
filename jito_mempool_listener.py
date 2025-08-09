#!/usr/bin/env python3
"""Jito mempool SPL-token listener.

Streams pending transactions from Jito’s block engine and prints
Dexscreener-style buy/sell flow for a chosen SPL token mint using the
official ``jito_searcher_client`` library. The mint address is passed as
the first command line argument::

    python jito_mempool_listener.py <MINT_ADDRESS>
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import random
import time

import logging
from rich.console import Console
from rich.table import Table

# --- Modern Solana SDK imports ---
from solders.pubkey import Pubkey as PublicKey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient

import os

# Official client handles authentication and gRPC streaming
from jito_searcher_client import get_async_searcher_client
from jito_searcher_client.generated.searcher_pb2 import (
    PendingTxSubscriptionRequest,
)

# -----------------------------------------------------------------------------
# Configuration – update with your own values
# -----------------------------------------------------------------------------
# Block engine host (region-specific). ``jito_searcher_client`` handles TLS and auth.
BLOCK_ENGINE_HOST = "frankfurt.mainnet.block-engine.jito.wtf"
RPC_URL = "https://api.mainnet-beta.solana.com"
# Pyth SOL/USD price account. ``pythclient`` expects plain base58 strings for
# account keys, so keep this as ``str`` rather than a ``solders`` ``PublicKey``.
SOL_USD_PRICE_ACCOUNT = "J83w4WJZb5tF7rj6vVY3WbDXJ6gwHnhpYkzjX6qT3MrH"
KEYPAIR_PATH = "./auth.json"  # Keypair authorized with Jito
PAIR_ADDRESS = "0x9F5a0AD81Fe7fD5dFb84EE7A0CFb83967359BD90"
SOL_TOKEN_ADDRESS = "0x570A5D26f7765Ecb712C0924E4De545B89fD43dF"
USDT_TOKEN_ADDRESS = "0x55d398326f99059fF775485246999027B3197955"
# -----------------------------------------------------------------------------


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
    val = getattr(sim, "value", None)
    if not val:
        return None

    tb_post = [b for b in (val.post_token_balances or []) if b.mint == str(target_mint)]
    if not tb_post:
        return None

    token_post = tb_post[0]
    token_pre = next(
        (b for b in (val.pre_token_balances or []) if b.owner == token_post.owner),
        None,
    )

    decimals = token_post.ui_token_amount.decimals or 0
    post_amt = int(token_post.ui_token_amount.amount or 0)
    pre_amt = int((token_pre.ui_token_amount.amount if token_pre else 0) or 0)
    token_delta = (post_amt - pre_amt) / (10 ** decimals if decimals else 1)

    try:
        owner_pk = PublicKey.from_string(token_post.owner)
        owner_index = list(tx.message.account_keys).index(owner_pk)
        sol_pre = (val.pre_balances or [0] * len(tx.message.account_keys))[owner_index]
        sol_post = (val.post_balances or [0] * len(tx.message.account_keys))[owner_index]
        sol_delta = (sol_post - sol_pre) / 1e9
    except Exception:
        sol_delta = 0.0

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


logger = logging.getLogger(__name__)


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

    sol_price_holder = {"p": await fetch_sol_price()}
    logger.debug("Fetched SOL price: %s", sol_price_holder["p"])

    async def sol_price_refresher(interval: int = 15) -> None:
        while True:
            p = await fetch_sol_price()
            if p > 0:
                sol_price_holder["p"] = p
            await asyncio.sleep(interval)

    asyncio.create_task(sol_price_refresher(15))

    console = Console()
    rows = []
    last_flush = time.time()

    while True:
        try:
            logger.debug("Connecting to block engine at %s", BLOCK_ENGINE_HOST)
            client = await get_async_searcher_client(BLOCK_ENGINE_HOST, kp)
            request = PendingTxSubscriptionRequest(accounts=[])
            async for notification in client.SubscribePendingTransactions(request):
                for packet in notification.transactions:
                    tx = VersionedTransaction.from_bytes(packet.data)
                    row = await process_tx(tx, rpc, target_mint, sol_price_holder["p"])
                    if row:
                        rows.append(row)

                now = time.time()
                if rows and (now - last_flush) >= 0.2:
                    table = Table(
                        "Time",
                        "Side",
                        "SOL In/Out",
                        "Token In/Out",
                        "Price (SOL)",
                        "Price (USD)",
                        "Tx Hash",
                    )
                    for r in rows:
                        table.add_row(
                            r.time,
                            r.side,
                            f"{r.sol:.6f}",
                            f"{r.token:.4f}",
                            f"{r.price_sol:.8f}",
                            f"${r.price_usd:.5f}",
                            r.sig,
                        )
                    console.print(table)
                    rows.clear()
                    last_flush = now
        except Exception as e:
            logger.warning("Stream error: %s; retrying…", e)
            await asyncio.sleep(min(60, 1 + random.random() * 2))
            continue


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Listen to Jito mempool for a token mint"
    )
    parser.add_argument("mint", help="Target SPL token mint address")
    parser.add_argument(
        "--debug", action="store_true", help="Enable verbose debug logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    target_mint = PublicKey.from_string(args.mint)
    asyncio.run(stream_mempool(target_mint))


if __name__ == "__main__":
    main()
