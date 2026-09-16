"""``@openstack`` marker grammar for HCL variable descriptions.

Pure parsing — no FastAPI, no DB, no filesystem. Extracted from
``app/routers/apps.py`` so the grammar can be read, tested and changed
without wading through HTTP handlers.

Public surface: :class:`MarkerError`, :func:`parse_marker`,
:func:`apply_defaults` and the ``*_TYPES`` / ``*_SCOPES`` vocabularies.
Everything prefixed with ``_`` is an implementation detail of this
module.
"""

import difflib
import re

# ----------------------------------------------------------------
# OPENSTACK MARKER PARSING (HCL VARIABLES)
# ----------------------------------------------------------------
# Apps declare value-help for OpenStack resources exclusively via an
# explicit marker in the variable's ``description``. No heuristics, no
# name inference. A variable without a marker renders as free text.
#
# Grammar (positional, with defaults):
#
#     @openstack:<type>[:<mode>][:<multi>][:<var_scope>]
#
#   <type>   — one of the resource kinds in ``OS_TYPES``, OR EMPTY. An
#              empty type slot is allowed when the marker only sets a
#              ``var_scope`` (e.g. ``@openstack:::user`` scopes an
#              otherwise free string variable per-user).
#   <mode>   — 'id' | 'name' (default 'name'; see ``NAME_ONLY_TYPES``).
#   <multi>  — 'multi' | 'list' | 'single' ('list' is a synonym for
#              'multi'). Default derives from the HCL type:
#              ``list``/``set``/``tuple`` → multi, else single.
#              ``map(...)``/``object(...)`` count as single.
#   <var_scope> — 'all' | 'team' | 'user' (default 'all'). Controls
#              whether the wizard renders one input (``all``), one per
#              team, or one per user. ``team``/``user`` require a
#              ``map(...)`` HCL type. Packer variables allow only ``all``.
#
# Examples:
#     @openstack:network                    → network, name-mode, multi from HCL
#     @openstack:network:id                 → network, id-mode
#     @openstack:security_group:name:multi  → SG, name-mode, multi
#     @openstack:flavor::multi              → empty mode slot ⇒ default 'name'
#     @openstack:flavor:id:single:team      → one flavor ID per team; map(string)
#     @openstack:::user                     → free-text, per-user scoped; map(...)
#
# The marker may appear anywhere in the description but must terminate at
# a word boundary. With multiple markers, the first with a KNOWN type
# wins; markers with unknown types are skipped. If no marker has a known
# type, that's an error (reported with a "did you mean …?" hint).
#
# Error handling: malformed or invalid markers raise ``MarkerError``,
# caught per-variable and attached to the payload as ``markerError`` so
# that variable renders as free text with an inline hint while the rest
# stay usable.
# ----------------------------------------------------------------

# Supported OpenStack resource types. Must stay consistent with:
#  - backend/app/routers/openstack_resources.py (list endpoints)
#  - frontend/src/types/index.ts (`AppVariableOsType`)
#  - frontend/src/components/OpenStackResourcePicker.vue (render)
OS_TYPES: set[str] = {
    "network",
    "subnet",
    "flavor",
    "image",
    "keypair",
    "security_group",
    "floating_ip_pool",
    "volume",
    "router",
    "availability_zone",
    # ``file`` is a special pseudo-resource: it doesn't pick from a
    # remote OpenStack API, it tells the wizard to render a file-upload
    # widget and route the bytes into ``userInputVar.terraform`` so the
    # template can drop them onto the VM via cloud-init ``write_files``.
    # The mode slot carries the scope (``all``/``team``/``user``); the
    # multi slot carries the mandatory extension filter (e.g. ``pdf`` or
    # ``pdf|docx``) — a file marker without a filter is rejected.
    "file",
}

# Allowed scope tokens for ``@openstack:file:<scope>``. Reuses the
# mode slot of the marker grammar — keeps the regex shape unchanged
# while teaching the parser to interpret the slot per-type.
FILE_SCOPES: set[str] = {"all", "team", "user"}

# Mandatory extension filter in the fourth marker slot for file
# variables. Only letters/digits and ``|`` as separator; matched
# case-insensitively, lowercased internally. Examples: ``pdf``,
# ``pdf|docx|txt``. Empty is not allowed.
_FILE_EXTENSIONS_RE = re.compile(r"^[a-z0-9]+(?:\|[a-z0-9]+)*$")

