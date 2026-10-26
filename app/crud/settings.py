"""CRUD for generic admin-editable global key-value settings."""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Setting
from app.utils.time import utcnow


def get_setting(db: Session, key: str) -> Setting | None:
    return db.query(Setting).filter(Setting.key == key).first()


def get_setting_value(db: Session, key: str) -> dict | None:
    row = get_setting(db, key)
    if row is None or not row.value:
        return None
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return None


def upsert_setting(db: Session, key: str, value: dict, updated_by_user_id: UUID) -> Setting:
    row = get_setting(db, key)
    now = utcnow()
    if row is None:
        row = Setting(
            key=key,
            value=json.dumps(value),
            updated_at=now,
            updated_by=updated_by_user_id,
        )
        db.add(row)
    else:
        row.value = json.dumps(value)
        row.updated_at = now
        row.updated_by = updated_by_user_id
    db.commit()
    db.refresh(row)
    return row
