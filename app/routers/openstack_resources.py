"""
Read API for the OpenStack resources of the calling user.

Used by the wizard so the user no longer has to copy UUIDs from the
Horizon dashboard. Each endpoint:

- Authenticates via Keycloak token
- Obtains an OpenStack connection from the service layer (per-request)
- Caches the response 60 s process-locally (see ``services/openstack_client``)
- Reduces the SDK object to a flat dict — only the fields that the
  frontend needs for display + selection. We do not want to leak SDK
  structure (sensitive fields, unwanted size).

Error strategy: 502 for OpenStack-side failures (no 500 — that is
reserved for "backend bug"). The frontend then renders a banner
"OpenStack not reachable, enter ID manually". 412 if credentials are missing.

Structure: every endpoint is "open a connection, iterate one SDK
collection, project each object to a flat dict". That scaffolding lives
once in :func:`_listing`; each endpoint supplies the two things that
actually differ — where the objects come from and what a row looks like.
The row shapes are module-level ``_row_*`` functions, because they are
the wire contract with the frontend and deserve names.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services import openstack_client
from app.utils.keycloak_auth import get_current_user_keycloak

logger = logging.getLogger(__name__)

router = APIRouter()


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def _safe_get(obj: Any, *names: str, default: Any = None) -> Any:
    """
    SDK objects are partly dict-like, partly property objects.
    We try all passed names in order.
    """
    for n in names:
        try:
            v = getattr(obj, n, None)
            if v is not None:
                return v
        except Exception:  # noqa: BLE001 — some properties raise lazily
            continue
    return default


def _list_with_oserror(
    user: User,
    kind: str,
    filters: dict | None,
    fetch_fn,
) -> list[dict]:
    """
    Wrapper that runs ``fetch_fn`` through the TTL cache and
    translates OpenStack exceptions into 502s.
    """
    try:
        return openstack_client.cached_list(
            user_id=user.userId,
            kind=kind,
            filters=filters,
            fetch=fetch_fn,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "OpenStack list %s failed for user %s: %s", kind, user.userId, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "openstack_list_failed", "kind": kind, "message": str(exc)},
        )


def _listing(
    db: Session,
    user: User,
    kind: str,
    *,
    source: Callable[[Any], Iterable[Any]],
    row: Callable[[Any], dict],
    keep: Callable[[Any], bool] | None = None,
    filters: dict | None = None,
) -> list[dict]:
    """Run one cached, error-translated resource listing.

    ``source`` receives the open connection and returns the SDK
    collection to walk; ``row`` projects one SDK object onto the flat
    dict the frontend consumes; ``keep`` optionally drops objects before
    projection. ``filters`` becomes part of the cache key, so any
    endpoint whose result depends on a query parameter must pass it —
    otherwise a filtered response would be served to an unfiltered
    request.

    The connection is opened inside the fetch callback, not around it,
    so a cache hit never touches OpenStack at all.
    """
    def fetch() -> list[dict]:
        with openstack_client.user_connection(db, user) as conn:
            return [row(o) for o in source(conn) if keep is None or keep(o)]

    return _list_with_oserror(user, kind, filters, fetch)


# ----------------------------------------------------------------
# Row shapes — the wire contract with the frontend's pickers.
# ----------------------------------------------------------------
def _row_network(n: Any) -> dict:
    return {
        "id": _safe_get(n, "id"),
        "name": _safe_get(n, "name") or "",
        "description": _safe_get(n, "description") or "",
        "shared": bool(_safe_get(n, "is_shared", "shared", default=False)),
        "external": _is_external(n),
        "status": _safe_get(n, "status") or "",
    }


def _is_external(n: Any) -> bool:
    """A network flagged ``router:external`` — i.e. a floating-IP pool."""
    return bool(_safe_get(n, "is_router_external", "router:external", default=False))


def _row_pool(n: Any) -> dict:
    return {
        "id": _safe_get(n, "id"),
        "name": _safe_get(n, "name") or "",
        "description": _safe_get(n, "description") or "",
    }


def _row_subnet(s: Any) -> dict:
    return {
        "id": _safe_get(s, "id"),
        "name": _safe_get(s, "name") or "",
        "cidr": _safe_get(s, "cidr") or "",
        "ip_version": _safe_get(s, "ip_version", default=4),
        "network_id": _safe_get(s, "network_id"),
        "gateway_ip": _safe_get(s, "gateway_ip") or "",
    }


def _row_flavor(f: Any) -> dict:
    return {
        "id": _safe_get(f, "id"),
        "name": _safe_get(f, "name") or "",
        "vcpus": _safe_get(f, "vcpus", default=0) or 0,
        "ram": _safe_get(f, "ram", default=0) or 0,        # MB
        "disk": _safe_get(f, "disk", default=0) or 0,      # GB
        "is_public": bool(_safe_get(f, "is_public", default=True)),
    }


def _row_image(img: Any) -> dict:
    return {
        "id": _safe_get(img, "id"),
        "name": _safe_get(img, "name") or "",
        "status": _safe_get(img, "status") or "",
        "visibility": _safe_get(img, "visibility") or "",
        "size": _safe_get(img, "size") or 0,         # bytes
        "disk_format": _safe_get(img, "disk_format") or "",
    }


def _row_keypair(k: Any) -> dict:
    name = _safe_get(k, "name") or ""
    return {
        "name": name,
        "fingerprint": _safe_get(k, "fingerprint") or "",
        "type": _safe_get(k, "type") or "ssh",
        # ``id`` equals the name for a keypair — we duplicate this
        # intentionally so the picker can uniformly read ``id``.
        "id": name,
    }


def _row_security_group(sg: Any) -> dict:
    return {
        "id": _safe_get(sg, "id"),
        "name": _safe_get(sg, "name") or "",
        "description": _safe_get(sg, "description") or "",
    }


def _row_volume(v: Any) -> dict:
    return {
        "id": _safe_get(v, "id"),
        "name": _safe_get(v, "name") or "",
        "size": _safe_get(v, "size") or 0,            # GB
        "status": _safe_get(v, "status") or "",
        "volume_type": _safe_get(v, "volume_type") or "",
        "bootable": bool(_safe_get(v, "is_bootable", "bootable", default=False)),
    }


def _row_router(r: Any) -> dict:
    return {
        "id": _safe_get(r, "id"),
        "name": _safe_get(r, "name") or "",
        "status": _safe_get(r, "status") or "",
        "external_gateway_info": _safe_get(r, "external_gateway_info") or None,
    }


def _row_availability_zone(az: Any) -> dict:
    name = _safe_get(az, "name") or ""
    return {
        # AZs have no UUID — the name IS the ID.
        "id": name,
        "name": name,
        "state": _safe_get(az, "state", "zoneState") or "",
    }


# ----------------------------------------------------------------
# Cache-Refresh
# ----------------------------------------------------------------
@router.post("/refresh", status_code=status.HTTP_204_NO_CONTENT)
def refresh_cache(
    kind: str | None = Query(default=None, description="Optional: nur diese Resource-Art invalidieren"),
    current_user: User = Depends(get_current_user_keycloak),
):
    """
    Cache bust for the calling user. Triggered by a click on the
    "Refresh" button next to a picker — the user has just created a
    new resource in Horizon and wants to see it.
    """
    removed = openstack_client.invalidate_user(current_user.userId, kind)
    logger.info("Cache invalidated for user %s (kind=%s, %d entries removed)",
                current_user.userId, kind, removed)
    return None


# ----------------------------------------------------------------
# Networks
# ----------------------------------------------------------------
@router.get("/networks")
def list_networks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Lists all networks that the user can see in their project.

    ``shared`` and ``router_external`` are included so the frontend can
    render "External Network" hints.
    """
    return _listing(
        db, current_user, "networks",
        source=lambda conn: conn.network.networks(),
        row=_row_network,
    )