# Allowed values for the general ``var_scope`` slot (fourth slot on
# non-file markers). ``all`` is the default — variables without a marker
# and markers without a 4th slot resolve to ``all``.
VAR_SCOPES: set[str] = {"all", "team", "user"}

# Resource kinds that effectively have no UUID in OpenStack or are
# addressed by name throughout — e.g. keypairs (Nova uses names only),
# availability zones (no UUID at all), floating-IP pools (external
# networks, referenced by name in modules).
#
# The name-only default applies ONLY when the author omits the mode.
# ``@openstack:keypair`` → mode='name'. ``@openstack:keypair:id`` is
# respected but practically pointless (yields an empty ID list).
NAME_ONLY_TYPES: set[str] = {"keypair", "availability_zone", "floating_ip_pool"}

# Marker regex. Matches the whole token at word boundaries so prose
# examples like ``"see @openstack:network in the docs"`` are recognised
# but ``"@openstackbar"`` is not. Slot content may not contain
# whitespace. Five or more colons = malformed (see
# ``_TOO_MANY_SEGMENTS_RE``).
#
# Right boundary: any non-identifier char — whitespace, line end, common
# punctuation ``. , ; : ! ? ) ] " '``. Left boundary: start or the same.
#
# Slot content:
#  * Slot 1 (type): ``[A-Za-z][A-Za-z0-9_]*`` or EMPTY. An empty type
#    slot means "only set var_scope, don't force a resource picker".
#  * Slots 2/3: ``[A-Za-z]*``.
#  * Slot 4 (var_scope for non-file, file-extensions filter for file):
#    ``[A-Za-z0-9|]*``. The ``|`` is only needed for the file-filter
#    case (e.g. ``pdf|docx``); the parser splits the semantics.
_MARKER_RE = re.compile(
    r"""
    (?:^|(?<=[\s.,;:!?()\[\]"']))   # Left boundary: start or whitespace/punctuation
    @openstack
    :([A-Za-z][A-Za-z0-9_]*)?       # 1: type (may be empty → scope-only marker)
    (?::([A-Za-z]*))?               # 2: mode slot (may be empty)
    (?::([A-Za-z]*))?               # 3: multi slot (may be empty)
    (?::([A-Za-z0-9|]*))?           # 4: var_scope / file-extensions (may be empty)
    (?=$|[\s.,;:!?)\]"'])           # Right boundary
    """,
    # Marker prefix is accepted case-insensitively (``@OpenStack:flavor``
    # == ``@openstack:flavor``); ``parse_marker`` lowercases slot content.
    re.VERBOSE | re.IGNORECASE,
)

# Detects a ``<whitespace>:<token>`` continuation right after a match
# (e.g. ``@openstack:flavor :id``). ``_MARKER_RE`` stops at the
# whitespace, so we raise an explicit error to surface the typo.
_MARKER_WHITESPACE_CONT_RE = re.compile(r"\s+:\s*[A-Za-z]")

# A comma as a slot separator is a common typo — see the call site.
# Match form: ``<tail starts with>,<token-char>``.
_MARKER_COMMA_CONT_RE = re.compile(r",\s*[A-Za-z0-9|]")

# Quick check: does the marker have too many segments?
# ``@openstack:network:id:multi:team:extra`` → fail. Every 5+ segment
# must be non-empty, otherwise a trailing-colon 4-slot form would be
# wrongly caught.
_TOO_MANY_SEGMENTS_RE = re.compile(
    r"@openstack(?::[A-Za-z0-9_|]+){5,}",
    re.IGNORECASE,
)


# Detects a marker-attempted-but-malformed input: fires when the
# description contains ``@openstack:`` but the strict regex matches
# nothing (dash/slash/equals separators, whitespace, empty type, etc.).
# Matches ``@openstack`` followed by ``:`` or whitespace+``:``.
_BAD_PREFIX_RE = re.compile(
    r"@openstack\s*:",
    re.IGNORECASE,
)


class MarkerError(ValueError):
    """Raised when an ``@openstack`` marker is syntactically or
    semantically invalid. Translated to HTTP 400 in the endpoint so the
    app author sees the error on the first ``GET /apps/{id}/variables``
    instead of the variable silently rendering as free text.

    ``code`` is a stable machine-readable key (e.g. ``MARKER_WHITESPACE``);
    ``message`` is human-readable German for now.
    """

    def __init__(self, var_name: str, message: str, code: str = "MARKER_INVALID"):
        super().__init__(f"Variable '{var_name}': {message}")
        self.var_name = var_name
        self.message = message
        self.code = code


