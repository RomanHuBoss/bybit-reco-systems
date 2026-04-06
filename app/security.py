from __future__ import annotations

import secrets
from dataclasses import dataclass
import ipaddress

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


def _is_loopback_host(client_host: str | None) -> bool:
    host = str(client_host or "").strip()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_authorized(expected_api_key: str | None, provided_api_key: str | None, *, client_host: str | None = None) -> bool:
    """Проверяет доступ к mutating API.

    Без ADMIN_API_KEY mutating-endpoints не должны быть открыты наружу: это слишком
    опасно для боевого/публично доступного стенда. При пустом ключе оставляем только
    loopback-доступ для локальной разработки. Если client_host не передан (например,
    helper-тест или внутренний вызов), сохраняем прежнюю permissive-семантику, чтобы
    сам helper оставался обратно совместимым вне HTTP-контекста.
    """
    if not expected_api_key:
        if client_host is None:
            return True
        return _is_loopback_host(client_host)
    if not provided_api_key:
        return False
    return secrets.compare_digest(str(expected_api_key), str(provided_api_key))
