import base64
import json
import os
import sys
from pathlib import Path

# Sicherstellen, dass das Backend-Verzeichnis im PYTHONPATH liegt
backend_dir = Path(__file__).resolve().parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

# Fernet-kompatibler 32-Byte Base64 Dummy-Key
dummy_fernet_key = base64.urlsafe_b64encode(b"0" * 32).decode()

# Erforderliche Umgebungsvariablen für Pydantic Settings
os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
os.environ.setdefault("SECRET_KEY", "export-secret-key-placeholder")
os.environ.setdefault("KEYCLOAK_SERVER_URL", "http://localhost:8080")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", dummy_fernet_key)

try:
    from app.main import app

    schema = app.openapi()
    target_path = backend_dir / "openapi.json"
    target_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    print(f"Erfolg: {len(schema.get('paths', {}))} Pfade nach {target_path} exportiert.")
except Exception as err:
    print(f"Export fehlgeschlagen: {err}", file=sys.stderr)
    sys.exit(1)