def _parse_var_scope(var_name: str, slot: str | None) -> str | None:
    """Validate and normalize the ``var_scope`` slot of a marker.

    Returns ``None`` for an empty slot; raises ``MarkerError`` for an
    unknown token (with a closest-match hint when one exists).
    """
    if slot is None or slot == "":
        return None
    rs = slot.lower()
    if rs in VAR_SCOPES:
        return rs
    suggestion = _closest_match(rs, VAR_SCOPES)
    hint = f"; meintest du '{suggestion}'?" if suggestion else ""
    raise MarkerError(
        var_name,
        f"ungültiger var_scope '{slot}'{hint} — erwartet "
        f"{sorted(VAR_SCOPES)}",
        code="MARKER_INVALID_VAR_SCOPE",
    )


def _forbid_packer_team_user_scope(var_name: str, source: str, var_scope: str | None) -> None:
    """Reject ``team``/``user`` scopes on packer variables.

    Packer builds ONE image shared by all later VMs/teams/users, so a
    per-team/per-user value would have no effect. Called from both the
    scope-only and resource marker paths.
    """
    if source == "packer" and var_scope in ("team", "user"):
        raise MarkerError(
            var_name,
            f"packer-Variablen unterstützen nur ``var_scope = all``; "
            f"angegeben: '{var_scope}'. Begründung: Packer baut EIN "
            f"Image, das von allen späteren VMs/Teams/Usern geteilt "
            f"wird — ein Per-Team-Wert hätte keine Wirkung.",
            code="MARKER_PACKER_SCOPE_FORBIDDEN",
        )


def _reject_malformed_markers(var_name: str, description: str) -> None:
    """Raise ``MarkerError`` for the malformed-marker shapes a plain
    regex match would silently swallow.

    Covers: too many segments, whitespace between segments, and a comma
    used as a slot separator (the only valid separator is ``|``).
    """
    # Six+ segments (i.e. five+ colons after ``@openstack:``) are never
    # legitimate. Check this first, BEFORE the main regex (which stops
    # after four slots) even notices.
    if _TOO_MANY_SEGMENTS_RE.search(description):
        raise MarkerError(
            var_name,
            "marker hat zu viele Segmente — erlaubt: "
            "@openstack:<type>[:<mode>][:<multi>][:<var_scope>]",
            code="MARKER_TOO_MANY_SEGMENTS",
        )

    matches = list(_MARKER_RE.finditer(description))
    if not matches:
        # Strict hard-fail path: someone typed ``@openstack:`` but our
        # grammar doesn't match — e.g. whitespace, a dash, ``=``, or a
        # slash. Fail loudly rather than render the variable as free-text.
        if _BAD_PREFIX_RE.search(description):
            raise MarkerError(
                var_name,
                "marker konnte nicht geparst werden — erlaubt ist nur "
                "``@openstack:<type>[:<mode>][:<multi>][:<var_scope>]`` mit "
                "Doppelpunkten als Trenner und ohne Whitespace zwischen "
                "den Segmenten",
                code="MARKER_UNPARSEABLE",
            )
        return

    # Whitespace between marker segments silently truncates:
    # ``_MARKER_RE`` stops at the first whitespace, so
    # ``@openstack:flavor :id`` parses only as ``@openstack:flavor``.
    # For each match, check for a ``<whitespace>:<token>`` continuation
    # and raise a clear error instead of swallowing the typo.
    for m in matches:
        tail = description[m.end():]
        if _MARKER_WHITESPACE_CONT_RE.match(tail):
            raise MarkerError(
                var_name,
                "marker enthält Whitespace zwischen den Segmenten — "
                "schreibe ihn ohne Leerzeichen (z.B. "
                "``@openstack:flavor:id:multi`` statt "
                "``@openstack:flavor :id :multi``)",
                code="MARKER_WHITESPACE",
            )

    # A comma as a slot separator is a common typo — the only allowed
    # separator is ``|`` (e.g. ``@openstack:file:all:pdf|docx``).
    # ``_MARKER_RE`` matches only up to the comma, so we detect
    # ``<match>,<token>`` explicitly and raise a clear error.
    for m in matches:
        tail = description[m.end():]
        if _MARKER_COMMA_CONT_RE.match(tail):
            raise MarkerError(
                var_name,
                "ungültiger Endungsfilter mit Komma — marker-Slots werden "
                "mit ``|`` getrennt, nicht mit Komma (z.B. "
                "``@openstack:file:all:pdf|docx`` statt "
                "``@openstack:file:all:pdf,docx``)",
                code="MARKER_FILE_INVALID_EXTENSIONS",
            )


