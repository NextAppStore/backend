import enum
import uuid

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base
from app.utils.time import utcnow


# ----------------------------------------------------------------
# ENUMS
# ----------------------------------------------------------------
class UserRole(str, enum.Enum):
    STUDENT = "student"
    TEACHER = "teacher"
    ADMIN = "admin"


class AppVersionApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TaskType(str, enum.Enum):
    DEPLOY = "deploy"
    UPDATE = "update"
    DESTROY = "destroy"
    PAUSE = "pause"
    RESUME = "resume"
    # Single-resource redeploy of ONE Compute-Instance via
    # ``tofu apply -replace=<addr> -target=<addr>``, without
    # touching the other team VMs of the same deployment.
    REDEPLOY = "redeploy"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OpenStackAuthType(str, enum.Enum):
    APPLICATION_CREDENTIAL = "v3applicationcredential"
    PASSWORD = "password"


class VmTopology(str, enum.Enum):
    SHARED = "shared"
    PER_TEAM = "per_team"
    PER_USER = "per_user"
    CUSTOM = "custom"


# Backward-compatibility alias
Topology = VmTopology


# ----------------------------------------------------------------
# COURSE MODEL
# ----------------------------------------------------------------
class Course(Base):
    __tablename__ = "courses"

    courseId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    # Moodle LTI context id (the "course" on the platform side). Used to
    # find-or-create the matching Course row on an LTI launch.
    lti_context_id = Column(String, unique=True, index=True, nullable=True)

    # Relationships
    users = relationship("User", back_populates="course")
    # Many-to-many to User via the ``course_teachers`` join table:
    # a course can have several teachers and a teacher several courses.
    # Backs the ``is_course_teacher`` capability check.
    course_teachers = relationship(
        "CourseTeacher",
        back_populates="course",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ----------------------------------------------------------------
# USER MODEL
# ----------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    userId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    keycloak_id = Column(String, unique=True, index=True, nullable=True)  # Keycloak User ID (sub)
    # LTI 1.3 identity (Moodle). ``sub`` is only unique per-issuer, so the
    # two columns together are the identity key — never match on
    # ``lti_sub`` alone. Both nullable: a user may have a Keycloak
    # identity, an LTI identity, or (after email-linking) both.
    lti_iss = Column(String, nullable=True)
    lti_sub = Column(String, nullable=True)
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, nullable=False)
    firstName = Column(String, nullable=True)
    lastName = Column(String, nullable=True)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.STUDENT)
    courseId = Column(UUID(as_uuid=True), ForeignKey("courses.courseId"), nullable=True, index=True)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("lti_iss", "lti_sub", name="uq_users_lti_identity"),)

    # Relationships
    course = relationship("Course", back_populates="users")
    apps = relationship("App", back_populates="user")
    deployments = relationship("Deployment", back_populates="user")
    user_to_deployments = relationship("UserToDeployment", back_populates="user")
    user_to_teams = relationship("UserToTeam", back_populates="user")
    course_teacher_links = relationship(
        "CourseTeacher",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    openstack_credential = relationship(
        "UserOpenStackCredential",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ----------------------------------------------------------------
# APP MODEL
# ----------------------------------------------------------------
class App(Base):
    __tablename__ = "apps"

    appId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    image = Column(LargeBinary, nullable=True)  # raw bytes of the uploaded logo
    image_mime = Column(
        String(64), nullable=True
    )  # e.g. "image/png" — needed to build a data-URL on read
    git_link = Column(String, nullable=True)
    is_private = Column(Boolean, nullable=False, default=False)
    userId = Column(UUID(as_uuid=True), ForeignKey("users.userId"), nullable=False, index=True)
    vm_topology = Column(
        Enum(VmTopology, values_callable=lambda x: [e.value for e in x], name="vmtopology"),
        nullable=True,
    )
    requirements = Column(Text, nullable=True)
    recommended_for = Column(Text, nullable=True)

    created_at = Column(DateTime, default=utcnow)
    # Soft-delete marker. When set, the app is hidden from default
    # queries but the row stays so existing deployments keep their FK
    # valid. Soft-delete is refused while the app has live deployments.
    deleted_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="apps")
    deployments = relationship("Deployment", back_populates="app")
    version_approvals = relationship(
        "AppVersionApproval",
        back_populates="app",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ----------------------------------------------------------------
# DEPLOYMENT MODEL
# ----------------------------------------------------------------
class Deployment(Base):
    __tablename__ = "deployments"

    deploymentId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    releaseTag = Column(String, nullable=True)
    userInputVar = Column(Text, nullable=True)  # could also be JSON
    userId = Column(UUID(as_uuid=True), ForeignKey("users.userId"), nullable=False, index=True)
    appId = Column(UUID(as_uuid=True), ForeignKey("apps.appId"), nullable=False, index=True)
    # Soft-delete marker. Set to hide the deployment from default
    # queries while keeping the row for audit/restore. DELETE is only
    # allowed in terminal states, so OpenStack resources are already
    # gone by the time this is set.
    deleted_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="deployments")
    app = relationship("App", back_populates="deployments")
    user_to_deployments = relationship(
        "UserToDeployment",
        back_populates="deployment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    teams = relationship(
        "Team",
        back_populates="deployment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    tasks = relationship(
        "Task",
        back_populates="deployment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ----------------------------------------------------------------
# TASK MODEL
# ----------------------------------------------------------------
class Task(Base):
    """One job run on a deployment, and at the same time its queue entry.

    The API inserts it PENDING with a sealed payload in the same
    transaction as the business change (``task_service``); committing is
    the hand-over to the worker. The worker claims it, appends
    ``TaskEvent`` rows while it runs and records ``logs``/``outputs`` and
    the final status. The API then does the follow-up work once
    (``task_finalizer``) and sets ``finalized_at``.

    The worker's database role may only touch the columns listed in the
    ``worker_role`` migration; renaming one of them breaks the worker
    (see ``tests/test_worker_contract.py``).
    """

    __tablename__ = "tasks"
    __table_args__ = (
        # At most one pending or running task per deployment: the backstop
        # for the advisory locks in ``crud.locks``. Declared here and not
        # only in a migration, because autogenerate drops indexes the model
        # doesn't know about (migration 73fd123a60aa did exactly that).
        Index(
            "uq_tasks_active_per_deployment",
            "deploymentId",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'RUNNING')"),
        ),
        # What the worker's claim and the API's finalizer and reaper look
        # at. Partial, so they stay small however long the history gets.
        Index("ix_tasks_pending", "created_at", postgresql_where=text("status = 'PENDING'")),
        Index("ix_tasks_unfinalized", "finished_at", postgresql_where=text("finalized_at IS NULL")),
        Index("ix_tasks_running_lease", "lease_until", postgresql_where=text("status = 'RUNNING'")),
    )

    taskId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deploymentId = Column(
        UUID(as_uuid=True),
        ForeignKey("deployments.deploymentId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type = Column(Enum(TaskType), nullable=False)
    status = Column(Enum(TaskStatus), default=TaskStatus.PENDING)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    logs = Column(Text, nullable=True)  # JSON or text
    outputs = Column(Text, nullable=True)  # OpenTofu outputs as JSON/text
    # Last known phase/percent, written by the worker with every progress
    # event, so a page reload shows where a running task is without
    # replaying its event stream.
    current_phase = Column(String(50), nullable=True)
    progress_pct = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    # --- Queue columns --------------------------------------------------
    # Everything the worker needs to run the job (app, release, variables,
    # teams, the caller's OpenStack credential envelope, the state token),
    # Fernet-encrypted JSON. Cleared when a worker claims the task, so it
    # sits in the database only while nobody works on it.
    payload = Column(LargeBinary, nullable=True)
    # SHA-256 of the per-task token that opens the deployment's OpenTofu
    # state in ``/internal/tfstate`` while the task runs.
    state_token_hash = Column(String(64), nullable=True)
    # Which worker slot holds the task, and until when. The worker renews
    # the lease while the job runs; a RUNNING task whose lease ran out
    # belongs to a worker that died, and ``task_finalizer.reap_expired``
    # fails it. Nothing is retried automatically.
    claimed_by = Column(String, nullable=True)
    lease_until = Column(DateTime, nullable=True)
    # Set once the follow-up work for a finished task ran (mails after a
    # deploy, soft-delete after a destroy). Claimed with SKIP LOCKED, so
    # that work runs once however many API processes there are.
    finalized_at = Column(DateTime, nullable=True)

    # Relationships
    deployment = relationship("Deployment", back_populates="tasks")
    events = relationship(
        "TaskEvent",
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ----------------------------------------------------------------
# TASK EVENT MODEL
# ----------------------------------------------------------------
class TaskEvent(Base):
    """One progress, log or terminal event of a task.

    Appended by the worker while the job runs (the terminal one when the
    task ends). The live stream reads them in ``id`` order, so the id is
    also the SSE ``Last-Event-ID``. ``payload`` is the JSON object sent
    to the browser unchanged.
    """

    __tablename__ = "task_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    taskId = Column(
        UUID(as_uuid=True),
        ForeignKey("tasks.taskId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # "task-progress", "task-log", "task-succeeded", ...
    type = Column(String(32), nullable=False)
    payload = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    task = relationship("Task", back_populates="events")


# ----------------------------------------------------------------
# TERRAFORM STATE MODEL
# ----------------------------------------------------------------
class TerraformState(Base):
    """The OpenTofu state of one deployment.

    Written and read by OpenTofu through the API's HTTP state backend
    (``routers/internal_tfstate``), read by the API for the resource
    views. Encrypted, because the state holds every attribute of every
    resource, generated passwords included. Dropped after a successful
    destroy.
    """

    __tablename__ = "terraform_states"

    deploymentId = Column(
        UUID(as_uuid=True),
        ForeignKey("deployments.deploymentId", ondelete="CASCADE"),
        primary_key=True,
    )
    state = Column(LargeBinary, nullable=True)  # Fernet, JSON inside
    # OpenTofu's state lock: its ID, the lock info JSON it sent (returned
    # to a competing client) and the task holding it. A lock whose task is
    # no longer RUNNING is stale and may be taken over, so a worker that
    # died mid-apply cannot block the deployment for good.
    lock_id = Column(String, nullable=True)
    lock_info = Column(Text, nullable=True)
    lock_task_id = Column(UUID(as_uuid=True), nullable=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


# ----------------------------------------------------------------
# LTI LOGIN STATE MODEL
# ----------------------------------------------------------------
class LtiLoginState(Base):
    """One OIDC ``state``/``nonce`` pair issued by ``/lti/login``.

    Shared by all API processes (Moodle's login and launch requests can
    land on different ones), single use, short-lived. See
    ``app.utils.lti_auth``.
    """

    __tablename__ = "lti_login_states"

    state = Column(String, primary_key=True)
    nonce = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


# ----------------------------------------------------------------
# QUEUE NOTIFICATIONS
# ----------------------------------------------------------------
# NOTIFY is only a wake-up call; the data is always read from the tables.
# Postgres delivers a NOTIFY when the transaction commits, so nobody is
# woken for a row they cannot see yet. Payloads are ids only (NOTIFY is
# capped at 8000 bytes).
#
# * task_queued   — a PENDING task was inserted; wakes the worker slots.
# * task_events   — a task event was inserted; payload: deployment id,
#                   wakes the live streams of that deployment.
# * task_finished — a task reached a terminal status; payload: task id,
#                   wakes the API's finalizer.
#
# Attached to the tables so ``metadata.create_all`` (tests) creates them
# too; migration ``e7f8a9b0c1d2`` creates the same for real databases.
QUEUE_TRIGGER_FUNCTIONS_SQL = """
CREATE OR REPLACE FUNCTION appstore_notify_task_queued() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('task_queued', '');
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION appstore_notify_task_finished() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('task_finished', NEW."taskId"::text);
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION appstore_notify_task_event() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify(
        'task_events',
        (SELECT "deploymentId"::text FROM tasks WHERE "taskId" = NEW."taskId")
    );
    RETURN NULL;
END $$;
"""

TASKS_TRIGGERS_SQL = """
CREATE TRIGGER tasks_notify_queued
    AFTER INSERT ON tasks
    FOR EACH ROW WHEN (NEW.status = 'PENDING')
    EXECUTE FUNCTION appstore_notify_task_queued();

CREATE TRIGGER tasks_notify_finished
    AFTER UPDATE OF status ON tasks
    FOR EACH ROW WHEN (
        OLD.status IS DISTINCT FROM NEW.status
        AND NEW.status IN ('SUCCESS', 'FAILED', 'CANCELLED')
    )
    EXECUTE FUNCTION appstore_notify_task_finished();
"""

TASK_EVENTS_TRIGGERS_SQL = """
CREATE TRIGGER task_events_notify
    AFTER INSERT ON task_events
    FOR EACH ROW
    EXECUTE FUNCTION appstore_notify_task_event();
"""

event.listen(Task.__table__, "after_create", DDL(QUEUE_TRIGGER_FUNCTIONS_SQL + TASKS_TRIGGERS_SQL))
event.listen(TaskEvent.__table__, "after_create", DDL(TASK_EVENTS_TRIGGERS_SQL))


# ----------------------------------------------------------------
# USERTODEPLOYMENT MODEL
# ----------------------------------------------------------------
class UserToDeployment(Base):
    __tablename__ = "user_to_deployments"

    userToDeploymentId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    userId = Column(
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    deploymentId = Column(
        UUID(as_uuid=True),
        ForeignKey("deployments.deploymentId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationships
    user = relationship("User", back_populates="user_to_deployments")
    deployment = relationship("Deployment", back_populates="user_to_deployments")


# ----------------------------------------------------------------
# TEAM MODEL
# ----------------------------------------------------------------
class Team(Base):
    __tablename__ = "teams"

    teamId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    deploymentId = Column(
        UUID(as_uuid=True),
        ForeignKey("deployments.deploymentId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationships
    deployment = relationship("Deployment", back_populates="teams")
    user_to_teams = relationship(
        "UserToTeam",
        back_populates="team",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ----------------------------------------------------------------
# USERTOTEAM MODEL
# ----------------------------------------------------------------
class UserToTeam(Base):
    __tablename__ = "user_to_teams"

    userToTeamId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    userId = Column(
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    teamId = Column(
        UUID(as_uuid=True),
        ForeignKey("teams.teamId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationships
    user = relationship("User", back_populates="user_to_teams")
    team = relationship("Team", back_populates="user_to_teams")


# ----------------------------------------------------------------
# USER OPENSTACK CREDENTIAL MODEL
# ----------------------------------------------------------------
class UserOpenStackCredential(Base):
    __tablename__ = "user_openstack_credentials"

    credentialId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    userId = Column(
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    auth_type = Column(
        Enum(
            OpenStackAuthType,
            name="openstackauthtype",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )

    # Non-secret display fields (plaintext)
    auth_url = Column(String, nullable=False)
    region_name = Column(String, nullable=True)
    interface = Column(String, nullable=True, default="public")
    identity_api_version = Column(String, nullable=True, default="3")
    project_id = Column(String, nullable=True)
    project_name = Column(String, nullable=True)
    user_domain_name = Column(String, nullable=True)
    project_domain_name = Column(String, nullable=True)

    # Encrypted (Fernet ciphertext) — never logged, never returned via API
    encrypted_identifier = Column(LargeBinary, nullable=False)
    encrypted_secret = Column(LargeBinary, nullable=False)

    last_validated_at = Column(DateTime, nullable=True)
    last_validation_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    user = relationship("User", back_populates="openstack_credential")


# ----------------------------------------------------------------
# APP VERSION APPROVAL MODEL
# ----------------------------------------------------------------
class AppVersionApproval(Base):
    __tablename__ = "app_version_approvals"

    approvalId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    appId = Column(
        UUID(as_uuid=True),
        ForeignKey("apps.appId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_tag = Column(String, nullable=False)
    status = Column(
        Enum(AppVersionApprovalStatus),
        nullable=False,
        default=AppVersionApprovalStatus.PENDING,
    )
    diff_url = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    rejection_reason = Column(Text, nullable=True)
    reviewed_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("appId", "version_tag", name="uq_app_version_approval"),)

    # Relationships
    app = relationship("App", back_populates="version_approvals")
    reviewer = relationship("User", foreign_keys=[reviewed_by])


# ----------------------------------------------------------------
# COURSE TEACHER MODEL
# ----------------------------------------------------------------
# Many-to-many join between courses and users. A row ``(course_id,
# user_id)`` declares that ``user`` is one of the teachers responsible
# for ``course``. Backs the per-course "course-teacher" capability.
# Both FKs together form the composite primary key.
class CourseTeacher(Base):
    __tablename__ = "course_teachers"

    courseId = Column(
        "course_id",
        UUID(as_uuid=True),
        ForeignKey("courses.courseId", ondelete="CASCADE"),
        primary_key=True,
    )
    userId = Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )

    # Relationships
    course = relationship("Course", back_populates="course_teachers")
    user = relationship("User", back_populates="course_teacher_links")
