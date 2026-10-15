"""Short-lived, encrypted user OAuth grant for Feishu document reads."""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import time
from typing import Any, Callable

from Crypto.Cipher import AES

from .feishu_oauth import OAuthGrant
from .feishu_review import FeishuDocumentRevisionReader
from .models import IdentityContext
from .store import SQLiteStore


DOC_READ_SCOPE = "docx:document:readonly"


class UserTokenVault:
    """Store only a short-lived encrypted access token; no refresh token."""

    def __init__(self, store: SQLiteStore, key_b64: str, *, clock: Callable[[], float] = time.time) -> None:
        try:
            key = base64.urlsafe_b64decode(key_b64.encode("ascii"))
        except (ValueError, binascii.Error, UnicodeEncodeError) as exc:
            raise ValueError("user token encryption key is invalid") from exc
        if len(key) != 32:
            raise ValueError("user token encryption key must be 32 bytes")
        self.store = store
        self.key = key
        self.clock = clock

    @staticmethod
    def _aad(identity: IdentityContext) -> bytes:
        return f"{identity.tenant_id}:{identity.actor_id}:{identity.subject_ref_hash}".encode()

    def replace(self, identity: IdentityContext, grant: OAuthGrant) -> None:
        if identity.auth_mode != "user_oauth":
            raise PermissionError("user OAuth identity is required")
        if not grant.access_token or not grant.access_token.strip():
            raise PermissionError("user OAuth access token is missing")
        scopes = identity.scopes
        lifetime = int(grant.expires_in or 0)
        if DOC_READ_SCOPE not in scopes or lifetime <= 30:
            self.store.delete_user_oauth_token(identity.actor_id)
            return
        nonce = secrets.token_bytes(12)
        cipher = AES.new(self.key, AES.MODE_GCM, nonce=nonce)
        cipher.update(self._aad(identity))
        ciphertext, tag = cipher.encrypt_and_digest(grant.access_token.encode("utf-8"))
        payload = base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")
        self.store.save_user_oauth_token(
            actor_id=identity.actor_id, tenant_id=identity.tenant_id,
            subject_ref_hash=identity.subject_ref_hash, scopes=scopes,
            token_ciphertext=payload, expires_at=int(self.clock()) + lifetime,
        )

    def get(self, identity: IdentityContext) -> str:
        if identity.auth_mode not in {"feishu_event", "user_oauth"}:
            raise PermissionError("user document authorization requires a member identity")
        row = self.store.get_user_oauth_token(identity.actor_id)
        if (row is None or row["tenant_id"] != identity.tenant_id
                or row["subject_ref_hash"] != identity.subject_ref_hash
                or DOC_READ_SCOPE not in json.loads(row["scopes_json"])
                or int(row["expires_at"]) <= int(self.clock()) + 30):
            raise PermissionError("user document authorization is missing or expired")
        try:
            payload = base64.urlsafe_b64decode(row["token_ciphertext"])
            nonce, tag, ciphertext = payload[:12], payload[12:28], payload[28:]
            cipher = AES.new(self.key, AES.MODE_GCM, nonce=nonce)
            cipher.update(self._aad(identity))
            token = cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
            if not token.strip():
                raise ValueError("empty user access token")
            return token
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise PermissionError("user document authorization cannot be verified") from exc

    def revoke(self, actor_id: str) -> None:
        self.store.delete_user_oauth_token(actor_id)


class FeishuUserDocumentReader:
    """Use a member's current OAuth token for metadata and every text block."""

    def __init__(self, vault: UserTokenVault, client: Any) -> None:
        if not getattr(getattr(client, "config", None), "enable_set_token", False):
            raise ValueError("user document client must require an explicit access token")
        self.vault = vault
        self.client = client

    def read_document(self, identity: IdentityContext, document_id: str) -> dict[str, Any]:
        token = self.vault.get(identity)
        return FeishuDocumentRevisionReader(self.client, user_access_token=token).read_document(document_id)


__all__ = ["DOC_READ_SCOPE", "FeishuUserDocumentReader", "UserTokenVault"]