def _select_marker(var_name: str, description: str):
    """Pick the effective marker match from the description.

    The first marker with a KNOWN type OR an empty type slot (= a
    scope-only marker) wins; markers with an unknown, non-empty type are
    skipped (tolerated). Returns the chosen
    ``(match, raw_type, raw_mode, raw_multi, raw_scope)`` tuple, or
    ``None`` when the description carries no marker at all. Raises
    ``MarkerError`` when every marker had an unknown type.
    """
    matches = list(_MARKER_RE.finditer(description))
    if not matches:
        return None

    first_unknown: tuple[str, str] | None = None  # (raw_type, suggestion)
    for m in matches:
        raw_type = (m.group(1) or "")
        os_type_candidate = raw_type.lower()
        if raw_type == "" or os_type_candidate in OS_TYPES:
            return (m, raw_type, m.group(2), m.group(3), m.group(4))
        if first_unknown is None:
            first_unknown = (raw_type, _closest_match(os_type_candidate, OS_TYPES) or "")

    # There were markers, but all with unknown types. Hard-fail with a
    # hint pointing at the first — that is very likely the author's typo.
    raw_type, suggestion = first_unknown  # type: ignore[misc]
    hint = f"; meintest du '{suggestion}'?" if suggestion else ""
    raise MarkerError(
        var_name,
        f"unbekannter resource-type '{raw_type}'{hint} — "
        f"erwartet: {sorted(OS_TYPES)}",
        code="MARKER_UNKNOWN_OS_TYPE",
    )


def _parse_scope_only_marker(
    var_name: str, source: str, raw_mode: str | None, raw_multi: str | None, raw_scope: str | None
):
    """Parse a scope-only marker (empty type slot, e.g. ``@openstack:::team``).

    Such a marker has no type/mode/multi — only scope meaning. When the
    author uses a short form (``@openstack::team`` with two slots instead
    of four), ``team`` lands in the mode slot rather than the fourth. We
    take the first non-empty slot of mode/multi/scope and accept it as
    long as it is a var_scope token — this makes marker spelling robust
    against the number of colons. Several occupied slots at once remain
    an error (ambiguous).
    """
    candidates = [s for s in (raw_mode, raw_multi, raw_scope) if s not in (None, "")]
    if len(candidates) > 1:
        raise MarkerError(
            var_name,
            "leerer type-slot ist nur in Kombination mit ``var_scope`` "
            "erlaubt (z.B. ``@openstack:::team``); mehrere belegte "
            "Slots sind hier nicht zulässig",
            code="MARKER_EMPTY_TYPE_AMBIGUOUS",
        )
    var_scope = _parse_var_scope(var_name, candidates[0] if candidates else None)
    if var_scope is None:
        raise MarkerError(
            var_name,
            "leerer Marker — entweder einen resource-type angeben "
            "(z.B. ``@openstack:flavor``) oder einen var_scope "
            "(z.B. ``@openstack:::team``)",
            code="MARKER_EMPTY",
        )
    _forbid_packer_team_user_scope(var_name, source, var_scope)
    return (None, None, None, None, var_scope, None)


