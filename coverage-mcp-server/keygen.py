"""
Generate a coverage-mcp-server API key pair.

Usage:
    python3 keygen.py

Output:
    Raw key  → give this to the client (Claude Desktop config, curl, etc.)
    Key hash → store this in MCP_API_KEY_HASH on the server

The raw key is never stored on the server. If the hash leaks, the raw key
cannot be recovered from it (SHA-256 of 256 bits of entropy is infeasible
to brute-force).
"""

import hashlib
import secrets

raw_key = secrets.token_hex(32)  # 256 bits of randomness
key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

print()
print("=== Coverage MCP Server — API Key ===")
print()
print(f"  Raw key (client)        : {raw_key}")
print(f"  SHA-256 hash (server)   : {key_hash}")
print()
print("--- Server (.env) ---")
print(f"MCP_API_KEY_HASH={key_hash}")
print()
print("--- Client (Authorization header) ---")
print(f"Authorization: Bearer {raw_key}")
print()
print("--- Claude Desktop config ---")
print(f'"env": {{ "MCP_API_KEY": "{raw_key}" }}')
print()
print("Keep the raw key secret. Only the hash belongs on the server.")
