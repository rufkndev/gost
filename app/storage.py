"""Хранилище файлов: имена по UUID, без исходных имён на диске; шифрование Fernet по флагу."""

from __future__ import annotations

import uuid
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import Settings


class Storage:
    def __init__(self, root: Path, fernet_key: str | None = None) -> None:
        self.root = root
        self.fernet = Fernet(fernet_key.encode()) if fernet_key else None

    @classmethod
    def from_settings(cls, s: Settings) -> Storage:
        key = s.storage_fernet_key.get_secret_value() if s.storage_encryption else None
        return cls(s.storage_dir, key)

    def _path(self, key: str) -> Path:
        if not key or "/" in key or ".." in key:
            raise ValueError("bad storage key")
        return self.root / key

    def save(self, data: bytes) -> str:
        key = uuid.uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        payload = self.fernet.encrypt(data) if self.fernet else data
        self._path(key).write_bytes(payload)
        return key

    def load(self, key: str) -> bytes:
        payload = self._path(key).read_bytes()
        return self.fernet.decrypt(payload) if self.fernet else payload

    def delete(self, key: str | None) -> bool:
        if not key:
            return False
        p = self._path(key)
        if p.exists():
            p.unlink()
            return True
        return False

    def exists(self, key: str | None) -> bool:
        return bool(key) and self._path(key or "").exists()

    def free_bytes(self) -> int:
        import shutil

        self.root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.root).free