def _parse_file_marker(
    var_name: str, source: str, raw_mode: str | None, raw_multi: str | None, raw_scope: str | None
):
    """Parse a ``@openstack:file`` marker.

    File markers have their own slot semantics: the mode slot carries the
    scope (``all``/``team``/``user``) and the multi slot carries the
    MANDATORY extension filter (``pdf`` or ``pdf|docx``). Handled
    separately so the generic mode/multi logic stays untouched.
    """
    if source == "packer":
        # Packer builds an image — file variables would never reach the
        # build (the files path today merges hard-coded into
        # ``userInputVar.terraform``). Rather than a silent trap: a
        # marker error.
        raise MarkerError(
            var_name,
            "``@openstack:file`` ist in Packer-Variablen nicht "
            "unterstützt — Dateien werden ausschließlich im "
            "Terraform-Pfad zugestellt",
            code="MARKER_FILE_PACKER_FORBIDDEN",
        )

    file_scope: str | None = None
    if raw_mode is not None and raw_mode != "":
        rs = raw_mode.lower()
        if rs in FILE_SCOPES:
            file_scope = rs
        else:
            scope_suggestion = _closest_match(rs, FILE_SCOPES)
            hint = f"; meintest du '{scope_suggestion}'?" if scope_suggestion else ""
            raise MarkerError(
                var_name,
                f"ungültiger file-scope '{raw_mode}'{hint} — erwartet "
                f"{sorted(FILE_SCOPES)}",
                code="MARKER_INVALID_FILE_SCOPE",
            )

    # The multi slot is now the mandatory extensions filter. An empty
    # slot is an error — file variables need an explicit allow-list so
    # the wizard can filter in the ``accept`` attribute and the backend
    # upload has a clear validation path.
    #
    # Regex detail: for values with ``|`` (e.g. ``pdf|docx``) the content
    # lands in the fourth slot instead of the third, because the third
    # slot does not accept a pipe. We accept that transparently — both
    # positions are checked for the extensions content.
    exts_slot: str | None = None
    if raw_multi not in (None, ""):
        exts_slot = raw_multi
        if raw_scope not in (None, ""):
            raise MarkerError(
                var_name,
                f"@openstack:file akzeptiert keinen fünften Slot "
                f"(angegeben: '{raw_scope}') — der Scope steht im "
                f"dritten Slot (z.B. ``@openstack:file:user:pdf``)",
                code="MARKER_FILE_EXTRA_SLOT",
            )
    elif raw_scope not in (None, ""):
        exts_slot = raw_scope
    if exts_slot is None:
        raise MarkerError(
            var_name,
            "``@openstack:file`` braucht einen Endungsfilter im "
            "vierten Slot, z.B. ``@openstack:file:all:pdf`` oder "
            "``@openstack:file:user:pdf|docx``",
            code="MARKER_FILE_MISSING_EXTENSIONS",
        )
    exts_raw = exts_slot.lower()
    if not _FILE_EXTENSIONS_RE.match(exts_raw):
        raise MarkerError(
            var_name,
            f"ungültiger Endungsfilter '{exts_slot}' — erlaubt sind "
            f"alphanumerische Endungen, mehrere getrennt mit '|' "
            f"(z.B. ``pdf|docx``)",
            code="MARKER_FILE_INVALID_EXTENSIONS",
        )
    file_exts = exts_raw.split("|")

    return ("file", None, None, file_scope, file_scope, file_exts)


def _parse_marker_mode(var_name: str, os_type: str, raw_mode: str | None) -> str | None:
    """Parse the mode slot (``id``/``name``) of a resource marker.

    Empty slot → ``None`` (defaults applied by the caller). Raises with a
    targeted hint when the author placed a multi-flag or a var_scope into
    the mode slot, or used an unknown token.
    """
    if raw_mode is None:
        return None
    rm = raw_mode.lower()
    if rm == "":
        # An empty slot is allowed: ``@openstack:flavor::multi`` means
        # "mode = default, multi = multi". We leave ``mode = None``; the
        # defaults are applied by the caller.
        return None
    if rm in ("id", "name"):
        return rm
    if rm in ("multi", "list", "single"):
        # Common author mistake: the user wanted to set ``:multi`` but
        # didn't leave the mode slot empty. Instead of a generic "invalid
        # mode" message, show the correct marker.
        raise MarkerError(
            var_name,
            f"'{raw_mode}' ist ein multi-Flag, nicht ein Mode — "
            f"schreibe den Marker mit leerem Mode-Slot, z.B. "
            f"``@openstack:{os_type}::{rm}``",
            code="MARKER_MULTI_IN_MODE_SLOT",
        )
    if rm in VAR_SCOPES:
        # var-scope-in-mode-slot: same logic as multi-in-mode-slot. The
        # app author wanted to set the ``var_scope`` but didn't leave the
        # middle slots empty (``@openstack:flavor:team`` instead of
        # ``@openstack:flavor:::team``). Instead of a cryptic "invalid
        # mode" message, show the correct marker.
        raise MarkerError(
            var_name,
            f"'{raw_mode}' ist ein var_scope, nicht ein Mode — "
            f"schreibe den Marker mit leerem Mode-/Multi-Slot, z.B. "
            f"``@openstack:{os_type}:::{rm}``",
            code="MARKER_SCOPE_IN_MODE_SLOT",
        )
    mode_suggestion = _closest_match(rm, {"id", "name"})
    hint = f"; meintest du '{mode_suggestion}'?" if mode_suggestion else ""
    raise MarkerError(
        var_name,
        f"ungültiger mode '{raw_mode}'{hint} — erwartet 'id' oder 'name'",
        code="MARKER_INVALID_MODE",
    )


