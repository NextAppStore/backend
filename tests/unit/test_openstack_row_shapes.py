"""Charakterisierungstests für die Row-Projektionen der Resources-API.

Warum es diese Datei gibt: ``test_openstack_resources_api.py`` fährt nur
drei der zehn Endpoints an (flavors, images, subnets). Die Wire-Shape
der übrigen sieben — networks, keypairs, security-groups,
floating-ip-pools, volumes, routers, availability-zones — war damit
ungetestet, obwohl das Frontend feldweise darauf zugreift.

Die Projektionen sind seit dem ``_listing``-Refactor freie Funktionen
über einem SDK-Objekt, also ohne Connection, Cache oder FastAPI
testbar. Die Tests nageln genau das fest, was über die Leitung geht:
Feldnamen, Defaults für fehlende Attribute, und die beiden Stellen, an
denen dasselbe Feld unter zwei SDK-Namen auftauchen kann.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.routers.openstack_resources import (
    _is_external,
    _row_availability_zone,
    _row_flavor,
    _row_image,
    _row_keypair,
    _row_network,
    _row_pool,
    _row_router,
    _row_security_group,
    _row_subnet,
    _row_volume,
)


class _Sdk(SimpleNamespace):
    """Stand-in für ein SDK-Objekt.

    ``_safe_get`` liest per ``getattr`` und behandelt ``None`` wie
    "nicht gesetzt" — ``SimpleNamespace`` bildet beides ab, ohne die
    Auto-Attribute eines MagicMock, die jedes fehlende Feld
    versehentlich befüllen würden.
    """


# ----------------------------------------------------------------
# Vollständig befüllte Objekte → alle Felder wandern durch
# ----------------------------------------------------------------
def test_row_network_full():
    assert _row_network(_Sdk(
        id="net-1", name="private", description="internes Netz",
        is_shared=True, is_router_external=False, status="ACTIVE",
    )) == {
        "id": "net-1",
        "name": "private",
        "description": "internes Netz",
        "shared": True,
        "external": False,
        "status": "ACTIVE",
    }


def test_row_subnet_full():
    assert _row_subnet(_Sdk(
        id="sub-1", name="sub", cidr="10.0.0.0/24",
        ip_version=4, network_id="net-1", gateway_ip="10.0.0.1",
    )) == {
        "id": "sub-1",
        "name": "sub",
        "cidr": "10.0.0.0/24",
        "ip_version": 4,
        "network_id": "net-1",
        "gateway_ip": "10.0.0.1",
    }


def test_row_flavor_full():
    assert _row_flavor(_Sdk(
        id="f-1", name="m1.small", vcpus=2, ram=4096, disk=40, is_public=False,
    )) == {
        "id": "f-1",
        "name": "m1.small",
        "vcpus": 2,
        "ram": 4096,
        "disk": 40,
        "is_public": False,
    }


def test_row_image_full():
    assert _row_image(_Sdk(
        id="img-1", name="ubuntu-24.04", status="active",
        visibility="public", size=1234, disk_format="qcow2",
    )) == {
        "id": "img-1",
        "name": "ubuntu-24.04",
        "status": "active",
        "visibility": "public",
        "size": 1234,
        "disk_format": "qcow2",
    }


def test_row_volume_full():
    assert _row_volume(_Sdk(
        id="vol-1", name="data", size=100,
        status="in-use", volume_type="ssd", is_bootable=True,
    )) == {
        "id": "vol-1",
        "name": "data",
        "size": 100,
        "status": "in-use",
        "volume_type": "ssd",
        "bootable": True,
    }


def test_row_router_full():
    gateway = {"network_id": "ext-1"}
    assert _row_router(_Sdk(
        id="r-1", name="edge", status="ACTIVE", external_gateway_info=gateway,
    )) == {
        "id": "r-1",
        "name": "edge",
        "status": "ACTIVE",
        "external_gateway_info": gateway,
    }


def test_row_security_group_full():
    assert _row_security_group(_Sdk(
        id="sg-1", name="web", description="80/443",
    )) == {"id": "sg-1", "name": "web", "description": "80/443"}


def test_row_pool_full():
    assert _row_pool(_Sdk(
        id="ext-1", name="public", description="Floating IPs",
    )) == {"id": "ext-1", "name": "public", "description": "Floating IPs"}


# ----------------------------------------------------------------
# Leere / fehlende Attribute → dokumentierte Defaults
# ----------------------------------------------------------------
@pytest.mark.parametrize(
    "row_fn,expected",
    [
        (_row_network, {
            "id": None, "name": "", "description": "",
            "shared": False, "external": False, "status": "",
        }),
        (_row_subnet, {
            "id": None, "name": "", "cidr": "",
            "ip_version": 4, "network_id": None, "gateway_ip": "",
        }),
        (_row_flavor, {
            "id": None, "name": "", "vcpus": 0,
            "ram": 0, "disk": 0, "is_public": True,
        }),
        (_row_image, {
            "id": None, "name": "", "status": "",
            "visibility": "", "size": 0, "disk_format": "",
        }),
        (_row_volume, {
            "id": None, "name": "", "size": 0,
            "status": "", "volume_type": "", "bootable": False,
        }),
        (_row_router, {
            "id": None, "name": "", "status": "",
            "external_gateway_info": None,
        }),
        (_row_security_group, {"id": None, "name": "", "description": ""}),
        (_row_pool, {"id": None, "name": "", "description": ""}),
    ],
)
def test_row_defaults_for_empty_sdk_object(row_fn, expected):
    """Ein Objekt ohne die erwarteten Attribute darf die Response nicht
    sprengen — das Frontend liest die Felder unbedingt."""
    assert row_fn(_Sdk()) == expected


# ----------------------------------------------------------------
# Sonderfälle, die die Projektion bewusst anders behandelt
# ----------------------------------------------------------------
def test_row_keypair_mirrors_name_into_id():
    """Nova-Keypairs werden über den Namen adressiert. Die Projektion
    spiegelt ihn nach ``id``, damit der Picker überall ``id`` lesen kann."""
    row = _row_keypair(_Sdk(name="adrian-key", fingerprint="aa:bb", type="ssh"))
    assert row == {
        "name": "adrian-key",
        "fingerprint": "aa:bb",
        "type": "ssh",
        "id": "adrian-key",
    }
    assert row["id"] == row["name"]


def test_row_keypair_defaults_type_to_ssh():
    assert _row_keypair(_Sdk(name="k"))["type"] == "ssh"


def test_row_availability_zone_uses_name_as_id():
    """AZs haben keine UUID — der Name IST die ID."""
    assert _row_availability_zone(_Sdk(name="nova", state="available")) == {
        "id": "nova",
        "name": "nova",
        "state": "available",
    }


def test_row_availability_zone_accepts_legacy_zone_state():
    """Ältere Nova-Versionen liefern ``zoneState`` statt ``state``."""
    assert _row_availability_zone(_Sdk(name="nova", zoneState="available"))["state"] == (
        "available"
    )


@pytest.mark.parametrize(
    "attrs,expected",
    [
        ({"is_router_external": True}, True),
        ({"is_router_external": False}, False),
        # Neutron liefert das Flag je nach SDK-Version unter dem
        # Raw-API-Namen ``router:external``.
        ({"router:external": True}, True),
        ({}, False),
    ],
)
def test_is_external_reads_both_sdk_spellings(attrs, expected):
    assert _is_external(_Sdk(**attrs)) is expected
