import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import libpq_url
from app.routers import (
    admin_apps,
    apps,
    auth_keycloak,
    auth_lti,
    courses,
    dashboard,
    deployments,
    internal_tfstate,
    openstack_credentials,
    openstack_resources,
    quotas,
    tasks,
    teams,
    users,
)
from app.services import task_finalizer
from app.services.db_events import dispatcher as db_events

logger = logging.getLogger(__name__)


# ``DISABLE_BACKGROUND_TASKS`` short-circuits the lifespan body so the
# app is fully wired but the database listener and the task reaper are
# not started. Used by the test suite, where every TestClient would
# otherwise open its own LISTEN connection and reaper loop.
def _background_tasks_disabled() -> bool:
    return os.getenv("DISABLE_BACKGROUND_TASKS", "").lower() in ("1", "true", "yes")


# ----------------------------------------------------------------
# STARTUP/SHUTDOWN
# ----------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("=== Application Starting ===")
    logger.info("ℹ️  Use 'alembic upgrade head' to apply database migrations")

    if _background_tasks_disabled():
        logger.info(
            "DISABLE_BACKGROUND_TASKS set — not starting the database "
            "listener and the task reaper (test mode)"
        )
        try:
            yield
        finally:
            logger.info("=== Application Shutting Down (test mode) ===")
        return

    # One LISTEN connection per process: wakes this process's live streams
    # on ``task_events`` and hands ``task_finished`` to the finalizer. On
    # every (re)connect it catches up on tasks that finished meanwhile.
    db_events.start(
        libpq_url(settings.DATABASE_URL),
        on_task_finished=task_finalizer.finalize_task_by_id,
        on_connected=task_finalizer.catch_up,
    )
    logger.info("Database listener started")

    # Fails tasks of workers whose lease ran out. A fixed interval is fine
    # here: noticing a crashed worker is not latency-critical.
    reaper_task = asyncio.create_task(task_finalizer.run_reaper())
    logger.info("Task reaper scheduled")

    logger.info("Application started")

    try:
        yield
    finally:
        # Shutdown
        logger.info("=== Application Shutting Down ===")
        reaper_task.cancel()
        try:
            await reaper_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Task reaper raised on shutdown")
        await db_events.stop()
        logger.info("Shutdown complete")


# ----------------------------------------------------------------
# FASTAPI APP
# ----------------------------------------------------------------
app = FastAPI(
    title="Backend API",
    description="FastAPI Backend with Auth, Git & a Postgres job queue",
    version="1.0.0",
    lifespan=lifespan
)

# ----------------------------------------------------------------
# CORS
# ----------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------------------
# ROUTERS
# ----------------------------------------------------------------
app.include_router(auth_keycloak.router, prefix="/auth", tags=["Authentication"])
app.include_router(auth_lti.router, prefix="/lti", tags=["LTI"])
app.include_router(users.router, prefix="/users", tags=["Users"])
app.include_router(courses.router, prefix="/courses", tags=["Courses"])
app.include_router(apps.router, prefix="/apps", tags=["Apps"])
app.include_router(admin_apps.router, prefix="/admin", tags=["Admin"])
app.include_router(deployments.router, prefix="/deployments", tags=["Deployments"])
app.include_router(tasks.router, prefix="/tasks", tags=["Tasks"])
app.include_router(teams.router, prefix="/teams", tags=["Teams"])
app.include_router(quotas.router, prefix="/quotas", tags=["Quotas"])
app.include_router(dashboard.router, prefix="/dashboard", tags=["Dashboard"])
app.include_router(openstack_credentials.router, tags=["OpenStack Credentials"])
# OpenTofu's HTTP state backend for the worker's jobs (per-task token,
# not in the OpenAPI document, not routed by the public nginx).
app.include_router(internal_tfstate.router, prefix="/internal/tfstate")
# Read API for OpenStack resources (Networks, Flavors, Images, ...),
# used by the wizard's value-help dropdowns so users don't have to type
# UUIDs from Horizon.
app.include_router(
    openstack_resources.router,
    prefix="/me/openstack/resources",
    tags=["OpenStack Resources"],
)


# ----------------------------------------------------------------
# HEALTH CHECK
# ----------------------------------------------------------------
@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "backend-api",
        "version": "1.0.0"
    }