def _parse_marker_multi(var_name: str, raw_multi: str | None) -> bool | None:
    """Parse the multi slot (``multi``/``list``/``single``) of a marker.

    ``list`` is a synonym for ``multi``. Empty slot → ``None``.
    """
    if raw_multi is None:
        return None
    mm = raw_multi.lower()
    if mm == "":
        return None
    if mm in ("multi", "list"):
        return True
    if mm == "single":
        return False
    multi_suggestion = _closest_match(mm, {"multi", "list", "single"})
    hint = f"; meintest du '{multi_suggestion}'?" if multi_suggestion else ""
    raise MarkerError(
        var_name,
        f"ungültiger multi-Flag '{raw_multi}'{hint} — erwartet "
        "'multi', 'list' oder 'single'",
        code="MARKER_INVALID_MULTI",
    )


def _collection_check_type(type_lower: str, var_scope: str | None) -> str:
    """Return the HCL type to run the collection check against.

    For scope team/user the wizard contract requires a ``map(...)`` HCL
    type. A naive ``is_collection`` check would fail (``map(list(string))``
    starts with ``map(``) even though the inner element type is a real
    collection. For scoped markers we unwrap the outer ``map(...)`` and
    check the INNER type against the multi expectation.
    """
    if var_scope not in ("team", "user") or not type_lower.startswith("map("):
        return type_lower
    # Bracket-balance the inner part out of ``map(...)``. Naive
    # ``[4:-1]`` slicing isn't enough because nested ``map(map(...))`` is
    # legitimate — we walk the characters once and count parentheses.
    depth = 0
    start = type_lower.find("(")
    inner_end = -1
    for i in range(start, len(type_lower)):
        ch = type_lower[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                inner_end = i
                break
    if inner_end > start + 1:
        return type_lower[start + 1:inner_end].strip()
    return type_lower


def _check_multi_type_conflict(
    var_name: str, var_type: str, type_for_collection_check: str, multi: bool | None
) -> None:
    """Cross-check the marker's ``:multi``/``:single`` against the HCL type.

    ``list``/``set``/``tuple`` are the collection-capable picker types;
    for these ``:multi`` is natural. ``map``/``object`` are technical
    collections the picker can't drive, so they're treated like
    single-strings for conflict detection.
    """
    is_collection_type = (
        type_for_collection_check.startswith(("list(", "set(", "tuple("))
        or type_for_collection_check in ("list", "set")
    )
    if multi is True and not is_collection_type and type_for_collection_check not in ("", "string"):
        # ``string`` is let through because many apps declare
        # ``type = string`` without a multi-marker and the frontend then
        # delivers CSV anyway. But e.g. ``type = number`` or
        # ``type = map(...)`` with ``:multi`` is clearly contradictory.
        raise MarkerError(
            var_name,
            f"marker deklariert ':multi', aber HCL-Type ist '{var_type}' "
            "— erlaubt sind nur ``string``, ``list(...)``, ``set(...)`` "
            "und ``tuple(...)``",
            code="MARKER_MULTI_TYPE_CONFLICT",
        )
    if multi is False and is_collection_type:
        raise MarkerError(
            var_name,
            f"marker deklariert ':single', aber HCL-Type ist '{var_type}' "
            "(eine list/set/tuple-Kollektion) — fixe einen der beiden",
            code="MARKER_SINGLE_TYPE_CONFLICT",
        )


def _parse_resource_marker(
    var_name: str,
    var_type: str,
    source: str,
    os_type: str,
    raw_mode: str | None,
    raw_multi: str | None,
    raw_scope: str | None,
):
    """Parse a generic (non-file) resource marker: mode, multi, scope."""
    mode = _parse_marker_mode(var_name, os_type, raw_mode)
    multi = _parse_marker_multi(var_name, raw_multi)

    type_lower = (var_type or "").strip().lower()
    # Parse the scope once; reused for both the inner-type collection
    # lookup and the returned value so the same error can't fire twice.
    var_scope = _parse_var_scope(var_name, raw_scope)
    type_for_collection_check = _collection_check_type(type_lower, var_scope)
    _check_multi_type_conflict(var_name, var_type, type_for_collection_check, multi)

    _forbid_packer_team_user_scope(var_name, source, var_scope)
    return (os_type, mode, multi, None, var_scope, None)


def parse_marker(
    var_name: str, var_type: str, description: str, source: str = "terraform"
) -> tuple[str | None, str | None, bool | None, str | None, str | None, list[str] | None]:
    """
    Parse the ``@openstack:<type>[:<mode>][:<multi>][:<var_scope>]`` marker
    from the description. Returns ``(None, None, None, None, None, None)``
    when NO marker is present (not an error — the variable renders as free
    text).

    Multi-marker behavior: if several markers are found, the first with a
    known type OR an empty type slot (a pure var_scope marker) is used.
    This is intentionally tolerant. Mode/multi validation errors of the
    chosen marker remain hard failures.

    Raises ``MarkerError`` on:
      - a malformed marker (too many segments, internal whitespace,
        unknown mode/multi/scope tokens, wrong slot separators)
      - a marker contradicting the HCL type (``:single`` with
        ``type = list(...)`` or ``:multi`` with ``type = number``;
        ``:team``/``:user`` with ``type = string``)
      - file-specific: invalid scope, missing extension filter, or an
        invalid filter.
      - packer source with ``var_scope in {team, user}``.

    Returns: ``(os_type, mode, multi, file_scope, var_scope, file_exts)``.

    * ``os_type``     — None when the marker had an empty type (pure
                        var_scope marker).
    * ``mode``        — set for non-file only.
    * ``multi``       — set for non-file only.
    * ``file_scope``  — set for file only (``all``/``team``/``user``).
    * ``var_scope``   — generic scope (``all``/``team``/``user``); for file
                        variables it mirrors ``file_scope`` so the wizard
                        has one source for slot resolution.
    * ``file_exts``   — set for file only: list of allowed extensions
                        (e.g. ``["pdf", "docx"]``), order stable.

    The heavy lifting is delegated to focused helpers: this function only
    orchestrates the pipeline (reject malformed → select marker →
    dispatch to the scope-only / file / generic resource parser).
    """
    if not description:
        return (None, None, None, None, None, None)

    _reject_malformed_markers(var_name, description)

    chosen = _select_marker(var_name, description)
    if chosen is None:
        return (None, None, None, None, None, None)

    _, raw_type, raw_mode, raw_multi, raw_scope = chosen
    os_type: str | None = raw_type.lower() if raw_type else None

    if os_type is None:
        return _parse_scope_only_marker(var_name, source, raw_mode, raw_multi, raw_scope)

    if os_type == "file":
        return _parse_file_marker(var_name, source, raw_mode, raw_multi, raw_scope)

    return _parse_resource_marker(
        var_name, var_type, source, os_type, raw_mode, raw_multi, raw_scope
    )



def apply_defaults(
    os_type: str, mode: str | None, multi: bool | None, var_type: str
) -> tuple[str, bool]:
    """
    Apply the documented defaults when the marker leaves slots empty:

    - ``mode``: 'name'. For ``NAME_ONLY_TYPES`` (keypair, availability
      zone, floating-IP pool) 'name' is effectively the only useful
      choice; ``:id`` is respected but yields little.
    - ``multi``: derived from the HCL type — ``list``/``set``/``tuple``
      → True, else False.
    """
    if mode is None:
        mode = "name"

    if multi is None:
        type_lower = (var_type or "").strip().lower()
        # ``map(...)``/``object({...})`` are technically collections but
        # the picker can't drive them, so we treat them as "single" and
        # leave it to the author to request ``:multi`` explicitly.
        # ``list``/``set``/``tuple`` are auto-detected as multi.
        multi = (
            type_lower.startswith(("list(", "set(", "tuple("))
            or type_lower in ("list", "set")
        )

    return (mode, multi)


def _closest_match(s: str, candidates: set[str]) -> str | None:
    """
    Simple Levenshtein-1 heuristic for "did you mean …?" hints.
    ``difflib`` is imported lazily since this is the only place it's used.
    """
    if not s:
        return None
    matches = difflib.get_close_matches(s, candidates, n=1, cutoff=0.7)
    return matches[0] if matches else None
