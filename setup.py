"""
Polymarket V2 API Credential Generator — Bear Oracle Confirmed Sniper

Reads POLY_PRIVATE_KEY and POLY_FUNDER_ADDRESS from '.env', derives API
credentials via py-clob-client-v2, and writes them back to '.env'.

Usage:
  1. Copy .env.example to .env and fill in:
       POLY_PRIVATE_KEY=0x...
       POLY_FUNDER_ADDRESS=0x...

  2. Run:
       python setup.py

  3. .env will be updated with all credentials ready for the bot.

Note: The allowance check at the end may show $0.00 — this is a known
cosmetic false alarm in the V2 CLOB backend. Run approve_usdc.py separately
to set on-chain approvals for the V2 exchange contracts.
"""

import sys
from pathlib import Path

from dotenv import dotenv_values, set_key

ENV_FILE = ".env"

try:
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds, AssetType, BalanceAllowanceParams
    from py_clob_client_v2.constants import POLYGON
except ImportError:
    print("py-clob-client-v2 not installed.")
    print("Run: pip install -r requirements.txt")
    sys.exit(1)


def run_setup() -> None:
    # ── Read from .env ────────────────────────────────────────────
    if not Path(ENV_FILE).exists():
        print(f"'{ENV_FILE}' not found.")
        print(
            "Copy .env.example to .env and fill in POLY_PRIVATE_KEY and POLY_FUNDER_ADDRESS."
        )
        sys.exit(1)

    env = dotenv_values(ENV_FILE)
    pk = env.get("POLY_PRIVATE_KEY", "").strip()
    funder = env.get("POLY_FUNDER_ADDRESS", "").strip()
    # Signature type MUST match the wallet kind, or the derived API key binds to
    # the wrong address → "Invalid api key" (401) on every L2 call. 0 = EOA
    # (no funder); 1 = email/Magic proxy; 2 = browser-wallet (Gnosis Safe) proxy.
    # A non-empty funder means a proxy wallet → sig_type must be 1 or 2.
    sig_type = int(env.get("POLY_SIG_TYPE", "0").strip().strip("'\"") or "0")

    if not pk or pk == "0x...":
        print("POLY_PRIVATE_KEY is missing or not set in .env")
        sys.exit(1)

    print("--- Generating API Credentials ---")
    print(f"  Private key: {pk[:6]}...{pk[-4:]}")
    print(f"  Funder: {funder or '(not set)'}")
    print(f"  Sig type: {sig_type}")

    # ── Initialize CLOB client (Level 1 — key derivation only) ───
    # use_server_time=True syncs the EIP-712 timestamp with Polymarket's clock,
    # preventing "Could not derive api key" 400 errors caused by local clock skew.
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=pk,
        chain_id=POLYGON,
        funder=funder or None,
        signature_type=sig_type,
        use_server_time=True,
    )

    # ── Derive API credentials ────────────────────────────────────
    try:
        creds = client.create_or_derive_api_key()
    except Exception as exc:
        print(f"Failed to derive API credentials: {exc}")
        print()
        print("  Common causes:")
        print("  1. Wallet not registered on Polymarket — visit app.polymarket.com,")
        print("     connect this wallet, and complete the sign-in flow at least once.")
        print("  2. Clock skew — already mitigated by use_server_time=True above.")
        print("  3. Wrong POLY_FUNDER_ADDRESS — must match the wallet that holds pUSD.")
        sys.exit(1)

    print(f"  API Key:        {creds.api_key}")
    print(f"  API Secret:     {creds.api_secret[:8]}...")
    print(f"  API Passphrase: {creds.api_passphrase[:8]}...")

    # ── Write back to .env ────────────────────────────────────────
    set_key(ENV_FILE, "POLY_PRIVATE_KEY", pk)
    set_key(ENV_FILE, "POLY_FUNDER_ADDRESS", funder)
    set_key(ENV_FILE, "POLY_API_KEY", creds.api_key)
    set_key(ENV_FILE, "POLY_API_SECRET", creds.api_secret)
    set_key(ENV_FILE, "POLY_API_PASSPHRASE", creds.api_passphrase)
    set_key(ENV_FILE, "POLY_SIG_TYPE", str(sig_type))

    # ── Re-initialize client with Level 2 credentials ────────────
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=pk,
        chain_id=POLYGON,
        funder=funder or None,
        signature_type=sig_type,
        use_server_time=True,
        creds=ApiCreds(
            api_key=creds.api_key,
            api_secret=creds.api_secret,
            api_passphrase=creds.api_passphrase,
        ),
    )

    # ── Balance / allowance check (cosmetic — see note above) ─────
    print("\n--- Verifying Balance & Allowance (V2 CLOB view) ---")
    try:
        resp = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        raw_balance = (
            resp.get("balance", 0)
            if isinstance(resp, dict)
            else getattr(resp, "balance", 0)
        )
        raw_allowance = (
            resp.get("allowance", 0)
            if isinstance(resp, dict)
            else getattr(resp, "allowance", 0)
        )
        balance = float(raw_balance or 0)
        allowance = float(raw_allowance or 0)
        if balance > 1_000_000:
            balance /= 1e6
        if allowance > 1_000_000:
            allowance /= 1e6
        print(f"  Balance:   ${balance:.2f} pUSD")
        print(f"  Allowance: ${allowance:.2f} pUSD")
        if allowance == 0:
            print(
                "  (!) Allowance shows $0.00 — this is a known V2 cosmetic false alarm."
            )
            print("      Run approve_usdc.py to set real on-chain approvals.")
        else:
            print("  Allowance confirmed via CLOB view.")
    except Exception as exc:
        print(f"  Could not verify via CLOB: {exc}")
        print("  Run approve_usdc.py to set on-chain approvals.")

    print(f"\nDone. Credentials written to '{ENV_FILE}'")
    print("Next steps:")
    print("  1. python wrap_pusd.py       — convert USDC.e to pUSD")
    print("  2. python approve_usdc.py    — on-chain approvals for V2 contracts")
    print("  3. python shadow.py          — verify pipeline before trading")


if __name__ == "__main__":
    run_setup()
