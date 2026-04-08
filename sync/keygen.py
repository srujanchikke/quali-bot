import hashlib, secrets

raw  = secrets.token_hex(32)
hash = hashlib.sha256(raw.encode()).hexdigest()

print(f"\nRaw key  (Jenkins SYNC_API_KEY) : {raw}")
print(f"Hash     (local  SYNC_API_KEY_HASH) : {hash}\n")
