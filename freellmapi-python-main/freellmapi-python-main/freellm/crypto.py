from __future__ import annotations
import hashlib
import os
import secrets
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ALGORITHM = "aes-256-gcm"
KEY_BYTES = 32
KEY_HEX_LEN = 64
PLACEHOLDER = "your-64-char-hex-key-here"
KEY_FILE_NAME = ".encryption-key"

_cached_key: bytes | None = None

def _parse_hex_key(value: str, source: str) -> bytes:
    if len(value) != KEY_HEX_LEN or not all(c in "0123456789abcdefABCDEF" for c in value):
        raise ValueError(
            f"Invalid ENCRYPTION_KEY ({source}): expected {KEY_HEX_LEN} hex chars (32 bytes), got {len(value)} chars. "
            f"Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return bytes.fromhex(value)

def is_dev_fallback_allowed() -> bool:
    return os.getenv("NODE_ENV", os.getenv("ENV", "development")) != "production"

def _key_file_path(db_path: str | None) -> Path | None:
    if not db_path or db_path == ":memory:":
        return None
    return Path(db_path).parent / KEY_FILE_NAME

def _write_key_file_atomic(key_file: Path, hex_key: str) -> None:
    tmp = key_file.parent / f"{KEY_FILE_NAME}.tmp-{secrets.token_hex(6)}"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(hex_key, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    tmp.replace(key_file)
    try:
        os.chmod(key_file, 0o600)
    except Exception:
        pass

def init_encryption_key(db_path: str | None = None, conn=None) -> None:
    global _cached_key
    env_key = os.getenv("ENCRYPTION_KEY")
    if env_key and env_key != PLACEHOLDER:
        _cached_key = _parse_hex_key(env_key.strip(), "env")
        return
    if not is_dev_fallback_allowed():
        raise RuntimeError(
            "ENCRYPTION_KEY is required in production. "
            f"Set a {KEY_HEX_LEN}-char hex key (python -c \"import secrets; print(secrets.token_hex(32))\")"
        )
    kf = _key_file_path(db_path)
    if kf is None:
        # in-memory: store in DB settings table if conn given
        if conn is not None:
            try:
                row = conn.execute("SELECT value FROM settings WHERE key='encryption_key'").fetchone()
                if row:
                    _cached_key = _parse_hex_key(row[0], "db")
                    return
                _cached_key = secrets.token_bytes(KEY_BYTES)
                conn.execute("INSERT INTO settings(key,value) VALUES('encryption_key',?)", (_cached_key.hex(),))
                conn.commit()
                return
            except Exception:
                pass
        _cached_key = secrets.token_bytes(KEY_BYTES)
        return
    if kf.exists():
        val = kf.read_text(encoding="utf-8").strip()
        _cached_key = _parse_hex_key(val, "file")
        return
    # legacy in DB
    if conn is not None:
        try:
            row = conn.execute("SELECT value FROM settings WHERE key='encryption_key'").fetchone()
            if row:
                migrated = _parse_hex_key(row[0], "db")
                _write_key_file_atomic(kf, migrated.hex())
                _cached_key = migrated
                # verify roundtrip
                probe = encrypt("roundtrip")
                if decrypt(probe["encrypted"], probe["iv"], probe["authTag"]) != "roundtrip":
                    _cached_key = None
                    raise RuntimeError("[crypto] key migration roundtrip failed")
                conn.execute("DELETE FROM settings WHERE key='encryption_key'")
                conn.commit()
                return
        except Exception:
            pass
    _cached_key = secrets.token_bytes(KEY_BYTES)
    _write_key_file_atomic(kf, _cached_key.hex())

def get_encryption_key() -> bytes:
    if _cached_key is None:
        raise RuntimeError("Encryption key not initialized. Call init_encryption_key() first.")
    return _cached_key

def encryption_key_fingerprint() -> str | None:
    if _cached_key is None:
        return None
    return "sha256:" + hashlib.sha256(_cached_key).hexdigest()[:16]

def is_encryption_key_initialized() -> bool:
    return _cached_key is not None

def encrypt(text: str) -> dict:
    key = get_encryption_key()
    iv = secrets.token_bytes(12)  # GCM standard 96-bit nonce
    aesgcm = AESGCM(key)
    ct_with_tag = aesgcm.encrypt(iv, text.encode("utf-8"), None)
    # cryptography appends 16-byte tag at end
    encrypted = ct_with_tag[:-16].hex()
    auth_tag = ct_with_tag[-16:].hex()
    return {"encrypted": encrypted, "iv": iv.hex(), "authTag": auth_tag}

def decrypt(encrypted: str, iv: str, auth_tag: str) -> str:
    key = get_encryption_key()
    iv_b = bytes.fromhex(iv)
    ct = bytes.fromhex(encrypted) + bytes.fromhex(auth_tag)
    aesgcm = AESGCM(key)
    pt = aesgcm.decrypt(iv_b, ct, None)
    return pt.decode("utf-8")

def mask_key(key: str) -> str:
    if len(key) < 5:
        return "****"
    if len(key) <= 8:
        return "****" + key[-2:]
    return key[:4] + "..." + key[-4:]

def generate_unified_key() -> str:
    return "freellmapi-" + secrets.token_hex(24)
