from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

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
