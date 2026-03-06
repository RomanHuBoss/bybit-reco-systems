from __future__ import annotations

import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet


@dataclass
class KeyStore:
    master_key: bytes

    @staticmethod
    def from_env(master_key: str | None) -> "KeyStore | None":
        if not master_key:
            return None
        return KeyStore(master_key=master_key.encode("utf-8"))

    def encrypt(self, data: str) -> str:
        f = Fernet(self.master_key)
        return f.encrypt(data.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str:
        f = Fernet(self.master_key)
        return f.decrypt(token.encode("utf-8")).decode("utf-8")


def is_authorized(expected_api_key: str | None, provided_api_key: str | None) -> bool:
    """Optional admin-key auth.

    If ADMIN_API_KEY is not configured, mutating endpoints stay open for local/dev use.
    When configured, require constant-time equality.
    """
    if not expected_api_key:
        return True
    if not provided_api_key:
        return False
    return secrets.compare_digest(str(expected_api_key), str(provided_api_key))
