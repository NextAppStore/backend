"""Admin-only global platform settings.

Currently exposes a single setting — the default OpenStack network used
for ``@openstack:network``-marked deploy variables that the deploying
user leaves unset (see ``app/routers/deployments.py::create_deployment``,
``_apply_default_network``). Stored generically in the ``settings``
key-value table (``app/models.py::Setting``) so future global settings
can reuse the same table without a new migration.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.crud import settings as crud_settings
from app.database import get_db
from app.models import User
from app.schemas import DefaultNetworkResponse, DefaultNetworkUpsert
from app.services import openstack_client
from app.utils.permissions import require_admin

router = APIRouter()

_DEFAULT_NETWORK_KEY = "default_network"


def _to_response(value: dict | None, *, updated_at=None, updated_by=None) -> DefaultNetworkResponse:
    if value is None:
        return DefaultNetworkResponse(configured=False)
    return DefaultNetworkResponse(
        configured=True,
        id=value.get("id"),
        name=value.get("name"),
        mode=value.get("mode"),
        updated_at=updated_at,
        updated_by=updated_by,
    )


@router.get(
    "/settings/default-network",
    response_model=DefaultNetworkResponse,
    tags=["Admin"],
)
def get_default_network(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Always 200 — ``configured=False`` when no default has been set yet."""
    row = crud_settings.get_setting(db, _DEFAULT_NETWORK_KEY)
    value = crud_settings.get_setting_value(db, _DEFAULT_NETWORK_KEY)
    if row is None:
        return _to_response(None)
    return _to_response(value, updated_at=row.updated_at, updated_by=row.updated_by)


@router.put(
    "/settings/default-network",
    response_model=DefaultNetworkResponse,
    tags=["Admin"],
)
def put_default_network(
    payload: DefaultNetworkUpsert,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Set the platform-wide default network.

    Validated against OpenStack using the *admin's own* credentials
    (same ``user_connection`` helper used by ``GET /networks``) — this
    only confirms the network is resolvable by at least one project; it
    is the admin's responsibility to pick a network that is actually
    shared/external and visible to every deploying user's project.
    """
    with openstack_client.user_connection(db, current_user) as conn:
        found = conn.network.find_network(payload.id) or conn.network.find_network(payload.name)
        if found is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"reason": "network_not_found", "id": payload.id, "name": payload.name},
            )

    row = crud_settings.upsert_setting(
        db,
        _DEFAULT_NETWORK_KEY,
        {"id": payload.id, "name": payload.name, "mode": payload.mode},
        current_user.userId,
    )
    value = crud_settings.get_setting_value(db, _DEFAULT_NETWORK_KEY)
    return _to_response(value, updated_at=row.updated_at, updated_by=row.updated_by)
