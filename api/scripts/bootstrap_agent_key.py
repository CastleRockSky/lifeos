"""
bootstrap_agent_key.py — Issue a new agent API key.

Run from inside the api container:
    docker exec -it lifeos-api python scripts/bootstrap_agent_key.py \
        --name HealthBot --domains medical

Prints the plaintext key ONCE — store it somewhere safe (1Password, env var on
the agent host, etc.). Only the SHA-256 hash is persisted, so a lost key cannot
be recovered.
"""

import argparse
import asyncio
import hashlib
import os
import secrets
import sys

import asyncpg


def make_key() -> str:
    # 32 random bytes → ~43 char URL-safe token. Prefixed for human recognition.
    return "lifeos_agent_" + secrets.token_urlsafe(32)


async def main():
    parser = argparse.ArgumentParser(description="Issue a LifeOS agent API key.")
    parser.add_argument("--name", required=True, help="Agent name, e.g. HealthBot")
    parser.add_argument(
        "--domains",
        required=True,
        help="Comma-separated allowed domains (e.g. 'medical' or 'medical,vet'). "
             "Use '*' to allow all domains.",
    )
    args = parser.parse_args()

    domains = [d.strip() for d in args.domains.split(",") if d.strip()]
    if not domains:
        print("--domains must include at least one domain.", file=sys.stderr)
        sys.exit(1)

    plaintext = make_key()
    key_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://lifeos:lifeos@postgres:5432/lifeos",
    )
    conn = await asyncpg.connect(database_url)
    try:
        row = await conn.fetchrow("""
            INSERT INTO agent_api_keys (key_hash, agent_name, allowed_domains)
            VALUES ($1, $2, $3)
            RETURNING id, created_at
        """, key_hash, args.name, domains)
    finally:
        await conn.close()

    print()
    print("Agent key issued.")
    print(f"  id:       {row['id']}")
    print(f"  name:     {args.name}")
    print(f"  domains:  {', '.join(domains)}")
    print(f"  created:  {row['created_at'].isoformat()}")
    print()
    print("Plaintext key (shown only once — store securely):")
    print(f"  {plaintext}")
    print()
    print("Use as: X-Agent-Key: <key>")


if __name__ == "__main__":
    asyncio.run(main())