# ----------------------------------------------------------------
# Subnets — optionally filtered by network
# ----------------------------------------------------------------
@router.get("/subnets")
def list_subnets(
    network_id: str | None = Query(default=None, description="Filter: nur Subnets in diesem Network"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """
    With ``network_id``: subnets of that network. Without: all subnets in
    the project. Filtering happens server-side (OpenStack API), not only
    after the cache — otherwise we would have a separate cache key per
    network, and the unfiltered list cache would never help.
    """
    kwargs = {"network_id": network_id} if network_id else {}
    return _listing(
        db, current_user, "subnets",
        source=lambda conn: conn.network.subnets(**kwargs),
        row=_row_subnet,
        filters=kwargs or None,
    )


# ----------------------------------------------------------------
# Flavors
# ----------------------------------------------------------------
@router.get("/flavors")
def list_flavors(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """
    Compute flavors with the three spec fields the user really wants
    (CPU/RAM/Disk). ``is_public=False`` means private — we still emit
    it, the frontend can render a note.
    """
    return _listing(
        db, current_user, "flavors",
        source=lambda conn: conn.compute.flavors(get_extra_specs=False),
        row=_row_flavor,
    )


# ----------------------------------------------------------------
# Images
# ----------------------------------------------------------------
@router.get("/images")
def list_images(
    status_filter: str = Query(
        default="active",
        alias="status",
        description="OS Image Status (default: active). Alle Stati: 'all'",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """
    Default ``status=active`` — we do not want to show "queued" or
    "deleted" images in the picker. ``status=all`` for power users who
    really want to see everything.
    """
    kwargs = (
        {"status": status_filter}
        if status_filter and status_filter != "all"
        else {}
    )
    return _listing(
        db, current_user, "images",
        source=lambda conn: conn.image.images(**kwargs),
        row=_row_image,
        filters={"status": status_filter},
    )


# ----------------------------------------------------------------
# Keypairs
# ----------------------------------------------------------------
@router.get("/keypairs")
def list_keypairs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """
    SSH keypairs of the user. Here the identity is always the ``name``,
    never the ID — Keystone keypairs do have IDs but Terraform modules
    use the name.
    """
    return _listing(
        db, current_user, "keypairs",
        source=lambda conn: conn.compute.keypairs(),
        row=_row_keypair,
    )


# ----------------------------------------------------------------
# Security Groups
# ----------------------------------------------------------------
@router.get("/security-groups")
def list_security_groups(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    return _listing(
        db, current_user, "security_groups",
        source=lambda conn: conn.network.security_groups(),
        row=_row_security_group,
    )


# ----------------------------------------------------------------
# Floating IP Pools (External Networks)
# ----------------------------------------------------------------
@router.get("/floating-ip-pools")
def list_floating_ip_pools(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """
    There is no dedicated ``Pool`` resource in OpenStack — pools are
    networks with ``router:external = true``. Terraform modules usually
    expect the **name** of the external network.
    """
    return _listing(
        db, current_user, "floating_ip_pools",
        source=lambda conn: conn.network.networks(),
        keep=_is_external,
        row=_row_pool,
    )


# ----------------------------------------------------------------
# Volumes
# ----------------------------------------------------------------
@router.get("/volumes")
def list_volumes(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """
    Cinder volumes. The frontend filters ``status`` itself if needed
    — we emit all of them, because a user can well attach a second
    instance to an ``in-use`` volume.
    """
    return _listing(
        db, current_user, "volumes",
        source=lambda conn: conn.volume.volumes(),
        row=_row_volume,
    )


# ----------------------------------------------------------------
# Routers
# ----------------------------------------------------------------
@router.get("/routers")
def list_routers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    return _listing(
        db, current_user, "routers",
        source=lambda conn: conn.network.routers(),
        row=_row_router,
    )


# ----------------------------------------------------------------
# Availability Zones
# ----------------------------------------------------------------
@router.get("/availability-zones")
def list_availability_zones(
    service: str = Query(
        default="compute",
        description="OpenStack-Service: compute (Nova), network (Neutron), volume (Cinder)",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """
    AZs differ per service. Default: compute (Nova), because that is
    the most common use case (VM placement).
    """
    return _listing(
        db, current_user, f"availability_zones_{service}",
        source=lambda conn: _availability_zone_source(conn, service),
        # AZs without a name carry nothing the picker could select.
        keep=lambda az: bool(_safe_get(az, "name")),
        row=_row_availability_zone,
        filters={"service": service},
    )


def _availability_zone_source(conn: Any, service: str) -> Iterable[Any]:
    """Pick the AZ collection of the requested OpenStack service.

    Raised from inside the fetch callback, so the 400 travels out
    through ``_list_with_oserror``, which re-raises ``HTTPException``
    untouched rather than masking it as a 502.
    """
    if service == "compute":
        return conn.compute.availability_zones()
    if service == "network":
        return conn.network.availability_zones()
    if service == "volume":
        return conn.volume.availability_zones()
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown service '{service}' (compute|network|volume)",
    )
