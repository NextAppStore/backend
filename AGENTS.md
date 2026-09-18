# Backend Agent Rules & Architecture Harness

- **Hierarchie:** Erbt alle globalen System-Invarianten und Sicherheitsleitplanken aus `../org-docs/AGENTS.md`.
- **Geltungsbereich:** Verbindliche Richtlinie für alle AI-Coding-Assistenten (Cursor, Claude Code, GitHub Copilot, Antigravity) sowie menschliche Entwickler im Subsystem `backend/`.

---

## 1. Erzwungener Contract-Sync & Schema-Synchronisation (Single Source of Truth)

- **REST API Contract (`backend/openapi.json`):**
  - `openapi.json` ist die verbindliche Single Source of Truth (SSOT) für das Vue-Frontend. Sämtliche TypeScript-Typen und API-Clients werden direkt hieraus abgeleitet.
  - **Mandatory Export Rule:** Bei jeder Modifikation an FastAPI-Routern (`app/routers/`), Request-/Response-Modellen (`app/schemas.py`) oder Endpunkt-Parametern **muss** die Spezifikation vor Abschluss des Tasks über die Docker-Sandbox neu exportiert werden:
    ```bash
    python3 harness/sandbox.py python3 backend/export_openapi.py
    # oder über den CLI-Alias:
    python3 harness/sandbox.py python3 backend/api.py
    ```
  - **Invariante:** Ein Feature oder Refactoring gilt als **unvollständig**, solange Änderungen am Backend-Code nicht in `backend/openapi.json` synchronisiert und versioniert sind.

---

## 2. Alembic-Migrations-Pflicht & Datenbank-Konsistenz

- **Migrations-Zwang bei Schema-Modifikationen:**
  - Jede Anpassung an den SQLAlchemy-Tabellendefinitionen, Spalten, Datentypen, Indizes oder Constraints in `app/models.py` zieht zwingend eine neue, versionierte Migrationsdatei in `backend/alembic/versions/` nach sich.
  - Schema-Anpassungen im Code dürfen niemals ohne entsprechende Alembic-Migration committet werden.
- **Autogenerierung & Validierung gegen lokale Sandbox-PostgreSQL:**
  - Migrationen müssen innerhalb der Docker-Sandbox mit initialisierter Test-PostgreSQL generiert und unmittelbar via `upgrade head` verifiziert werden:
    ```bash
    python3 harness/sandbox.py "service postgresql start >/dev/null 2>&1 && \
      su - postgres -c \"psql -c \\\"CREATE USER testuser WITH PASSWORD 'testpass' SUPERUSER;\\\"\" >/dev/null 2>&1 || true; \
      su - postgres -c \"createdb -O testuser testdb\" >/dev/null 2>&1 || true; \
      export DATABASE_URL=\"postgresql+psycopg2://testuser:testpass@localhost:5432/testdb\" && \
      export CREDENTIAL_ENCRYPTION_KEY=\"MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE=\" && \
      export SECRET_KEY=\"test-secret-key-for-harness-testing-only\" && \
      cd backend && \
      alembic revision --autogenerate -m '<aussagekraeftiger_titel>' && \
      alembic upgrade head"
    ```
  - **Qualitätsprüfung:** Vor Abschluss ist die generierte Migrationsdatei auf ungewollte Drops, korrekte Foreign Keys und eine vollständige `downgrade()`-Implementierung zu prüfen.

---

## 3. LTI 1.3 & Authentifizierungs-Sicherheitsgrenzen (Zero-Trust)

- **Kryptografische Signaturprüfung (JWKS):**
  - Moodle- und Keycloak-/OIDC-Tokens müssen im Produktionscode ausnahmslos kryptografisch gegen das öffentliche JWKS-Keyset validiert werden (`verify_signature: True`, `verify_exp: True`).
  - **Strenges Verbot:** Das Deaktivieren der Signaturprüfung (`verify_signature: False`) oder das Umgehen der kryptografischen Validierung im Produktionscode (`app/`) ist strikt verboten.
  - **Mock-Isolation:** Signatur-Bypasses oder synthetische Decodes sind **ausschließlich** in isolierten Unit-Test-Mocks (`backend/tests/`) gestattet.
