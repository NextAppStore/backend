"""Parsing of HCL ``variable "..." { ... }`` blocks into the dicts the
wizard consumes.

Sits one layer above :mod:`app.services.hcl.markers`: it walks the
declaration blocks of a ``variables.tf`` / ``variables.pkr.hcl`` file,
coerces HCL default literals into Python values, and runs the marker
grammar over each description. Marker failures are attached to the
variable as ``markerError`` rather than raised, so one bad marker never
breaks the whole wizard.

Pure functions over file paths and strings — no FastAPI, no DB.
"""

import json
import re
from typing import Any

from app.services.hcl.markers import MarkerError, apply_defaults, parse_marker


def _line_number_at(content: str, char_index: int) -> int:
    """1-based line index for a char position. Used to point
    MarkerError messages at the line in ``variables.tf`` instead of only
    naming the variable."""
    return content.count("\n", 0, char_index) + 1


def validate_file_var_shape(var_name: str, var_type: str, scope: str) -> None:
    """Verify a ``@openstack:file:<scope>``-marked variable has the
    HCL type the wizard contract expects.

    The contract — documented in the deploy/file-uploads design — is:

    * ``scope = all``  → ``map(object({...}))``
    * ``scope = team`` → ``map(map(object({...})))``
    * ``scope = user`` → ``map(map(object({...})))``

    The outer map keys content by upload-key (today always
    ``"uploaded"``, reserved for future multi-file-per-slot). For
    ``team``/``user`` the next layer keys by team name resp.
    ``Team-User``-pair so the worker can route per-recipient bytes.

    We don't try to parse HCL — we just check the prefix shape with
    cheap string ops. False positives are unlikely (no real-world HCL
    type accidentally starts with ``map(map(`` unless it is one) and
    a strict full parse would be a big dependency for one check.
    """
    type_normalised = (var_type or "").strip().lower().replace(" ", "")
    if scope == "all" and not type_normalised.startswith("map(object("):
        raise MarkerError(
            var_name,
            f"marker ``@openstack:file:all`` erwartet HCL-Type "
            f"``map(object({{name=string, content_b64=string, "
            f"size=number, content_type=string}}))`` — angegeben: '{var_type}'",
            code="MARKER_FILE_TYPE_SHAPE",
        )
    if scope in ("team", "user") and not type_normalised.startswith("map(map(object("):
        raise MarkerError(
            var_name,
            f"marker ``@openstack:file:{scope}`` erwartet HCL-Type "
            f"``map(map(object({{name=string, content_b64=string, "
            f"size=number, content_type=string}})))`` — angegeben: '{var_type}'",
            code="MARKER_FILE_TYPE_SHAPE",
        )


def validate_scoped_var_shape(var_name: str, var_type: str, scope: str) -> None:
    """Verify a non-file variable marked with ``var_scope = team|user``
    has a map-typed HCL declaration.

    Reasoning: bei ``team``/``user``-Scope schickt der Wizard eine Map
    (slot_key → value) an Terraform/Packer. Wenn der HCL-Type ein
    Skalar ist (``string``, ``number``, ...), würde Terraform die Map
    beim Apply ablehnen. Wir fangen das hier ab, damit der App-Autor
    den Fehler bei ``GET /apps/{id}/variables`` sieht und nicht erst
    beim ersten Deploy.

    Bei ``scope = all`` (oder fehlendem Scope) gilt das nicht — dann
    rendert der Wizard genau EIN Eingabefeld, das wie heute direkt
    als Skalar oder Liste an Terraform durchgereicht wird.
    """
    if scope not in ("team", "user"):
        return
    type_normalised = (var_type or "").strip().lower().replace(" ", "")
    if not type_normalised.startswith("map(") and type_normalised not in ("map",):
        raise MarkerError(
            var_name,
            f"marker deklariert ``var_scope = {scope}``, aber HCL-Type "
            f"ist '{var_type}'. Pro Scope-Eintrag liefert der Wizard "
            f"eine Map (slot_key → value), also muss der HCL-Type "
            f"``map(...)`` sein — z.B. ``map(string)`` oder "
            f"``map(list(string))``.",
            code="MARKER_SCOPED_REQUIRES_MAP",
        )


def _coerce_hcl_default(raw_default: str, var_type: str) -> tuple[Any, bool]:
    """Coerce an HCL default literal into its Python equivalent so the
    frontend sees ``default = 2`` as ``2`` (number) rather than ``"2"``
    (string). Returns ``(value, required)`` — an HCL ``null`` default
    yields ``None`` AND ``required = True`` (Terraform treats null as "no
    default").

    Robust against minor whitespace and trailing commas; any parse error
    falls back to the raw string.
    """
    if raw_default is None:
        return (None, True)

    stripped = raw_default.strip()
    # An empty slot and a literal HCL ``null`` both mean "no default",
    # which Terraform treats as "required".
    if stripped == "" or stripped.lower() == "null":
        return (None, True)

    type_lower = (var_type or "").strip().lower()

    # Bool before anything else — otherwise ``"true"`` declared as a
    # string default would be swallowed by the string path.
    if type_lower == "bool":
        coerced = _as_bool(stripped)
        if coerced is not None:
            return (coerced, False)

    if type_lower == "number":
        return (_as_number(stripped), False)

    if _is_collection_type(type_lower) or stripped.startswith(("[", "{")):
        return (_as_json_literal(stripped), False)

    return (_unquote(stripped), False)


