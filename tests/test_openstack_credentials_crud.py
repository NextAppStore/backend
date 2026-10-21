"""Phase C6: CRUD-level tests for the per-user OpenStack credential row.

Exercises the encryption round-trip on real DB rows: ciphertext on disk,
plaintext only in-process. Integration-marked because Fernet round-trip
plus the BYTEA columns requires the test database.

Each test maps to one CRUD entry point in
`app/crud/openstack_credentials.py`:

  * upsert                         — write path, encrypts at rest
  * get_decrypted_for_backend      — backend-only plaintext read
  * stamp_validation               — touch-up of validation metadata
  * delete                         — idempotent removal
  * get_dispatch_envelope          — JSON-safe envelope for the job payload
"""
from __future__ import annotations

import base64
from datetime import datetime

import pytest

from app.crud import openstack_credentials as crud_creds
from app.models import OpenStackAuthType, UserOpenStackCredential
from app.schemas import OpenStackCredentialUpsert
from app.utils import crypto


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def _password_payload(**overrides) -> OpenStackCredentialUpsert:
    base = {
        "auth_type": OpenStackAuthType.PASSWORD,
        "auth_url": "https://keystone.example/v3",
        "region_name": "RegionOne",
        "interface": "public",
        "identity_api_version": "3",
        "project_id": "project-uuid",
        "project_name": "demo",
        "user_domain_name": "Default",
        "project_domain_name": "Default",
        "identifier": "alice",
        "secret": "s3cret",
    }
    base.update(overrides)
    return OpenStackCredentialUpsert(**base)


# ----------------------------------------------------------------
# upsert: ciphertext at rest
# ----------------------------------------------------------------
@pytest.mark.integration
def test_upsert_encrypts_password_at_rest(db, mock_user):
    payload = _password_payload(identifier="alice", secret="s3cret")

    row = crud_creds.upsert(db, mock_user.userId, payload, (True, None))

    # The persisted columns must be Fernet ciphertext bytes, never the raw
    # plaintext UTF-8 bytes.
    assert isinstance(row.encrypted_identifier, (bytes, bytearray, memoryview))
    assert isinstance(row.encrypted_secret, (bytes, bytearray, memoryview))
    assert bytes(row.encrypted_identifier) != b"alice"
    assert bytes(row.encrypted_secret) != b"s3cret"
    assert b"alice" not in bytes(row.encrypted_identifier)
    assert b"s3cret" not in bytes(row.encrypted_secret)

    # And the round-trip must decrypt cleanly back to the originals.
    assert crypto.decrypt(bytes(row.encrypted_identifier)) == "alice"
    assert crypto.decrypt(bytes(row.encrypted_secret)) == "s3cret"

    # Validation success stamps the timestamp and clears any prior error.
    assert row.last_validated_at is not None
    assert row.last_validation_error is None

    # Re-read straight from the DB to confirm we are not just looking at
    # in-memory attributes that bypassed the column.
    db.expire_all()
    refreshed = crud_creds.get_for_user(db, mock_user.userId)
    assert refreshed is not None
    assert crypto.decrypt(bytes(refreshed.encrypted_identifier)) == "alice"
    assert crypto.decrypt(bytes(refreshed.encrypted_secret)) == "s3cret"


# ----------------------------------------------------------------
# get_decrypted_for_backend: plaintext for the backend's own OpenStack calls
# ----------------------------------------------------------------
@pytest.mark.integration
def test_get_decrypted_for_backend_returns_plaintext(db, mock_user):
    crud_creds.upsert(
        db,
        mock_user.userId,
        _password_payload(identifier="alice", secret="s3cret"),
        (True, None),
    )

    decrypted = crud_creds.get_decrypted_for_backend(db, mock_user.userId)

    # Plaintext keys present.
    assert decrypted["identifier"] == "alice"
    assert decrypted["secret"] == "s3cret"

    # Connection metadata is carried through verbatim.
    assert decrypted["auth_type"] == OpenStackAuthType.PASSWORD.value
    assert decrypted["auth_url"] == "https://keystone.example/v3"
    assert decrypted["region_name"] == "RegionOne"
    assert decrypted["interface"] == "public"
    assert decrypted["identity_api_version"] == "3"
    assert decrypted["project_id"] == "project-uuid"
    assert decrypted["project_name"] == "demo"
    assert decrypted["user_domain_name"] == "Default"
    assert decrypted["project_domain_name"] == "Default"

    # No ciphertext leaks into the plaintext-only dict.
    assert "encrypted_identifier" not in decrypted
    assert "encrypted_secret" not in decrypted
    assert "encrypted_identifier_b64" not in decrypted
    assert "encrypted_secret_b64" not in decrypted

    # And when no credential exists, the dedicated error fires (rather
    # than a generic AttributeError further down the stack).
    crud_creds.delete(db, mock_user.userId)
    with pytest.raises(crud_creds.NoCredentialError):
        crud_creds.get_decrypted_for_backend(db, mock_user.userId)


