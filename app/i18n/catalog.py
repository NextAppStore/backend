"""Nachrichtenkatalog (DE/EN) für alles, was das Backend ans Frontend schickt.

Jeder Eintrag hat einen stabilen Key und beide Sprachen. Platzhalter folgen
``str.format`` (``{name}``; wörtliche Klammern als ``{{`` / ``}}``).

**EN ist der Quelltext.** Die ``HTTPException``-Texte im Code bleiben Englisch
und werden im Exception-Handler (``app/i18n/handlers.py``) anhand ihres
EN-Musters nachgeschlagen. Für neue Meldungen gilt:

* Fehlertext in einer ``HTTPException``: Text im Code auf Englisch schreiben und
  hier mit gleichem Wortlaut eintragen. ``tests/unit/test_i18n_catalog.py``
  schlägt fehl, wenn ein ``detail``-Text im Code keinen Eintrag hat.
* Alles andere (Marker-Fehler, Mails, Task-Logs, SSE-Texte): mit
  ``app.i18n.t(key, **params)`` aus dem Katalog holen.

Die Struktur-Keys im Fehler-JSON (``reason``, ``code``) bleiben sprachneutral;
das Frontend mappt sie selbst.
"""

from __future__ import annotations

from typing import TypedDict


class Message(TypedDict):
    en: str
    de: str


