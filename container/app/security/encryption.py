import logging
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import settings
from app.db.repository import SecretsRepository

SESSION_SIGNING_INFO = b"agent-assist session signing"

logger = logging.getLogger(__name__)

FERNET_KEY_PATH = Path(settings.fernet_key_path)

_fernet: Fernet | None = None
_key_lock = threading.Lock()
_key_bytes: bytes | None = None


def _load_or_generate_key() -> bytes:
    """Load (or generate on first start) the Fernet key.

    COR-4: result is cached in ``_key_bytes`` and the load-or-generate
    block is guarded by a thread lock so two concurrent first-time
    callers cannot race and overwrite each other's freshly generated key.
    """
    global _key_bytes
    if _key_bytes is not None:
        return _key_bytes
    with _key_lock:
        if _key_bytes is not None:
            return _key_bytes
        if FERNET_KEY_PATH.exists():
            # Cold-path sync file I/O: first-use only, then cached in-process.
            # Off-loaded to a thread via asyncio.to_thread() in main.py lifespan
            # so it does not block the event loop at runtime.
            key = FERNET_KEY_PATH.read_bytes().strip()
            logger.info("Fernet key loaded from file")
        else:
            key = Fernet.generate_key()
            # Cold-path sync file I/O: key creation happens once per deployment.
            # Off-loaded to a thread via asyncio.to_thread() in main.py lifespan.
            FERNET_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
            FERNET_KEY_PATH.write_bytes(key)
            # NOTE: chmod has limited effect on Windows. The /data directory
            # should additionally be secured via NTFS permissions.
            FERNET_KEY_PATH.chmod(0o600)
            logger.info("New Fernet key generated")
        _key_bytes = key
        return _key_bytes


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_generate_key())
    return _fernet


def get_fernet_key() -> bytes:
    """Return the raw Fernet key bytes (for deriving secondary keys)."""
    return _load_or_generate_key()


def get_session_signing_key() -> bytes:
    """Derive a domain-separated signing key for the admin session cookie.

    SEC-6: previously the session signer used ``sha256(fernet_key)`` directly,
    which meant a leak of the Fernet key compromised both stored secrets and
    every issued session cookie. We now derive a 32-byte key via HKDF-SHA256
    with a fixed ``info`` label so the signing key is independent from the
    raw Fernet key.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=SESSION_SIGNING_INFO,
    )
    return hkdf.derive(_load_or_generate_key())


def encrypt(plaintext: str) -> bytes:
    return get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes) -> str:
    try:
        return get_fernet().decrypt(ciphertext).decode("utf-8")
    except InvalidToken:
        logger.warning("Failed to decrypt secret -- key may have changed")
        raise ValueError("Decryption failed") from None


async def store_secret(key: str, plaintext: str) -> None:
    encrypted = encrypt(plaintext)
    await SecretsRepository.set(key, encrypted)


async def retrieve_secret(key: str) -> str | None:
    encrypted = await SecretsRepository.get(key)
    if encrypted is None:
        return None
    try:
        return decrypt(encrypted)
    except ValueError:
        logger.error("Failed to decrypt secret '%s' -- Fernet key may have been rotated", key)
        raise RuntimeError(f"Failed to decrypt secret '{key}'") from None


async def delete_secret(key: str) -> None:
    await SecretsRepository.delete(key)


def export_fernet_key() -> str:
    """Export the Fernet key as a base64-encoded string for backup."""
    key = _load_or_generate_key()
    return key.decode("utf-8")


def is_fernet_key_present() -> bool:
    """Check if a Fernet key file exists."""
    return FERNET_KEY_PATH.exists()
