from __future__ import annotations

import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet


@dataclass
class KeyStore:
    master_key: bytes

    @staticmethod
    def from_env(master_key: str | None) -> "KeyStore | None":
        """Build a keystore from env and fail fast on malformed Fernet keys.

        Без этой проверки приложение могло принять битый MASTER_KEY на старте,
        а упасть только позже — в момент первой encrypt/decrypt операции. Для
        production-контура это плохая деградация: ошибка конфигурации должна
        проявляться сразу и явно.
        """
        if not master_key:
            return None
        encoded = master_key.encode("utf-8")
        try:
            Fernet(encoded)
        except Exception as exc:
            raise ValueError("MASTER_KEY must be a valid Fernet key") from exc
        return KeyStore(master_key=encoded)

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
