from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.config import settings


# ----------------------------------------------------------------
# URLS
# ----------------------------------------------------------------
def sqlalchemy_url(url: str) -> str:
    """``url`` with the psycopg (3) driver selected.

    Compose files, CI and ``.env`` files carry plain ``postgresql://`` URLs
    (or the old ``postgresql+psycopg2://``); SQLAlchemy would pick psycopg2
    for those. psycopg 3 is the only driver installed, because the live
    stream needs its asynchronous ``LISTEN`` support.
    """
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        return url
    return parsed.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def libpq_url(url: str) -> str:
    """``url`` as libpq understands it (no SQLAlchemy driver suffix), for
    connections opened with psycopg directly, like the ``LISTEN`` one."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        return url
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


# ----------------------------------------------------------------
# DATABASE ENGINE
# ----------------------------------------------------------------
engine = create_engine(
    sqlalchemy_url(settings.DATABASE_URL),
    pool_pre_ping=True,  # Verify connections before using
    pool_size=5,
    max_overflow=10
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# ----------------------------------------------------------------
# DEPENDENCY
# ----------------------------------------------------------------
def get_db():
    """Database session dependency"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