- **Strikte Mandantentrennung (Multi-Tenancy):**
  - Sämtliche Ressourcen (Deployments, VMs, Tasks, DB-Einträge) müssen zwingend an den Moodle-Kurskontext (`context.id` / `courseId`) gekoppelt sein.
  - Rollenrechte sind strikt einzuhalten: `#Instructor` / `Teacher` besitzen volle Deployment- und Lifecycle-Rechte (Deploy, Pause, Resume, Destroy); `#Learner` / `Student` erhalten ausschließlich Lesezugriff auf ihre zugewiesenen VM-Verbindungsdaten. Cross-Course-Zugriffe müssen technisch ausgeschlossen sein.

---

## 4. Asynchrone Entkopplung (Non-Blocking FastAPI)

- **Keine synchronen Cloud-Operationen in HTTP-Endpunkten:**
  - FastAPI-Endpunkte dürfen niemals langwierige oder potenziell blockierende Cloud-Aktionen (wie Terraform `apply`/`destroy`, Packer-Builds oder direkte synchrone OpenStack-Provisionierungen) direkt im Request-Thread ausführen.
  - Synchrone Aufrufe gefährden die Stabilität, führen zu HTTP-Timeouts und blockieren den ASGI-Worker.
- **Typisierte Celery-Tasks via RabbitMQ:**
  - Sämtliche Cloud- und Infrastrukturjobs müssen als typisierte Celery-Tasks an den RabbitMQ-Broker übergeben werden (`app.services.task_service.dispatch_to_celery` bzw. `celery_app.send_task(...)`).
  - **Transaktionales Muster:**
    1. Erstellen des Task-Datensatzes mit Status `PENDING` in der Datenbank.
    2. Commit der DB-Transaktion.
    3. Asynchroner Dispatch an Celery außerhalb der offenen Transaktion.
    4. Sofortige HTTP 202 (Accepted) Response an den Client mit `taskId` und Statusabfrage-Endpunkt.

---

## 5. Deterministischer Self-Correction Loop (Sandbox-Verifikation)

- **Strict Host Isolation:**
  - Führe **niemals** Linter, Tests, Paketmanager (`poetry`) oder DB-Befehle direkt auf dem Host-Betriebssystem aus. Alle Ausführungen erfolgen gekapselt über die Docker-Sandbox (`harness/sandbox.py`).
- **Gestaffelter Verifikationsablauf vor Task-Abschluss:**
  1. **Stufe 1 – Linter & Codequalität:**
     ```bash
     python3 harness/sandbox.py ruff check backend/
     ```
  2. **Stufe 2 – Fast Unit Tests:**
     ```bash
     python3 harness/sandbox.py pytest backend/tests/unit/ -q --no-cov
     # oder kombiniert via Flag:
     python3 harness/sandbox.py --fast
     ```
  3. **Stufe 3 – Gezielte Komponenten- & Integrationstests:**
     ```bash
     python3 harness/sandbox.py pytest backend/tests/<ziel_test>.py -q --no-cov
     ```
  4. **Stufe 4 – Schema- & Contract-Validierung:**
     Bei Änderungen an Routern, Models oder Schemas muss `openapi.json` aktualisiert werden:
     ```bash
     python3 harness/sandbox.py python3 backend/export_openapi.py
     ```
- **Autonome Fehleranalyse & Iteration:**
  - Tritt ein Fehler auf (Exit-Code != 0), analysiert der Agent die Fehlerausgabe (Linter-Meldung, Pytest-Failure, Traceback) eigenständig, wendet zielgerichtete Korrekturen an und wiederholt die Verifikation deterministisch, bis alle Prüfungen mit `exit code: 0` erfolgreich durchlaufen.
