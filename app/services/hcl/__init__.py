"""HCL parsing for app repositories.

Three layers, bottom-up:

* :mod:`markers`   — the ``@openstack:...`` grammar in variable descriptions
* :mod:`variables` — ``variable "x" { ... }`` blocks → wizard dicts
* :mod:`packer`    — which Packer templates a repo declares

None of them know about HTTP, the database or the ORM. The boundary
that adds those concerns is :mod:`app.services.app_variables`.
"""

from app.services.hcl.markers import (
    FILE_SCOPES,
    NAME_ONLY_TYPES,
    OS_TYPES,
    VAR_SCOPES,
    MarkerError,
    apply_defaults,
    parse_marker,
)
from app.services.hcl.packer import (
    PackerTemplate,
    PackerTemplateDiscoveryError,
    discover_packer_templates,
)
from app.services.hcl.variables import (
    iter_variable_blocks,
    parse_one_variable,
    parse_packer_variables,
    parse_terraform_variables,
    validate_file_var_shape,
    validate_scoped_var_shape,
)

__all__ = [
    "FILE_SCOPES",
    "NAME_ONLY_TYPES",
    "OS_TYPES",
    "VAR_SCOPES",
    "MarkerError",
    "PackerTemplate",
    "PackerTemplateDiscoveryError",
    "apply_defaults",
    "discover_packer_templates",
    "iter_variable_blocks",
    "parse_marker",
    "parse_one_variable",
    "parse_packer_variables",
    "parse_terraform_variables",
    "validate_file_var_shape",
    "validate_scoped_var_shape",
]