def _as_bool(stripped: str) -> bool | None:
    """``true``/``false`` case-insensitively; ``None`` for anything else,
    which lets the caller fall through to the remaining type paths."""
    lowered = stripped.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return None


def _as_number(stripped: str) -> Any:
    """Int, or float when the literal carries a decimal point or an
    exponent. An unparseable literal passes through as the raw string —
    the wizard renders it verbatim rather than dropping the author's value.
    """
    try:
        if "." in stripped or "e" in stripped.lower():
            return float(stripped)
        return int(stripped)
    except ValueError:
        return stripped


def _is_collection_type(type_lower: str) -> bool:
    """True for the HCL type constructors whose literals are JSON-shaped."""
    return (
        type_lower.startswith(("list(", "set(", "tuple(", "map("))
        or type_lower in ("list", "set", "map", "object")
    )


def _as_json_literal(stripped: str) -> Any:
    """Parse a list/map literal.

    python-hcl2 would be the clean option, but it isn't a backend
    dependency and adding a lazy import would make the import path
    fragile. ``json.loads`` covers it instead: HCL list/map literals over
    string, number and bool values are a true subset of JSON.

    Two things JSON rejects that HCL allows: capitalised ``True``/
    ``False``/``Null``, which the retry lowercases. Anything still
    unparseable (unquoted identifiers like ``[NAT]``, interpolations)
    passes through as the raw string.
    """
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        pass
    try:
        normalised = re.sub(
            r"\b(true|false|null)\b",
            lambda m: m.group(0).lower(),
            stripped,
            flags=re.IGNORECASE,
        )
        return json.loads(normalised)
    except (ValueError, TypeError):
        return stripped


def _unquote(stripped: str) -> str:
    """Strip one layer of matching outer quotes, if the caller left them on."""
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ('"', "'"):
        return stripped[1:-1]
    return stripped


def parse_one_variable(
    *,
    var_name: str,
    var_block: str,
    var_block_offset: int,
    file_content: str,
    file_label: str,
    source: str,
) -> dict[str, Any]:
    """
    Process a single ``variable "..." { ... }`` block.

    Always returns the variable dict; marker errors are NOT raised but
    attached to the variable in the ``markerError`` field, so the frontend
    can render the variable as free text and show the error inline instead
    of breaking the whole wizard on one bad marker.
    """
    # Extract type
    type_match = re.search(r'type\s*=\s*([^\n]+)', var_block)
    var_type = type_match.group(1).strip() if type_match else "string"

    # Extract description
    desc_match = re.search(r'description\s*=\s*"([^"]*)"', var_block)
    description = desc_match.group(1) if desc_match else ""

    # Extract default value
    default_match = re.search(r'default\s*=\s*([^\n]+)', var_block)
    default_raw = default_match.group(1).strip() if default_match else None

    # Coerce HCL defaults into their Python equivalents (number→int/float,
    # bool→bool, list/map→lists/dicts). ``null`` resets ``required`` to
    # True. On parse error the value falls back to the raw string.
    try:
        default_value, required = _coerce_hcl_default(default_raw, var_type)
    except Exception:
        # Defensive: no HCL edge case should crash the wizard. Worst
        # case, keep the raw string with required=False if a default was
        # present.
        default_value = default_raw
        required = default_raw is None

    var_info: dict[str, Any] = {
        "name": var_name,
        "type": var_type,
        "description": description,
        "default": default_value,
        "required": required,
        "source": source,
    }

    # Evaluate @openstack markers. Per-variable try/except: a typo in ONE
    # variable description must not block the whole wizard; the error
    # travels in the payload alongside the variable.
    try:
        (
            os_type,
            raw_mode,
            raw_multi,
            file_scope,
            var_scope,
            file_exts,
        ) = parse_marker(var_name, var_type, description, source=source)
        # File variables have a hard contract with cloud-init: the wizard
        # must know whether to render a single slot (scope=all), a map
        # over teams, or a map over users. The HCL type nesting must match
        # the scope or Terraform rejects the decode at apply — we catch it
        # here and give the author a clear error.
        if os_type == "file":
            validate_file_var_shape(var_name, var_type, file_scope or "all")
        elif var_scope:
            validate_scoped_var_shape(var_name, var_type, var_scope)
    except MarkerError as exc:
        line = _line_number_at(file_content, var_block_offset)
        var_info["markerError"] = {
            "variable": exc.var_name,
            "message": exc.message,
            "location": f"{file_label}:{line}",
            # ``code`` is the stable key for future i18n / frontend logic.
            "code": exc.code,
        }
        return var_info

    if os_type:
        if os_type == "file":
            # File variables are neither mode- nor multi-driven; the
            # wizard renders a FileDropZone, not the resource picker.
            # ``osMode`` and ``osMulti`` are deliberately left unset so
            # the frontend reads the absence as "not applicable" rather
            # than inventing a default.
            var_info["osType"] = os_type
            var_info["osScope"] = file_scope or "all"
            if file_exts:
                var_info["fileExtensions"] = file_exts
        else:
            mode, multi = apply_defaults(os_type, raw_mode, raw_multi, var_type)
            var_info["osType"] = os_type
            var_info["osMode"] = mode
            var_info["osMulti"] = multi

    # ``varScope`` is orthogonal to the resource type — even a free-text
    # variable (no ``osType``) can be scoped. For file variables we mirror
    # ``osScope`` into ``varScope`` so the frontend reads one source.
    if var_scope:
        var_info["varScope"] = var_scope
    elif os_type == "file":
        var_info["varScope"] = file_scope or "all"

    return var_info


