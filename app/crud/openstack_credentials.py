"""CRUD for the per-user OpenStack credential row.

Two flavours of `get_*` are exposed because the dispatch path and the
backend's own quotas endpoint have different needs:

* `get_dispatch_envelope` — JSON-safe dict with **base64 ciphertext**
  for the job payload. The backend never decrypts here.
* `get_decrypted_for_backend` — plaintext dict for the backend's own
  OpenStack calls (quotas). Plaintext lives only in this process's
  memory for the duration of the request.

The plaintext envelope MUST NOT be put into a job payload.
"""
from __future__ import annotations

import base64
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import UserOpenStackCredential
from app.schemas import OpenStackCredentialUpsert
from app.utils import crypto
from app.utils.time import utcnow


class NoCredentialError(Exception):
    """Raised when a deploy is attempted before the user has uploaded creds."""


def get_for_user(db: Session, user_id: UUID) -> UserOpenStackCredential | None:
    return (
        db.query(UserOpenStackCredential)
        .filter(UserOpenStackCredential.userId == user_id)
        .first()
    )


def upsert(
    db: Session,
    user_id: UUID,
    payload: OpenStackCredentialUpsert,
    validation_result: tuple[bool, str | None],
) -> UserOpenStackCredential:
    """Create or update the user's credential row.

    `validation_result` comes from `services.openstack_validator.validate`.
    On success we stamp `last_validated_at` and clear the error;
    on failure we still persist (so the user can fix it via the UI) and
    record the message.
    """
    ok, error = validation_result
    now = utcnow()

    enc_id = crypto.encrypt(payload.identifier)
    enc_secret = crypto.encrypt(payload.secret)

    row = get_for_user(db, user_id)
    if row is None:
        row = UserOpenStackCredential(
            userId=user_id,
            auth_type=payload.auth_type,
            auth_url=payload.auth_url,
            region_name=payload.region_name,
            interface=payload.interface or "public",
            identity_api_version=payload.identity_api_version or "3",
            project_id=payload.project_id,
            project_name=payload.project_name,
            user_domain_name=payload.user_domain_name,
            project_domain_name=payload.project_domain_name,
            encrypted_identifier=enc_id,
            encrypted_secret=enc_secret,
            last_validated_at=now if ok else None,
            last_validation_error=None if ok else error,
        )
        db.add(row)
    else:
        row.auth_type = payload.auth_type
        row.auth_url = payload.auth_url
        row.region_name = payload.region_name
        row.interface = payload.interface or "public"
        row.identity_api_version = payload.identity_api_version or "3"
        row.project_id = payload.project_id
        row.project_name = payload.project_name
        row.user_domain_name = payload.user_domain_name
        row.project_domain_name = payload.project_domain_name
        row.encrypted_identifier = enc_id
        row.encrypted_secret = enc_secret
        row.last_validated_at = now if ok else row.last_validated_at
        row.last_validation_error = None if ok else error

    db.commit()
    db.refresh(row)
    return row


def stamp_validation(
    db: Session,
    row: UserOpenStackCredential,
    validation_result: tuple[bool, str | None],
) -> UserOpenStackCredential:
    """Update validation metadata after a `/test` call without rotating ciphertext."""
    ok, error = validation_result
    if ok:
        row.last_validated_at = utcnow()
        row.last_validation_error = None
    else:
        row.last_validation_error = error
    db.commit()
    db.refresh(row)
    return row


def delete(db: Session, user_id: UUID) -> bool:
    row = get_for_user(db, user_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def _common_metadata(row: UserOpenStackCredential) -> dict:
    return {
        "auth_type": row.auth_type.value if hasattr(row.auth_type, "value") else row.auth_type,
        "auth_url": row.auth_url,
        "region_name": row.region_name,
        "interface": row.interface or "public",
        "identity_api_version": row.identity_api_version or "3",
        "project_id": row.project_id,
        "project_name": row.project_name,
        "user_domain_name": row.user_domain_name,
        "project_domain_name": row.project_domain_name,
    }


def get_dispatch_envelope(db: Session, user_id: UUID) -> dict:
    """Build the JSON-safe envelope shipped to the worker in the job payload.

    Ciphertext is base64-encoded straight from Postgres — no decryption
    hop in the backend. The worker decrypts in-process.
    """
    row = get_for_user(db, user_id)
    if row is None:
        raise NoCredentialError(f"No OpenStack credential for user {user_id}")
    envelope = _common_metadata(row)
    envelope["encrypted_identifier_b64"] = base64.b64encode(row.encrypted_identifier).decode("ascii")
    envelope["encrypted_secret_b64"] = base64.b64encode(row.encrypted_secret).decode("ascii")
    return envelope


def get_decrypted_for_backend(db: Session, user_id: UUID) -> dict:
    """Plaintext dict for backend-only OpenStack calls (quotas).

    NEVER put this into a job payload. Use `get_dispatch_envelope` for that path.
    """
    row = get_for_user(db, user_id)
    if row is None:
        raise NoCredentialError(f"No OpenStack credential for user {user_id}")
    out = _common_metadata(row)
    out["identifier"] = crypto.decrypt(row.encrypted_identifier)
    out["secret"] = crypto.decrypt(row.encrypted_secret)
    return out