MESSAGES: dict[str, Message] = {
    # ------------------------------------------------------------------
    # Allgemein / Routing
    # ------------------------------------------------------------------
    "http.not_found": {"en": "Not Found", "de": "Nicht gefunden"},
    "http.method_not_allowed": {
        "en": "Method Not Allowed",
        "de": "Methode nicht erlaubt",
    },
    # Von ``fastapi.security.HTTPBearer`` erzeugt, nicht aus unserem Code.
    "http.not_authenticated": {
        "en": "Not authenticated",
        "de": "Nicht authentifiziert",
    },
    "http.invalid_credentials": {
        "en": "Invalid authentication credentials",
        "de": "Ungültige Authentifizierungsdaten",
    },
    "user.not_found": {"en": "User not found", "de": "Benutzer nicht gefunden"},
    "user.profile_own_only": {
        "en": "You can only view your own profile",
        "de": "Du kannst nur dein eigenes Profil ansehen",
    },
    "user.search_too_short": {
        "en": "Search query must be at least 2 characters",
        "de": "Die Suchanfrage muss mindestens 2 Zeichen lang sein",
    },
    # ------------------------------------------------------------------
    # Apps und Freigaben
    # ------------------------------------------------------------------
    "app.not_found": {"en": "App not found", "de": "App nicht gefunden"},
    "app.no_permission": {
        "en": "You don't have permission to access this resource",
        "de": "Du hast keine Berechtigung für diese Ressource",
    },
    "app.no_git_repo": {
        "en": "App has no Git repository configured",
        "de": "Für die App ist kein Git-Repository konfiguriert",
    },
    "app.no_git_repo_lower": {
        "en": "App has no git repository configured",
        "de": "Für die App ist kein Git-Repository konfiguriert",
    },
    "app.variables_fetch_failed": {
        "en": "Failed to fetch variables",
        "de": "Die Variablen konnten nicht geladen werden",
    },
    "app.submit_marker_errors": {
        "en": "Version cannot be submitted — faulty @openstack markers in the variable files",
        "de": "Die Version kann nicht eingereicht werden — fehlerhafte "
        "@openstack-Marker in den Variablen-Dateien",
    },
    "app.approve_marker_errors": {
        "en": "Version cannot be approved — faulty @openstack markers in the variable files",
        "de": "Die Version kann nicht freigegeben werden — fehlerhafte "
        "@openstack-Marker in den Variablen-Dateien",
    },
    "approval.already_pending": {
        "en": "Version is already pending review",
        "de": "Die Version wartet bereits auf Freigabe",
    },
    "approval.already_approved": {
        "en": "Version is already approved",
        "de": "Die Version ist bereits freigegeben",
    },
    "approval.withdraw_status": {
        "en": "Only pending submissions can be withdrawn (current status: '{status}')",
        "de": "Nur ausstehende Einreichungen können zurückgezogen werden "
        "(aktueller Status: '{status}')",
    },
    "approval.not_found": {
        "en": "Version approval entry not found",
        "de": "Freigabe-Eintrag der Version nicht gefunden",
    },
    "approval.reject_status": {
        "en": "Cannot reject a version with status '{status}'",
        "de": "Eine Version im Status '{status}' kann nicht abgelehnt werden",
    },
    "approval.revoke_status": {
        "en": "Cannot revoke a version with status '{status}'",
        "de": "Die Freigabe einer Version im Status '{status}' kann nicht widerrufen werden",
    },
    "image.invalid_format": {
        "en": "image must be a data-URL like 'data:image/png;base64,<...>'",
        "de": "Das Bild muss eine Data-URL sein, z. B. 'data:image/png;base64,<...>'",
    },
    "image.invalid_base64": {
        "en": "image payload is not valid base64",
        "de": "Die Bilddaten sind kein gültiges Base64",
    },
    "packer.invalid_keys": {
        "en": "Packer template subdirectories with invalid keys "
        "(must match [a-z][a-z0-9_-]{{0,30}}): {keys}",
        "de": "Packer-Template-Unterverzeichnisse mit ungültigen Schlüsseln "
        "(erlaubt: [a-z][a-z0-9_-]{{0,30}}): {keys}",
    },
    "packer.mixed_layout": {
        "en": "App repository has BOTH packer/template.pkr.hcl (legacy layout) AND "
        "packer/<key>/template.pkr.hcl subdirectories ({keys}). Choose one layout "
        "— remove the legacy file or the subdirectories.",
        "de": "Das App-Repository enthält SOWOHL packer/template.pkr.hcl (altes Layout) "
        "ALS AUCH Unterverzeichnisse packer/<key>/template.pkr.hcl ({keys}). Bitte "
        "ein Layout wählen — entweder die alte Datei oder die Unterverzeichnisse entfernen.",
    },
    # ------------------------------------------------------------------
    # Git-Zugriff (services/git_service.py)
    # ------------------------------------------------------------------
    "git.no_invitation_github": {
        "en": "No pending invitation found for repository {repo}. Please ask the "
        "repository owner to invite the app store user.",
        "de": "Für das Repository {repo} liegt keine offene Einladung vor. Bitte den "
        "Repository-Eigentümer, den App-Store-Benutzer einzuladen.",
    },
    "git.invitation_accepted": {
        "en": "Invitation accepted successfully for {repo}",
        "de": "Einladung für {repo} erfolgreich angenommen",
    },
    "git.invitation_accept_failed": {
        "en": "Failed to accept invitation (HTTP {status})",
        "de": "Die Einladung konnte nicht angenommen werden (HTTP {status})",
    },
    "git.invitation_error": {
        "en": "Error processing invitation: {error}",
        "de": "Fehler bei der Verarbeitung der Einladung: {error}",
    },
    "git.repo_not_found_gitlab": {
        "en": "Repository not found or no pending invitation exists. Please ask the "
        "repository owner to invite the app store user to: {repo}",
        "de": "Repository nicht gefunden oder keine offene Einladung vorhanden. Bitte den "
        "Repository-Eigentümer, den App-Store-Benutzer einzuladen zu: {repo}",
    },
    "git.access_granted": {
        "en": "Access granted for {repo}",
        "de": "Zugriff auf {repo} gewährt",
    },
    "git.no_invitation_gitlab": {
        "en": "No pending invitation found. Please ask the repository owner to add the "
        "app store user as a member.",
        "de": "Keine offene Einladung gefunden. Bitte den Repository-Eigentümer, den "
        "App-Store-Benutzer als Mitglied hinzuzufügen.",
    },
    "git.unsupported_url": {
        "en": "Unable to parse repository URL or unsupported platform. Supported: "
        "GitHub, GitLab. URL: {url}",
        "de": "Die Repository-URL kann nicht gelesen werden oder die Plattform wird nicht "
        "unterstützt. Unterstützt: GitHub, GitLab. URL: {url}",
    },
    "git.token_missing": {
        "en": "Git access token is not configured. Please contact the administrator.",
        "de": "Es ist kein Git-Zugriffstoken konfiguriert. Bitte den Administrator kontaktieren.",
    },
    "git.auth_failed": {
        "en": "Authentication failed. The Git access token is invalid or expired.",
        "de": "Authentifizierung fehlgeschlagen. Das Git-Zugriffstoken ist ungültig oder abgelaufen.",
    },
    "git.still_denied": {
        "en": "Invitation was accepted but access still denied. Please wait a moment and "
        "try again.",
        "de": "Die Einladung wurde angenommen, der Zugriff wird aber noch verweigert. Bitte "
        "einen Moment warten und erneut versuchen.",
    },
    "git.invitation_verified": {
        "en": "Repository invitation accepted and access verified successfully",
        "de": "Einladung zum Repository angenommen und Zugriff erfolgreich geprüft",
    },
    "git.access_failed": {
        "en": "Failed to access repository (HTTP {status}). Please check the repository "
        "URL and permissions.",
        "de": "Zugriff auf das Repository fehlgeschlagen (HTTP {status}). Bitte Repository-URL "
        "und Berechtigungen prüfen.",
    },
    "git.access_verified": {
        "en": "Repository access verified successfully",
        "de": "Repository-Zugriff erfolgreich geprüft",
    },
    "git.timeout": {
        "en": "Request timeout. The Git provider did not respond in time. Please try again.",
        "de": "Zeitüberschreitung. Der Git-Anbieter hat nicht rechtzeitig geantwortet. Bitte "
        "erneut versuchen.",
    },
    "git.connect_failed": {
        "en": "Failed to connect to Git provider: {error}",
        "de": "Verbindung zum Git-Anbieter fehlgeschlagen: {error}",
    },
    "common.unexpected_error": {
        "en": "Unexpected error: {error}",
        "de": "Unerwarteter Fehler: {error}",
    },
    # ------------------------------------------------------------------
    # Kurse und Teams
    # ------------------------------------------------------------------
    "course.not_found": {"en": "Course not found", "de": "Kurs nicht gefunden"},
    "course.not_member": {
        "en": "User is not a member of this course",
        "de": "Der Benutzer ist kein Mitglied dieses Kurses",
    },
    "course.not_teacher": {
        "en": "User is not a teacher of this course",
        "de": "Der Benutzer ist kein Lehrender dieses Kurses",
    },
    "team.not_found": {"en": "Team not found", "de": "Team nicht gefunden"},
    "team.member_exists": {
        "en": "User already in team or team not found",
        "de": "Der Benutzer ist bereits im Team oder das Team existiert nicht",
    },
    "team.member_missing": {
        "en": "User not in team or team not found",
        "de": "Der Benutzer ist nicht im Team oder das Team existiert nicht",
    },
    # ------------------------------------------------------------------
    # Deployments, Tasks, Dateien
    # ------------------------------------------------------------------
    "deployment.not_found": {
        "en": "Deployment not found",
        "de": "Deployment nicht gefunden",
    },
    "deployment.no_permission": {
        "en": "You don't have permission to access this deployment",
        "de": "Du hast keine Berechtigung für dieses Deployment",
    },
    "deployment.owner_or_staff_only": {
        "en": "Only the deployment owner or staff can perform this action",
        "de": "Nur der Besitzer des Deployments oder Mitarbeitende dürfen diese Aktion ausführen",
    },
    "deployment.delete_status": {
        "en": "Cannot delete a deployment in status '{status}'. Wait for the active task "
        "to finish.",
        "de": "Ein Deployment im Status '{status}' kann nicht gelöscht werden. Bitte warten, "
        "bis die laufende Aufgabe beendet ist.",
    },
    "deployment.active_task": {
        "en": "Deployment already has an active task",
        "de": "Für das Deployment läuft bereits eine Aufgabe",
    },
    "deployment.action_status": {
        "en": "Cannot {action} a deployment in status '{status}'. Required status: {required}.",
        "de": "Aktion '{action}' ist für ein Deployment im Status '{status}' nicht möglich. "
        "Erforderlicher Status: {required}.",
    },
    "deployment.dispatch_failed": {
        "en": "Could not dispatch {task} task — please retry",
        "de": "Die Aufgabe '{task}' konnte nicht gestartet werden — bitte erneut versuchen",
    },
    "deployment.resend_status": {
        "en": "Cannot resend access mail while deployment is '{status}'. Wait for the "
        "active task to finish.",
        "de": "Die Zugangsmail kann nicht erneut gesendet werden, solange das Deployment im "
        "Status '{status}' ist. Bitte warten, bis die laufende Aufgabe beendet ist.",
    },
    "deployment.resend_own_only": {
        "en": "Members may only resend their own access mail",
        "de": "Mitglieder dürfen nur ihre eigene Zugangsmail erneut senden",
    },
    "task.not_found": {"en": "Task not found", "de": "Aufgabe nicht gefunden"},
    "task.deployment_not_found": {
        "en": "Deployment for task not found",
        "de": "Deployment zur Aufgabe nicht gefunden",
    },
    "files.none": {"en": "No files", "de": "Keine Dateien"},
    "files.no_upload_for_var": {
        "en": "No uploaded file under variable '{variable}'",
        "de": "Keine hochgeladene Datei für die Variable '{variable}'",
    },
    "files.no_file_in_slot": {
        "en": "No file in slot '{slot}'",
        "de": "Keine Datei im Slot '{slot}'",
    },
    "files.bad_payload": {
        "en": "Stored file payload is not valid base64",
        "de": "Die gespeicherten Dateidaten sind kein gültiges Base64",
    },
    # ------------------------------------------------------------------
    # OpenStack
    # ------------------------------------------------------------------
    "openstack.no_credentials": {
        "en": "No OpenStack credentials configured",
        "de": "Keine OpenStack-Zugangsdaten hinterlegt",
    },
    "openstack.unknown_service": {
        "en": "Unknown service '{service}' (compute|network|volume)",
        "de": "Unbekannter Dienst '{service}' (compute|network|volume)",
    },
    # Validierung der OpenStack-Zugangsdaten (services/openstack_validator.py).
    # Der EN-Text wird in der DB abgelegt und erst in der Antwort übersetzt.
    "openstack.invalid_credentials": {
        "en": "Invalid credentials",
        "de": "Ungültige Zugangsdaten",
    },
    "openstack.project_not_found": {
        "en": "Project or domain not found",
        "de": "Projekt oder Domain nicht gefunden",
    },
    "openstack.request_rejected": {
        "en": "OpenStack rejected request (HTTP {status})",
        "de": "OpenStack hat die Anfrage abgelehnt (HTTP {status})",
    },
    "openstack.auth_url_unreachable": {
        "en": "Could not reach auth_url: {error}",
        "de": "auth_url nicht erreichbar: {error}",
    },
    "openstack.sdk_error": {
        "en": "OpenStack SDK error: {error_type}",
        "de": "OpenStack-SDK-Fehler: {error_type}",
    },
    "quota.fetch_failed": {
        "en": "Failed to fetch quotas",
        "de": "Die Kontingente konnten nicht geladen werden",
    },
    "quota.compute_fetch_failed": {
        "en": "Failed to fetch compute quotas",
        "de": "Die Compute-Kontingente konnten nicht geladen werden",
    },
    # clouds.yaml-Import (services/clouds_yaml_parser.py)
    "clouds.invalid_yaml": {"en": "Invalid YAML: {error}", "de": "Ungültiges YAML: {error}"},
    "clouds.no_mapping": {
        "en": "clouds.yaml must contain a top-level 'clouds:' mapping",
        "de": "clouds.yaml muss auf oberster Ebene ein 'clouds:'-Mapping enthalten",
    },
    "clouds.empty": {
        "en": "clouds.yaml has no clouds defined",
        "de": "In clouds.yaml sind keine Clouds definiert",
    },
    "clouds.multiple": {
        "en": "Multiple clouds in YAML; specify cloud_name (one of: {names})",
        "de": "Mehrere Clouds im YAML; cloud_name angeben (einer von: {names})",
    },
    "clouds.not_found": {
        "en": "Cloud '{cloud}' not found in YAML (available: {names})",
        "de": "Cloud '{cloud}' im YAML nicht gefunden (verfügbar: {names})",
    },
    "clouds.not_mapping": {
        "en": "Cloud '{cloud}' is not a mapping",
        "de": "Cloud '{cloud}' ist kein Mapping",
    },
    "clouds.no_auth": {
        "en": "Cloud '{cloud}' is missing 'auth:' block",
        "de": "Cloud '{cloud}' hat keinen 'auth:'-Block",
    },
    "clouds.bad_auth_type": {
        "en": "Unsupported auth_type '{auth_type}'. Supported: v3applicationcredential, password",
        "de": "Nicht unterstützter auth_type '{auth_type}'. Unterstützt: "
        "v3applicationcredential, password",
    },
    "clouds.no_app_credential": {
        "en": "Application credential requires application_credential_id and "
        "application_credential_secret",
        "de": "Application Credential benötigt application_credential_id und "
        "application_credential_secret",
    },
    "clouds.no_auth_url": {
        "en": "auth.auth_url is required",
        "de": "auth.auth_url ist erforderlich",
    },
    "clouds.no_password": {
        "en": "Password auth requires auth.username and auth.password",
        "de": "Passwort-Authentifizierung benötigt auth.username und auth.password",
    },
    # ------------------------------------------------------------------
    # Authentifizierung (Keycloak)
    # ------------------------------------------------------------------
    "auth.token_inactive": {
        "en": "Token is not active or has expired",
        "de": "Das Token ist nicht aktiv oder abgelaufen",
    },
    "auth.failed": {"en": "Authentication failed", "de": "Authentifizierung fehlgeschlagen"},
    "auth.credentials_invalid": {
        "en": "Could not validate credentials",
        "de": "Die Zugangsdaten konnten nicht validiert werden",
    },
    "auth.token_invalid": {"en": "Invalid token", "de": "Ungültiges Token"},
    "auth.token_validation_failed": {
        "en": "Token validation failed",
        "de": "Token-Validierung fehlgeschlagen",
    },
    "auth.keycloak_no_email": {
        "en": "Keycloak user has no email; refusing to provision.",
        "de": "Der Keycloak-Benutzer hat keine E-Mail-Adresse; er wird nicht angelegt.",
    },
    "auth.token_no_sub": {
        "en": "Token missing user ID (sub)",
        "de": "Im Token fehlt die Benutzer-ID (sub)",
    },
    "auth.keycloak_search_failed": {
        "en": "Failed to search Keycloak users",
        "de": "Die Keycloak-Benutzersuche ist fehlgeschlagen",
    },
    "auth.keycloak_fetch_failed": {
        "en": "Failed to fetch Keycloak users",
        "de": "Die Keycloak-Benutzer konnten nicht geladen werden",
    },
    # ------------------------------------------------------------------
    # LTI
    # ------------------------------------------------------------------
    "lti.missing_oidc_params": {
        "en": "Missing required OIDC login-initiation parameters",
        "de": "Erforderliche OIDC-Login-Parameter fehlen",
    },
    "lti.unknown_issuer": {
        "en": "Unknown LTI platform issuer",
        "de": "Unbekannter Aussteller (Issuer) der LTI-Plattform",
    },
    "lti.missing_id_token": {
        "en": "LTI launch missing id_token or state",
        "de": "Im LTI-Launch fehlt id_token oder state",
    },
    "lti.jwks_unreachable": {
        "en": "Could not reach the LTI platform's key endpoint to verify the launch",
        "de": "Der Schlüssel-Endpunkt der LTI-Plattform ist nicht erreichbar; der Launch "
        "kann nicht verifiziert werden",
    },
    "lti.malformed_launch": {
        "en": "Malformed LTI launch token: {error}",
        "de": "Fehlerhaftes LTI-Launch-Token: {error}",
    },
    "lti.invalid_launch": {
        "en": "Invalid LTI launch token: {error}",
        "de": "Ungültiges LTI-Launch-Token: {error}",
    },
    "lti.bad_state": {
        "en": "LTI launch state/nonce missing, unknown, or already used",
        "de": "LTI-Launch-State/Nonce fehlt, ist unbekannt oder wurde bereits verwendet",
    },
    "lti.deployment_mismatch": {
        "en": "LTI launch deployment_id does not match the registered tool deployment",
        "de": "Die deployment_id des LTI-Launch passt nicht zum registrierten Tool-Deployment",
    },
    "lti.no_email": {
        "en": "LTI launch has no email claim; refusing to provision.",
        "de": "Der LTI-Launch enthält keine E-Mail-Adresse; der Benutzer wird nicht angelegt.",
    },
    "lti.invalid_session": {
        "en": "Invalid LTI session token: {error}",
        "de": "Ungültiges LTI-Session-Token: {error}",
    },
    "lti.session_no_typ": {
        "en": "Invalid LTI session token: missing typ claim",
        "de": "Ungültiges LTI-Session-Token: typ-Claim fehlt",
    },
    "lti.not_session": {
        "en": "Not an LTI session token",
        "de": "Kein LTI-Session-Token",
    },
    "lti.session_user_gone": {
        "en": "LTI session refers to a user that no longer exists",
        "de": "Die LTI-Session verweist auf einen Benutzer, den es nicht mehr gibt",
    },
    # ------------------------------------------------------------------
    # E-Mails (services/deployment_notifier.py, templates/email/)
    # ------------------------------------------------------------------
    "mail.user_subject": {"en": "Your access details", "de": "Deine Zugangsdaten"},
    "mail.owner_subject": {"en": "Deployment summary", "de": "Deployment-Zusammenfassung"},
    "mail.footer": {
        "en": "Sent automatically by Click-n-Deploy. Do not reply.",
        "de": "Automatisch von Click-n-Deploy versendet. Bitte nicht antworten.",
    },
    # ------------------------------------------------------------------
    # Validierungsfehler von Pydantic (422), nach ``type`` zugeordnet in
    # ``app/i18n/validation.py``. EN gibt Pydantic selbst aus; die Texte hier
    # dokumentieren nur die Vorlage.
    # ------------------------------------------------------------------
    "validation.missing": {"en": "Field required", "de": "Pflichtfeld fehlt"},
    "validation.string_type": {
        "en": "Input should be a valid string",
        "de": "Eingabe muss ein gültiger Text sein",
    },
    "validation.string_too_short": {
        "en": "String should have at least {min_length} characters",
        "de": "Der Text muss mindestens {min_length} Zeichen lang sein",
    },
    "validation.string_too_long": {
        "en": "String should have at most {max_length} characters",
        "de": "Der Text darf höchstens {max_length} Zeichen lang sein",
    },
    "validation.string_pattern_mismatch": {
        "en": "String should match pattern '{pattern}'",
        "de": "Der Text muss dem Muster '{pattern}' entsprechen",
    },
    "validation.int_type": {
        "en": "Input should be a valid integer",
        "de": "Eingabe muss eine ganze Zahl sein",
    },
    "validation.int_parsing": {
        "en": "Input should be a valid integer, unable to parse string as an integer",
        "de": "Eingabe muss eine ganze Zahl sein; der Text lässt sich nicht als Zahl lesen",
    },
    "validation.float_type": {
        "en": "Input should be a valid number",
        "de": "Eingabe muss eine gültige Zahl sein",
    },
    "validation.float_parsing": {
        "en": "Input should be a valid number, unable to parse string as a number",
        "de": "Eingabe muss eine gültige Zahl sein; der Text lässt sich nicht als Zahl lesen",
    },
    "validation.bool_type": {
        "en": "Input should be a valid boolean",
        "de": "Eingabe muss ein Wahrheitswert (true/false) sein",
    },
    "validation.bool_parsing": {
        "en": "Input should be a valid boolean, unable to interpret input",
        "de": "Eingabe muss ein Wahrheitswert (true/false) sein; der Wert ist nicht lesbar",
    },
    "validation.uuid_type": {
        "en": "UUID input should be a string, bytes or UUID object",
        "de": "Die UUID muss als Text oder UUID-Objekt angegeben werden",
    },
    "validation.uuid_parsing": {
        "en": "Input should be a valid UUID",
        "de": "Eingabe muss eine gültige UUID sein",
    },
    "validation.list_type": {
        "en": "Input should be a valid list",
        "de": "Eingabe muss eine Liste sein",
    },
    "validation.dict_type": {
        "en": "Input should be a valid dictionary",
        "de": "Eingabe muss ein Objekt (Dictionary) sein",
    },
    "validation.model_type": {
        "en": "Input should be a valid dictionary or object",
        "de": "Eingabe muss ein gültiges Objekt sein",
    },
    "validation.json_invalid": {"en": "Invalid JSON", "de": "Ungültiges JSON"},
    "validation.extra_forbidden": {
        "en": "Extra inputs are not permitted",
        "de": "Zusätzliche Felder sind nicht erlaubt",
    },
    "validation.enum": {
        "en": "Input should be {expected}",
        "de": "Eingabe muss {expected} sein",
    },
    "validation.literal_error": {
        "en": "Input should be {expected}",
        "de": "Eingabe muss {expected} sein",
    },
    "validation.greater_than": {
        "en": "Input should be greater than {gt}",
        "de": "Der Wert muss größer als {gt} sein",
    },
    "validation.greater_than_equal": {
        "en": "Input should be greater than or equal to {ge}",
        "de": "Der Wert muss mindestens {ge} betragen",
    },
    "validation.less_than": {
        "en": "Input should be less than {lt}",
        "de": "Der Wert muss kleiner als {lt} sein",
    },
    "validation.less_than_equal": {
        "en": "Input should be less than or equal to {le}",
        "de": "Der Wert darf höchstens {le} betragen",
    },
    "validation.too_short": {
        "en": "List should have at least {min_length} items",
        "de": "Die Liste muss mindestens {min_length} Einträge enthalten",
    },
    "validation.too_long": {
        "en": "List should have at most {max_length} items",
        "de": "Die Liste darf höchstens {max_length} Einträge enthalten",
    },
    "validation.value_error": {"en": "Value error, {error}", "de": "Ungültiger Wert: {error}"},
    # ------------------------------------------------------------------
    # Task-Logs, die das Backend selbst schreibt (Reconciler, Celery-Events)
    # ------------------------------------------------------------------
    "task.dispatch_failed": {
        "en": "Failed to dispatch to Celery",
        "de": "Übergabe an Celery fehlgeschlagen",
    },
    "task.reconciler_no_dispatch": {
        "en": "Reconciler: Celery dispatch did not complete (no task id stamped)",
        "de": "Reconciler: Die Übergabe an Celery wurde nicht abgeschlossen "
        "(keine Task-ID gesetzt)",
    },
    "task.reconciler_stuck_pending": {
        "en": "Reconciler: task stuck in Celery PENDING beyond grace window",
        "de": "Reconciler: Aufgabe hängt länger als die Karenzzeit in Celery PENDING fest",
    },
    "task.reconciler_no_traceback": {
        "en": "Task failed (reconciler could not retrieve traceback)",
        "de": "Aufgabe fehlgeschlagen (der Reconciler konnte den Traceback nicht abrufen)",
    },
    "sse.overflow": {
        "en": "live stream backpressure: dropped older events",
        "de": "Live-Stream überlastet: ältere Ereignisse wurden verworfen",
    },
    "log.error_line": {"en": "Error: {error}", "de": "Fehler: {error}"},
    "log.commit_info": {
        "en": "\nCommit: {hash}\n   Message: {message}\n   Author: {author}",
        "de": "\nCommit: {hash}\n   Nachricht: {message}\n   Autor: {author}",
    },
    "celery.not_registered": {
        "en": "The worker did not recognise the task type — backend and worker are out "
        "of sync. Please inform the administrator; the deployment itself keeps running.",
        "de": "Worker hat den Task-Typ nicht erkannt — Backend und Worker sind "
        "nicht synchron. Bitte den Administrator informieren; das Deployment "
        "selbst läuft weiter.",
    },
    "celery.worker_lost": {
        "en": "The worker process was terminated during execution. The task may be "
        "restarted automatically — please check the status again shortly.",
        "de": "Der Worker-Prozess wurde während der Ausführung beendet. Der Task "
        "wird ggf. automatisch neu gestartet — bitte den Status in Kürze "
        "erneut prüfen.",
    },
    "celery.timeout": {
        "en": "The task exceeded its time limit. If this happens repeatedly, the "
        "OpenStack region is usually overloaded.",
        "de": "Der Task hat das Zeit-Limit überschritten. Wenn das wiederholt "
        "passiert, ist meist die OpenStack-Region überlastet.",
    },
    "celery.reject": {
        "en": "The worker rejected the task — probably a routing or configuration "
        "problem on the platform.",
        "de": "Der Worker hat den Task abgelehnt — wahrscheinlich ein "
        "Routing- oder Konfigurationsproblem auf der Plattform.",
    },
    "celery.content_disallowed": {
        "en": "The task could not be deserialised. This is a platform bug — please "
        "inform the administrator.",
        "de": "Der Task konnte nicht deserialisiert werden. Das ist ein "
        "Plattform-Bug — bitte den Administrator informieren.",
    },
    # ------------------------------------------------------------------
    # @openstack-Marker-Fehler (routers/apps.py, Autoren-Diagnose)
    # ------------------------------------------------------------------
    "marker.did_you_mean": {
        "en": "; did you mean '{suggestion}'?",
        "de": "; meintest du '{suggestion}'?",
    },
    "marker.invalid_var_scope": {
        "en": "invalid var_scope '{slot}'{hint} — expected {expected}",
        "de": "ungültiger var_scope '{slot}'{hint} — erwartet {expected}",
    },
    "marker.packer_scope_forbidden": {
        "en": "packer variables only support ``var_scope = all``; given: '{scope}'. Reason: Packer builds ONE image that is shared by all later VMs/teams/users — a per-team value would have no effect.",
        "de": "packer-Variablen unterstützen nur ``var_scope = all``; angegeben: '{scope}'. Begründung: Packer baut EIN Image, das von allen späteren VMs/Teams/Usern geteilt wird — ein Per-Team-Wert hätte keine Wirkung.",
    },
    "marker.too_many_segments": {
        "en": 'marker has too many segments — allowed: @openstack:<type>[:<mode>][:<multi>][:<var_scope>]',
        "de": 'marker hat zu viele Segmente — erlaubt: @openstack:<type>[:<mode>][:<multi>][:<var_scope>]',
    },
    "marker.unparseable": {
        "en": 'marker could not be parsed — only ``@openstack:<type>[:<mode>][:<multi>][:<var_scope>]`` is allowed, with colons as separators and no whitespace between the segments',
        "de": 'marker konnte nicht geparst werden — erlaubt ist nur ``@openstack:<type>[:<mode>][:<multi>][:<var_scope>]`` mit Doppelpunkten als Trenner und ohne Whitespace zwischen den Segmenten',
    },
    "marker.whitespace": {
        "en": 'marker contains whitespace between the segments — write it without spaces (e.g. ``@openstack:flavor:id:multi`` instead of ``@openstack:flavor :id :multi``)',
        "de": 'marker enthält Whitespace zwischen den Segmenten — schreibe ihn ohne Leerzeichen (z.B. ``@openstack:flavor:id:multi`` statt ``@openstack:flavor :id :multi``)',
    },
    "marker.extensions_comma": {
        "en": 'invalid extension filter with comma — marker slots are separated with ``|``, not with a comma (e.g. ``@openstack:file:all:pdf|docx`` instead of ``@openstack:file:all:pdf,docx``)',
        "de": 'ungültiger Endungsfilter mit Komma — marker-Slots werden mit ``|`` getrennt, nicht mit Komma (z.B. ``@openstack:file:all:pdf|docx`` statt ``@openstack:file:all:pdf,docx``)',
    },
    "marker.unknown_os_type": {
        "en": "unknown resource type '{raw_type}'{hint} — expected: {expected}",
        "de": "unbekannter resource-type '{raw_type}'{hint} — erwartet: {expected}",
    },
    "marker.empty_type_ambiguous": {
        "en": 'an empty type slot is only allowed in combination with ``var_scope`` (e.g. ``@openstack:::team``); several occupied slots are not permitted here',
        "de": 'leerer type-slot ist nur in Kombination mit ``var_scope`` erlaubt (z.B. ``@openstack:::team``); mehrere belegte Slots sind hier nicht zulässig',
    },
    "marker.empty": {
        "en": 'empty marker — specify either a resource type (e.g. ``@openstack:flavor``) or a var_scope (e.g. ``@openstack:::team``)',
        "de": 'leerer Marker — entweder einen resource-type angeben (z.B. ``@openstack:flavor``) oder einen var_scope (z.B. ``@openstack:::team``)',
    },
    "marker.file_packer_forbidden": {
        "en": '``@openstack:file`` is not supported in packer variables — files are only delivered on the OpenTofu path',
        "de": '``@openstack:file`` ist in Packer-Variablen nicht unterstützt — Dateien werden ausschließlich im OpenTofu-Pfad zugestellt',
    },
    "marker.invalid_file_scope": {
        "en": "invalid file scope '{raw_mode}'{hint} — expected {expected}",
        "de": "ungültiger file-scope '{raw_mode}'{hint} — erwartet {expected}",
    },
    "marker.file_extra_slot": {
        "en": "@openstack:file does not accept a fifth slot (given: '{raw_scope}') — the scope goes in the third slot (e.g. ``@openstack:file:user:pdf``)",
        "de": "@openstack:file akzeptiert keinen fünften Slot (angegeben: '{raw_scope}') — der Scope steht im dritten Slot (z.B. ``@openstack:file:user:pdf``)",
    },
    "marker.file_missing_extensions": {
        "en": '``@openstack:file`` needs an extension filter in the fourth slot, e.g. ``@openstack:file:all:pdf`` or ``@openstack:file:user:pdf|docx``',
        "de": '``@openstack:file`` braucht einen Endungsfilter im vierten Slot, z.B. ``@openstack:file:all:pdf`` oder ``@openstack:file:user:pdf|docx``',
    },
    "marker.file_invalid_extensions": {
        "en": "invalid extension filter '{exts}' — only alphanumeric extensions are allowed, several separated with '|' (e.g. ``pdf|docx``)",
        "de": "ungültiger Endungsfilter '{exts}' — erlaubt sind alphanumerische Endungen, mehrere getrennt mit '|' (z.B. ``pdf|docx``)",
    },
    "marker.multi_in_mode_slot": {
        "en": "'{raw_mode}' is a multi flag, not a mode — write the marker with an empty mode slot, e.g. ``@openstack:{os_type}::{flag}``",
        "de": "'{raw_mode}' ist ein multi-Flag, nicht ein Mode — schreibe den Marker mit leerem Mode-Slot, z.B. ``@openstack:{os_type}::{flag}``",
    },
    "marker.scope_in_mode_slot": {
        "en": "'{raw_mode}' is a var_scope, not a mode — write the marker with empty mode/multi slots, e.g. ``@openstack:{os_type}:::{scope}``",
        "de": "'{raw_mode}' ist ein var_scope, nicht ein Mode — schreibe den Marker mit leerem Mode-/Multi-Slot, z.B. ``@openstack:{os_type}:::{scope}``",
    },
    "marker.invalid_mode": {
        "en": "invalid mode '{raw_mode}'{hint} — expected 'id' or 'name'",
        "de": "ungültiger mode '{raw_mode}'{hint} — erwartet 'id' oder 'name'",
    },
    "marker.invalid_multi": {
        "en": "invalid multi flag '{raw_multi}'{hint} — expected 'multi', 'list' or 'single'",
        "de": "ungültiger multi-Flag '{raw_multi}'{hint} — erwartet 'multi', 'list' oder 'single'",
    },
    "marker.multi_type_conflict": {
        "en": "marker declares ':multi', but the HCL type is '{var_type}' — only ``string``, ``list(...)``, ``set(...)`` and ``tuple(...)`` are allowed",
        "de": "marker deklariert ':multi', aber HCL-Type ist '{var_type}' — erlaubt sind nur ``string``, ``list(...)``, ``set(...)`` und ``tuple(...)``",
    },
    "marker.single_type_conflict": {
        "en": "marker declares ':single', but the HCL type is '{var_type}' (a list/set/tuple collection) — fix one of the two",
        "de": "marker deklariert ':single', aber HCL-Type ist '{var_type}' (eine list/set/tuple-Kollektion) — fixe einen der beiden",
    },
    "marker.file_type_shape_all": {
        "en": "marker ``@openstack:file:all`` expects HCL type ``map(object({{name=string, content_b64=string, size=number, content_type=string}}))`` — given: '{var_type}'",
        "de": "marker ``@openstack:file:all`` erwartet HCL-Type ``map(object({{name=string, content_b64=string, size=number, content_type=string}}))`` — angegeben: '{var_type}'",
    },
    "marker.file_type_shape_scoped": {
        "en": "marker ``@openstack:file:{scope}`` expects HCL type ``map(map(object({{name=string, content_b64=string, size=number, content_type=string}})))`` — given: '{var_type}'",
        "de": "marker ``@openstack:file:{scope}`` erwartet HCL-Type ``map(map(object({{name=string, content_b64=string, size=number, content_type=string}})))`` — angegeben: '{var_type}'",
    },
    "marker.scoped_requires_map": {
        "en": "marker declares ``var_scope = {scope}``, but the HCL type is '{var_type}'. For each scope entry the wizard supplies a map (slot_key → value), so the HCL type must be ``map(...)`` — e.g. ``map(string)`` or ``map(list(string))``.",
        "de": "marker deklariert ``var_scope = {scope}``, aber HCL-Type ist '{var_type}'. Pro Scope-Eintrag liefert der Wizard eine Map (slot_key → value), also muss der HCL-Type ``map(...)`` sein — z.B. ``map(string)`` oder ``map(list(string))``.",
    },
}
