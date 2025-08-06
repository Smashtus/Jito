#!/usr/bin/env python3
"""Generate a keypair file for Jito authentication."""

from solders.keypair import Keypair

KEYPAIR_PATH = "./auth.json"


def main() -> None:
    kp = Keypair()
    with open(KEYPAIR_PATH, "wb") as f:
        f.write(kp.to_bytes())
    print(f"Saved new keypair to {KEYPAIR_PATH}")
    print(f"Public key: {kp.pubkey()}")


if __name__ == "__main__":
    main()