# ----------------------------------------------------------------
# stamp_validation: metadata update without rotating ciphertext
# ----------------------------------------------------------------
@pytest.mark.integration
def test_stamp_validation_records_timestamp_and_status(db, mock_user):
    # Seed via upsert so we have a real row, but persist with a *failed*
    # validation so last_validated_at starts as None.
    row = crud_creds.upsert(
        db,
        mock_user.userId,
        _password_payload(),
        (False, "initial failure"),
    )
    assert row.last_validated_at is None
    assert row.last_validation_error == "initial failure"

    original_id_ct = bytes(row.encrypted_identifier)
    original_sec_ct = bytes(row.encrypted_secret)

    # 1. A failing stamp must NOT touch last_validated_at, but MUST
    #    overwrite the error message.
    before = datetime.utcnow()
    crud_creds.stamp_validation(db, row, (False, "still bad"))
    assert row.last_validated_at is None
    assert row.last_validation_error == "still bad"

    # 2. A passing stamp clears the error and writes a fresh timestamp.
    crud_creds.stamp_validation(db, row, (True, None))
    assert row.last_validation_error is None
    assert row.last_validated_at is not None
    assert row.last_validated_at >= before

    # 3. Ciphertext columns are untouched — stamp_validation is purely
    #    metadata, never a re-encrypt.
    db.expire_all()
    refreshed = crud_creds.get_for_user(db, mock_user.userId)
    assert refreshed is not None
    assert bytes(refreshed.encrypted_identifier) == original_id_ct
    assert bytes(refreshed.encrypted_secret) == original_sec_ct
    assert refreshed.last_validation_error is None
    assert refreshed.last_validated_at is not None


# ----------------------------------------------------------------
# delete: idempotent removal
# ----------------------------------------------------------------
@pytest.mark.integration
def test_delete_removes_row_idempotently(db, mock_user):
    crud_creds.upsert(db, mock_user.userId, _password_payload(), (True, None))
    assert crud_creds.get_for_user(db, mock_user.userId) is not None

    # First delete: row exists, returns True.
    assert crud_creds.delete(db, mock_user.userId) is True
    db.expire_all()
    assert crud_creds.get_for_user(db, mock_user.userId) is None

    # Second delete on the same user: no row, returns False, no exception.
    assert crud_creds.delete(db, mock_user.userId) is False
    assert crud_creds.get_for_user(db, mock_user.userId) is None

    # No phantom row was created as a side effect of the second call.
    remaining = db.query(UserOpenStackCredential).filter(
        UserOpenStackCredential.userId == mock_user.userId
    ).count()
    assert remaining == 0


# ----------------------------------------------------------------
# get_dispatch_envelope: JSON-safe envelope for the job payload
# ----------------------------------------------------------------
@pytest.mark.integration
def test_get_dispatch_envelope_includes_minimal_fields_for_the_job_payload(db, mock_user):
    crud_creds.upsert(
        db,
        mock_user.userId,
        _password_payload(identifier="alice", secret="s3cret"),
        (True, None),
    )

    envelope = crud_creds.get_dispatch_envelope(db, mock_user.userId)

    # ---- Required keys ----
    expected_keys = {
        "auth_type",
        "auth_url",
        "region_name",
        "interface",
        "identity_api_version",
        "project_id",
        "project_name",
        "user_domain_name",
        "project_domain_name",
        "encrypted_identifier_b64",
        "encrypted_secret_b64",
    }
    assert expected_keys.issubset(envelope.keys())

    # ---- Plaintext secrets MUST NOT appear ----
    assert "identifier" not in envelope
    assert "secret" not in envelope
    # The literal plaintext must not leak anywhere in the envelope values
    # (e.g. accidentally placed in metadata).
    for value in envelope.values():
        if isinstance(value, str):
            assert "alice" not in value or value == envelope["encrypted_identifier_b64"]
            assert "s3cret" not in value or value == envelope["encrypted_secret_b64"]

    # ---- Auth metadata serialized as the enum value (JSON-safe) ----
    assert envelope["auth_type"] == OpenStackAuthType.PASSWORD.value
    assert isinstance(envelope["auth_type"], str)

    # ---- Ciphertext is base64-ascii so it survives the JSON job payload ----
    id_b64 = envelope["encrypted_identifier_b64"]
    sec_b64 = envelope["encrypted_secret_b64"]
    assert isinstance(id_b64, str)
    assert isinstance(sec_b64, str)

    # Decoding the b64 must yield the same Fernet ciphertext that's on disk,
    # which in turn decrypts back to the original plaintext.
    id_ct = base64.b64decode(id_b64.encode("ascii"))
    sec_ct = base64.b64decode(sec_b64.encode("ascii"))
    assert crypto.decrypt(id_ct) == "alice"
    assert crypto.decrypt(sec_ct) == "s3cret"

    # ---- Missing credential raises NoCredentialError, not a generic error ----
    crud_creds.delete(db, mock_user.userId)
    with pytest.raises(crud_creds.NoCredentialError):
        crud_creds.get_dispatch_envelope(db, mock_user.userId)