def iter_variable_blocks(content: str):
    """Yield ``(var_name, var_block, block_offset)`` for each HCL
    ``variable "name" { ... }`` block, brace-balanced.

    A naive ``variable\\s+"([^"]+)"\\s*\\{([^}]+)\\}`` regex stops the
    block at the FIRST ``}`` and truncates any variable whose type or
    default literal contains braces — e.g. ``type = object({...})``,
    ``map(...)`` or ``default = {}``. Instead we match only the block
    HEAD and then walk the string counting ``{``/``}`` until depth
    returns to zero.

    ``var_block`` is the content BETWEEN the outer braces (exclusive);
    ``block_offset`` is the start index of the whole ``variable``
    declaration (used for line-number hints).
    """
    head_pattern = r'variable\s+"([^"]+)"\s*\{'
    for head in re.finditer(head_pattern, content):
        var_name = head.group(1)
        block_offset = head.start()
        open_brace = head.end() - 1  # index of the ``{`` matched above
        depth = 0
        end_index = -1
        for i in range(open_brace, len(content)):
            ch = content[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_index = i
                    break
        if end_index == -1:
            # Unbalanced braces — skip this malformed block rather than
            # emitting a truncated one.
            continue
        var_block = content[open_brace + 1:end_index]
        yield var_name, var_block, block_offset


def parse_terraform_variables(file_path: str) -> list[dict[str, Any]]:
    """Parse Terraform `variables.tf` file. Per-variable marker errors
    travel in the ``markerError`` field (not raised) — see
    ``parse_one_variable``."""
    with open(file_path) as f:
        content = f.read()

    variables = []
    for var_name, var_block, block_offset in iter_variable_blocks(content):
        # Filter: drop ``users`` and ``image_name``
        if var_name == "users" or var_name == "image_name":
            continue
        # Multi-image apps declare ``image_name_<key>`` per template and
        # mark those declarations with ``@platform:internal`` in the
        # description. The worker fills these from the discovered Packer
        # templates; the wizard must not surface them as user-editable
        # variables. Same rationale as the ``image_name``/``users``
        # filter above — these are platform-injected, not user input.
        desc_match = re.search(r'description\s*=\s*"([^"]*)"', var_block)
        description = desc_match.group(1) if desc_match else ""
        if "@platform:internal" in description:
            continue
        variables.append(parse_one_variable(
            var_name=var_name,
            var_block=var_block,
            var_block_offset=block_offset,
            file_content=content,
            file_label="terraform/variables.tf",
            source="terraform",
        ))

    return variables


def parse_packer_variables(file_path: str, template_key: str = "default") -> list[dict[str, Any]]:
    """Parse Packer `variables.pkr.hcl` file. Per-variable marker errors
    travel in the ``markerError`` field; see ``parse_one_variable``.

    ``template_key`` is recorded on each variable so the wizard can
    group Packer variables per template (and avoid name collisions
    across templates in multi-image apps). For the single-template
    layout the caller passes ``"default"``.
    """
    with open(file_path) as f:
        content = f.read()

    variables = []
    for var_name, var_block, block_offset in iter_variable_blocks(content):
        # Filter: image_name rauslassen
        if var_name == "image_name":
            continue
        var_info = parse_one_variable(
            var_name=var_name,
            var_block=var_block,
            var_block_offset=block_offset,
            file_content=content,
            file_label=f"packer/{template_key}/variables.pkr.hcl"
            if template_key != "default"
            else "packer/variables.pkr.hcl",
            source="packer",
        )
        var_info["template_key"] = template_key
        variables.append(var_info)

    return variables
