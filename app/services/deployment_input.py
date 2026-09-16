"""Validation and normalisation of the deployment wizard's ``userInputVar``.

Two jobs, both defense-in-depth against a hand-crafted POST that the
wizard's own UI would never produce:

* :func:`attach_files_to_user_input` folds the parallel ``files`` field
  into ``userInputVar.terraform`` and validates base64, declared size,
  per-file and per-deployment caps, plus the app author's declared
  ``fileExtensions`` filter.
* :func:`validate_scoped_user_input` enforces that ``varScope =
  team|user`` variables arrive as a map whose keys match the
  deployment's actual team / user roster.

Plus the response-shaping helpers that strip persisted base64 payloads
out of the JSON views (:func:`parse_and_strip_user_input` and friends) —
the dedicated download route is the only path that returns raw bytes.

This module raises ``HTTPException`` on purpose. Its structured 4xx
payloads (``{"reason": "file_too_large", ...}``) are a contract the
wizard reads field by field, so translating them into domain
exceptions and back would only add a lossy hop. It is a request
validation layer, not a domain layer.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from typing import NoReturn

from fastapi import HTTPException, status


def _reject(
    reason: str,
    *,
    status_code: int = status.HTTP_422_UNPROCESSABLE_ENTITY,
    **context,
) -> NoReturn:
    """Raise the structured rejection this module's callers expect.

    Every 4xx raised here has the same body shape — a stable ``reason``
    key plus whatever context makes the error actionable (``variable``,
    ``slot``, ``limit_bytes``, …). The wizard switches on ``reason`` and
    renders the rest, so the shape is a contract, not a convenience.

    Declared ``NoReturn`` so type checkers and readers both know that a
    call to it ends the current path — the call sites read as guard
    clauses rather than as statements that might fall through.
    """
    raise HTTPException(status_code=status_code, detail={"reason": reason, **context})


# Defense-in-depth limits for inline file uploads. The UX-side warning
# is mirrored on the wizard, but a hand-crafted POST could still try
# to push GBs of payload through ``userInputVar``. We refuse before
# the row hits the DB.
#
# Per-file cap matches the existing app-image cap so users don't have
# to learn a second number; deployment-wide cap is 5× that, leaving
# headroom for (e.g.) one big assignment plus several small starter
# files. Both are enforced post-base64-decode so a malicious base64
# blob of right-shape but wrong-size still fails fast.
_MAX_FILE_BYTES_PER_FILE = 2 * 1024 * 1024
_MAX_FILE_BYTES_PER_DEPLOYMENT = 10 * 1024 * 1024


def attach_files_to_user_input(
    user_input_var: dict | None,
    files: dict | None,
    variable_definitions: list[dict] | None = None,
) -> dict:
    """Validate and merge wizard-uploaded files into ``userInputVar``.

    The wizard ships files in a parallel ``files`` field instead of
    nesting them straight into ``userInputVar.terraform`` so the
    request payload's shape is obvious to a reader and so we can
    apply size / encoding validation in one place. Result is a fresh
    dict with the files folded into ``terraform[var_name]`` — the
    worker doesn't need to know they originally came from a separate
    field.

    Validation:
      * each top-level key in ``files`` becomes one terraform variable
      * each inner-map entry is one ``DeploymentFileUpload`` record
      * ``content_b64`` decodes cleanly (RFC 4648, padding optional)
      * decoded size matches the declared ``size`` (within rounding —
        client may have set it before encoding so we accept ±1)
      * per-file cap and total deployment cap
      * if ``variable_definitions`` are provided and a file variable
        declares ``fileExtensions``, each uploaded filename's suffix
        (lowercased, after the last dot) must be in the allowed list.
        Defense-in-depth: the wizard's ``accept`` attribute already
        filters in the picker, but a hand-crafted POST could bypass it.

    Raises ``HTTPException(413)`` for size violations and
    ``HTTPException(422)`` for malformed payload — Pydantic already
    rejected the obvious cases (missing fields, wrong types) before
    we get here, so we only catch what gets past it.
    """
    base = dict(user_input_var or {})
    base.setdefault("terraform", {})
    base.setdefault("packer", {})

    if not files:
        return base

    rules = _FileVarRules.from_definitions(variable_definitions)
    budget = _ByteBudget()
    terraform_block = dict(base.get("terraform") or {})

    for var_name, slot_map in files.items():
        if var_name in terraform_block:
            # Wizard already routed something into this variable — a
            # collision means the frontend filled both the variables
            # picker AND the file uploader for the same name. That's an
            # unrecoverable contract violation; surface it clearly.
            _reject("file_var_collision", variable=var_name)
        if not isinstance(slot_map, dict) or not slot_map:
            _reject("file_var_empty", variable=var_name)

        encoded_slots = {
            slot_key: _encode_slot(
                var_name, slot_key, upload, rules.allowed_exts(var_name), budget
            )
            for slot_key, upload in slot_map.items()
        }

        # scope=team|user: HCL type is map(map(object({...}))) —
        # outer key is the team/user slot, inner key is the upload slot.
        # scope=all: HCL type is map(object({...})) — flat map.
        if rules.is_scoped(var_name):
            terraform_block[var_name] = {
                slot_key: {"uploaded": file_obj}
                for slot_key, file_obj in encoded_slots.items()
            }
        else:
            terraform_block[var_name] = encoded_slots

    base["terraform"] = terraform_block
    return base


@dataclass(frozen=True)
class _FileVarRules:
    """Per-variable upload rules distilled from the app author's
    variable declarations.

    ``variable_definitions`` is optional at the call site (a caller
    without Git access passes ``None``), so both lookups have to degrade
    to "no rule" rather than to a missing key.
    """

    # var_name → lowercased allow-list from ``@openstack:file:<scope>:<exts>``.
    # Absent means the author declared no filter, which is not the same
    # as an empty list (that would reject everything).
    _exts: dict[str, list[str]]
    # Variables whose HCL type nests one more map level because they are
    # scoped per team or per user.
    _scoped: frozenset[str]

    @classmethod
    def from_definitions(cls, variable_definitions: list[dict] | None) -> "_FileVarRules":
        exts: dict[str, list[str]] = {}
        scoped: set[str] = set()
        for vdef in variable_definitions or []:
            declared = vdef.get("fileExtensions")
            if declared:
                exts[vdef["name"]] = [e.lower() for e in declared]
            if vdef.get("varScope") in ("team", "user"):
                scoped.add(vdef["name"])
        return cls(_exts=exts, _scoped=frozenset(scoped))

    def allowed_exts(self, var_name: str) -> list[str] | None:
        return self._exts.get(var_name)

    def is_scoped(self, var_name: str) -> bool:
        return var_name in self._scoped


class _ByteBudget:
    """Running total of decoded upload bytes for one deployment.

    Mutable on purpose: the deployment-wide cap has to trip as soon as
    it is exceeded, in the middle of the per-slot loop, so that a
    request carrying a hundred oversized files is rejected after the
    first few rather than after decoding them all.
    """

    def __init__(self) -> None:
        self.total = 0

    def add(self, n: int) -> None:
        self.total += n
        if self.total > _MAX_FILE_BYTES_PER_DEPLOYMENT:
            _reject(
                "deployment_files_too_large",
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                limit_bytes=_MAX_FILE_BYTES_PER_DEPLOYMENT,
            )


def _encode_slot(
    var_name: str,
    slot_key: str,
    upload,
    allowed_exts: list[str] | None,
    budget: _ByteBudget,
) -> dict:
    """Validate one uploaded file and return its persisted record.

    ``upload`` arrives as a Pydantic model instance already (FastAPI
    deserialised the request body into ``DeploymentCreate``), so fields
    are read off attributes. Checks run in the order the caller's error
    messages assume: extension, base64, declared size, per-file cap,
    deployment cap.
    """
    _check_extension(var_name, slot_key, upload, allowed_exts)
    decoded = _decode_b64(var_name, slot_key, upload.content_b64)

    if abs(len(decoded) - upload.size) > 1:
        _reject(
            "file_size_mismatch",
            variable=var_name,
            slot=slot_key,
            declared=upload.size,
            actual=len(decoded),
        )
    if len(decoded) > _MAX_FILE_BYTES_PER_FILE:
        _reject(
            "file_too_large",
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            variable=var_name,
            slot=slot_key,
            limit_bytes=_MAX_FILE_BYTES_PER_FILE,
            actual_bytes=len(decoded),
        )
    budget.add(len(decoded))

    return {
        "name": upload.name,
        "content_b64": upload.content_b64,
        "size": upload.size,
        "content_type": upload.content_type or "application/octet-stream",
    }


def _check_extension(
    var_name: str, slot_key: str, upload, allowed_exts: list[str] | None
) -> None:
    """Enforce the app author's ``fileExtensions`` filter, when declared.

    Compares the filename suffix (after the last dot, lowercased) to the
    allow-list. A name without a dot yields the empty suffix, which is
    never in a valid allow-list and therefore rejected.
    """
    if allowed_exts is None:
        return
    name = upload.name or ""
    dot = name.rfind(".")
    suffix = name[dot + 1 :].lower() if dot >= 0 else ""
    if suffix not in allowed_exts:
        _reject(
            "file_extension_rejected",
            variable=var_name,
            slot=slot_key,
            filename=upload.name,
            allowed=allowed_exts,
        )


def _decode_b64(var_name: str, slot_key: str, content_b64: str) -> bytes:
    """Decode one upload's payload, or reject with ``file_b64_invalid``.

    ``validate=True`` rejects any non-base64 character including
    whitespace; the wizard sends compact base64 so this is fine.
    """
    try:
        return base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        _reject("file_b64_invalid", variable=var_name, slot=slot_key, error=str(e))


def validate_scoped_user_input(
    user_input_var: dict | None,
    variable_definitions: list[dict],
    teams_payload: list,
) -> None:
    """Enforce that variables marked with ``varScope = team|user``
    arrive as a map whose keys match the deployment's team / user roster.

    Reasoning: the wizard packs scoped variables as a Map
    (``{slot_key: value, ...}``) and ships them via ``userInputVar``.
    A hand-crafted POST could ship arbitrary keys; we want unknown
    Scope-Targets to fail fast and loud before they hit Terraform,
    where the error would be a confusing "module: invalid for_each
    key" deep in the worker log.

    File variables are NOT skipped here — they share the same scoped
    map shape (``{slot_key: file_obj}``) and a hand-crafted POST could
    just as easily smuggle an unknown team name into a file-scope
    variable. We validate slot identity against the same roster; the
    per-file size / base64 / extension validation stays in
    :func:`attach_files_to_user_input` because that's the layer that
    actually decodes the bytes.

    Raises ``HTTPException(422)`` with ``reason="unknown_scope_target"``,
    ``reason="scoped_var_not_map"``, or ``reason="required_slot_empty"``
    for shape/identity/completeness problems.
    """
    if not user_input_var:
        return

    roster = _SlotRoster.from_payload(teams_payload)
    for source_key in ("terraform", "packer"):
        block = user_input_var.get(source_key)
        if not isinstance(block, dict):
            continue
        for vdef in _scoped_vars(variable_definitions, source_key):
            _validate_one_scoped_var(vdef, block, roster)


class _SlotRoster:
    """The universe of slot keys a deployment's scoped variables may use.

    ``team`` scope accepts any team name verbatim; ``user`` scope accepts
    ``TeamName-Username`` composites — the mirror of ``userSlotKey`` in
    the wizard.

    The team payload carries ``userIds`` (UUID strings), not usernames,
    while the wizard labels user slots with the username. Resolving that
    here would cost a DB round-trip, so user slots are validated by
    prefix instead: a known team name plus a non-empty remainder. That
    catches typos and cross-team key smuggling, which is what this guard
    is for.
    """

    def __init__(self, team_names: set[str]) -> None:
        self.team_names = team_names
        # Longest-first so a team named ``Team-A`` matches as ``Team-A``
        # rather than as ``Team`` — a naive ``slot_key.find('-')`` would
        # misclassify every team name containing a dash as unknown.
        self._by_length = sorted(team_names, key=len, reverse=True)

    @classmethod
    def from_payload(cls, teams_payload: list) -> "_SlotRoster":
        names: set[str] = set()
        for team in teams_payload or []:
            name = getattr(team, "name", None) or (
                team.get("name") if isinstance(team, dict) else None
            )
            if name:
                names.add(name)
        return cls(names)

    def knows_team(self, slot_key: str) -> bool:
        return slot_key in self.team_names

    def knows_user_slot(self, slot_key: str) -> bool:
        """True for a ``TeamName-Username`` composite of a known team.

        A slot key equal to a bare team name has no username segment and
        is rejected — the wizard always emits one slot per member.
        """
        for team in self._by_length:
            if slot_key == team:
                return False
            if slot_key.startswith(team + "-"):
                return True
        return False


def _scoped_vars(variable_definitions: list[dict], source_key: str):
    """Yield the ``varScope = team|user`` declarations of one source."""
    for vdef in variable_definitions:
        if vdef.get("source") != source_key:
            continue
        if vdef.get("varScope") in ("team", "user"):
            yield vdef


def _validate_one_scoped_var(vdef: dict, block: dict, roster: _SlotRoster) -> None:
    """Check shape, slot identity and completeness of one scoped variable."""
    var_name = vdef["name"]
    scope = vdef["varScope"]
    required = bool(vdef.get("required"))

    value = block.get(var_name)
    if value is None:
        # An absent optional variable is left at its HCL default. An
        # absent REQUIRED one is treated as an empty map so the
        # completeness sweep below reports it as missing slots rather
        # than passing silently.
        if not required:
            return
        value = {}

    if not isinstance(value, dict):
        _reject("scoped_var_not_map", variable=var_name, scope=scope)

    for slot_key in value:
        _check_slot_identity(var_name, scope, slot_key, roster)

    # File vars are exempt from the completeness sweep: their bytes are
    # validated in :func:`attach_files_to_user_input`, which raises the
    # more specific ``file_var_empty`` / ``file_b64_invalid``. Here we
    # only checked slot identity.
    if required and vdef.get("osType") != "file":
        _check_required_slots_filled(var_name, scope, value, roster)


def _check_slot_identity(
    var_name: str, scope: str, slot_key: str, roster: _SlotRoster
) -> None:
    """Reject a slot key that names no team / member of this deployment.

    Without this an unknown key would reach Terraform and surface as a
    confusing ``module: invalid for_each key`` deep in the worker log.
    """
    if scope == "team":
        if not roster.knows_team(slot_key):
            _reject(
                "unknown_scope_target",
                variable=var_name,
                scope=scope,
                slot=slot_key,
                allowed=sorted(roster.team_names),
            )
    elif not roster.knows_user_slot(slot_key):
        _reject(
            "unknown_scope_target",
            variable=var_name,
            scope=scope,
            slot=slot_key,
            hint="expected ``TeamName-Username``",
        )


def _check_required_slots_filled(
    var_name: str, scope: str, value: dict, roster: _SlotRoster
) -> None:
    """Reject a required scoped variable with blank slots.

    Two sweeps, because they catch different failures. Every team of the
    deployment must appear (a team-scoped var with one team omitted);
    and every slot the caller DID ship must be non-empty (a half-filled
    submission). For ``user`` scope only the second sweep applies — the
    per-team member roster isn't available here without the DB
    round-trip this layer deliberately avoids.

    Without the check an empty map slips through and fails downstream
    with an opaque ``Inappropriate value for attribute``.
    """
    expected = set(roster.team_names) if scope == "team" else set()
    missing = [slot for slot in expected if _is_empty_slot_value(value.get(slot))]
    missing += [
        slot
        for slot, val in value.items()
        if _is_empty_slot_value(val) and slot not in missing
    ]
    if missing:
        _reject(
            "required_slot_empty",
            variable=var_name,
            scope=scope,
            missing_slots=sorted(missing),
        )


def _is_empty_slot_value(val) -> bool:
    """Treat None, empty string, empty list and empty dict as "not filled"."""
    if val is None:
        return True
    if isinstance(val, str) and val == "":
        return True
    return isinstance(val, (list, dict)) and len(val) == 0


def strip_file_vars_from_user_input(user_input_var: dict | None) -> dict | None:
    """Strip per-file ``content_b64`` payloads from a userInputVar dict.

    Used by the deployment detail responses so the JSON the frontend
    receives only carries metadata (name/size/content_type) — the
    decoded bytes can be many MBs each and shipping them on every
    page render is wasteful. Owners who actually want the file fetch
    it via the dedicated download endpoint.

    Heuristic-based: a variable is a file slot when its value is a
    mapping whose entries each carry a ``content_b64`` field — the
    same shape ``attach_files_to_user_input`` writes. We match on
    that key because no other user-input kind uses it.
    """
    if not isinstance(user_input_var, dict):
        return user_input_var

    out = {k: v for k, v in user_input_var.items() if k != "terraform"}
    tf_block = user_input_var.get("terraform")
    if not isinstance(tf_block, dict):
        if "terraform" in user_input_var:
            out["terraform"] = tf_block
        return out

    stripped_tf: dict = {}
    for var_name, value in tf_block.items():
        if looks_like_file_var(value):
            stripped_tf[var_name] = _file_var_metadata_only(value)
        else:
            stripped_tf[var_name] = value
    out["terraform"] = stripped_tf
    return out


def parse_and_strip_user_input(raw: str | None) -> dict | None:
    """Parse a stored ``userInputVar`` JSON string and strip file bytes.

    Wraps the ``json.loads`` → :func:`strip_file_vars_from_user_input`
    chain (with a malformed-JSON guard) shared by the list, detail, and
    create responses so all three surface the same file-stripped shape.
    Returns ``None`` for an empty or unparseable value.
    """
    if not raw:
        return None
    try:
        return strip_file_vars_from_user_input(json.loads(raw))
    except json.JSONDecodeError:
        return None


def looks_like_file_var(value) -> bool:
    """True if ``value`` matches the file-upload shape produced by
    :func:`attach_files_to_user_input`: a non-empty mapping whose
    values are objects carrying ``content_b64`` plus the metadata
    triplet. Used at response-shaping time to identify file-typed
    variables without consulting the app's variable schema, AND at
    lifecycle-dispatch time (destroy/pause/resume/redeploy) to drop
    file vars from the worker's var-set so Terraform's schema
    validation doesn't trip on a payload it doesn't need.

    Shape examples it matches (and only these):

    * ``scope=all``   → ``{"all": {name, content_b64, size, content_type}}``
    * ``scope=team``  → ``{"Team-1": {...}, "Team-2": {...}}``
    * ``scope=user``  → ``{"Team-1-luca": {...}, ...}``

    Strict signature: each slot must carry ``content_b64``. Rows
    that survived an earlier response-side-strip-then-persisted
    accident (metadata triplet only, no bytes) are NOT auto-
    detected — clean them up by hand (delete the deployment row +
    its pg-backend tfstate schema). The strictness is intentional:
    a lenient detector would silently swallow legitimate non-file
    map variables that coincidentally share the metadata key names.
    """
    if not isinstance(value, dict) or not value:
        return False
    for slot in value.values():
        if not isinstance(slot, dict):
            return False
        if "content_b64" not in slot:
            return False
    return True


def _file_var_metadata_only(value: dict) -> dict:
    """Return a copy of a file-shape variable with the ``content_b64``
    payload stripped. Metadata fields (name, size, content_type)
    survive so the UI can list "what was uploaded" without shipping
    base64 megabytes on every detail-view render.
    """
    out: dict = {}
    for slot_key, slot in value.items():
        out[slot_key] = {k: v for k, v in slot.items() if k != "content_b64"}
    return out
