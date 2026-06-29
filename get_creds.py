"""
One-shot CLOB API credential generator using curl_cffi to bypass Cloudflare.
Run this locally (not on the server) after pip install curl_cffi.

Usage:
    python get_creds.py
"""

import sys
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values, set_key

ENV_FILE = ".env"

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    print("curl_cffi not installed. Run: pip install curl_cffi")
    sys.exit(1)

try:
    from py_clob_client_v2.signing.eip712 import sign_clob_auth_message
    from py_clob_client_v2.signer import Signer
    from py_clob_client_v2.constants import POLYGON
except ImportError:
    print("py-clob-client-v2 not installed. Run: pip install py-clob-client-v2")
    sys.exit(1)

CLOB_HOST = "https://clob.polymarket.com"


def get_server_time() -> int:
    resp = cffi_requests.get(
        f"{CLOB_HOST}/time",
        impersonate="chrome",
        timeout=10,
    )
    data = resp.json()
    if isinstance(data, dict):
        return int(data.get("time", datetime.now().timestamp()))
    return int(data)


def build_l1_headers(signer: Signer, timestamp: int, nonce: int = 0) -> dict:
    signature = sign_clob_auth_message(signer, timestamp, nonce)
    return {
        "POLY_ADDRESS": signer.address(),
        "POLY_SIGNATURE": signature,
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_NONCE": str(nonce),
    }


def create_api_key(signer: Signer, timestamp: int) -> dict | None:
    headers = build_l1_headers(signer, timestamp)
    resp = cffi_requests.post(
        f"{CLOB_HOST}/auth/api-key",
        headers=headers,
        impersonate="chrome",
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.json()
    print(f"  create_api_key failed ({resp.status_code}): {resp.text[:200]}")
    return None


def derive_api_key(signer: Signer, timestamp: int) -> dict | None:
    headers = build_l1_headers(signer, timestamp)
    resp = cffi_requests.get(
        f"{CLOB_HOST}/auth/derive-api-key",
        headers=headers,
        impersonate="chrome",
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.json()
    print(f"  derive_api_key failed ({resp.status_code}): {resp.text[:200]}")
    return None


def main() -> None:
    if not Path(ENV_FILE).exists():
        print(f"'{ENV_FILE}' not found. Copy .env.example to .env first.")
        sys.exit(1)

    env = dotenv_values(ENV_FILE)
    pk = env.get("POLY_PRIVATE_KEY", "").strip()
    funder = env.get("POLY_FUNDER_ADDRESS", "").strip()

    if not pk or pk == "0x...":
        print("POLY_PRIVATE_KEY is missing or not set in .env")
        sys.exit(1)

    print(f"  Private key: {pk[:6]}...{pk[-4:]}")
    print(f"  Funder:      {funder or '(not set)'}")

    signer = Signer(pk, POLYGON)
    print(f"  Address:     {signer.address()}")

    print("\n--- Fetching server time ---")
    ts = get_server_time()
    print(f"  Server timestamp: {ts}")

    print("\n--- Trying create_api_key ---")
    data = create_api_key(signer, ts)

    if data is None:
        print("\n--- Falling back to derive_api_key ---")
        ts = get_server_time()
        data = derive_api_key(signer, ts)

    if data is None:
        print(
            "\nFailed to get credentials. Check that your wallet is registered on app.polymarket.com."
        )
        sys.exit(1)

    api_key = data.get("apiKey") or data.get("api_key", "")
    secret = data.get("secret") or data.get("api_secret", "")
    passphrase = data.get("passphrase") or data.get("api_passphrase", "")

    print(f"\n  API Key:        {api_key}")
    print(f"  API Secret:     {secret[:8]}...")
    print(f"  API Passphrase: {passphrase[:8]}...")

    set_key(ENV_FILE, "POLY_API_KEY", api_key)
    set_key(ENV_FILE, "POLY_API_SECRET", secret)
    set_key(ENV_FILE, "POLY_API_PASSPHRASE", passphrase)
    set_key(ENV_FILE, "POLY_SIG_TYPE", "0")

    print(f"\nDone. Credentials written to '{ENV_FILE}'")
    print("Next steps:")
    print(
        "  scp .env <server>:~/polymarket-arbitrage-bot/bear-oracle-confirmed-sniper/.env"
    )
    print("  python wrap_pusd.py")
    print("  python approve_usdc.py")


if __name__ == "__main__":
    main()
