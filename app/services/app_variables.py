"""Load an app's variable declarations out of its Git repository.

This is the boundary layer between the pure HCL parsers in
:mod:`app.services.hcl` and the HTTP world: it clones the release's
variable files, runs the parsers over them, and translates the parsers'
domain errors into ``HTTPException``. Everything below it is free of
FastAPI; everything above it is a router.

Two routers consume it — ``GET /apps/{id}/variables`` (the wizard) and
``POST /deployments`` (server-side enforcement of the app author's
``varScope`` / ``fileExtensions`` contracts) — plus the admin approval
gate in ``POST /admin/apps/{id}/versions/{tag}/approve``.
"""

import logging
import os
from typing import Any

from fastapi import HTTPException, status

from app.services.git_service import git_service
from app.services.hcl import (
    PackerTemplateDiscoveryError,
    discover_packer_templates,
    parse_packer_variables,
    parse_terraform_variables,
)

logger = logging.getLogger(__name__)


def load_variable_definitions(app, version: str) -> list[dict[str, Any]]:
    """Clone the app's release-vars and parse all Terraform/Packer
    variables into the same shape ``GET /apps/{id}/variables`` returns.

    Reusable from ``POST /deployments`` so the deployment endpoint can
    enforce per-variable contracts (``varScope``, ``fileExtensions``)
    using the App-Autor's declarations as source-of-truth. Cleans up
    the temporary clone on its own — callers don't manage paths.

    Raises ``HTTPException(400)`` if the app has no Git link and
    bubbles unexpected errors as ``HTTPException(500)``.
    """
    if not app.git_link:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="App has no Git repository configured",
        )

    deployment_id = f"vars_{app.appId}_{version}".replace("/", "_")
    repo_path = None
    try:
        repo_path = git_service.clone_release_vars(app.git_link, version, deployment_id)
        variables: list[dict[str, Any]] = []
        tf_vars_path = os.path.join(repo_path, "terraform", "variables.tf")
        if os.path.exists(tf_vars_path):
            variables.extend(parse_terraform_variables(tf_vars_path))
        # Discover all Packer templates (legacy single-file layout OR
        # per-key subdirectories) and parse each one's variables. The
        # ``template_key`` is recorded on every Packer variable so the
        # wizard can group inputs per image. Discovery raises if the
        # repo has an ambiguous or unsafe layout — surface that as
        # HTTP 422 so the app author can fix the repo before any
        # deploy attempt.
        try:
            templates = discover_packer_templates(repo_path)
        except PackerTemplateDiscoveryError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            )
        for tmpl in templates:
            if os.path.isfile(tmpl.variables_path):
                variables.extend(
                    parse_packer_variables(tmpl.variables_path, template_key=tmpl.key)
                )
        return variables
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "Failed to load variable definitions for app %s version %s",
            app.appId, version,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch variables",
        )
    finally:
        if repo_path:
            try:
                git_service.cleanup_repository(repo_path)
            except Exception as cleanup_error:
                logger.error(
                    "Failed to cleanup repository: %s", str(cleanup_error)
                )
