# Backend-Refactoring — Bericht

**Branch:** `refactor/backend-structure` (9 Commits, Basis `7b7690c`)
**Umfang:** 22 Dateien, +3170 / −2543 Zeilen
**Verhalten:** unverändert — 659 Tests grün, OpenAPI-Schema byte-identisch

---

## 1. Warum überhaupt

Das Backend funktionierte. Refactoring „einfach so" ist laut dem
verwendeten Skill ausdrücklich kein Grund. Der Anlass war ein konkretes,
messbares Problem:

**Zwei Router-Dateien hatten den Großteil der Geschäftslogik
aufgesaugt.** `routers/apps.py` war 1748 Zeilen lang, davon 1188 ein
reiner HCL-Parser ohne jeden HTTP-Bezug. `routers/deployments.py` hatte
2133 Zeilen mit vier unabhängigen Verantwortlichkeiten.

Das ist nicht nur unschön, es hatte bereits eine sichtbare Folge: zwei
andere Router mussten Funktionen **aus einem Router importieren**, einer
davon funktions-lokal mit dem Kommentar `# local: avoid import cycle`.
Ein Import-Zyklus-Workaround ist das Symptom einer falschen Schichtung —
wenn er auftaucht, ist die Frage nicht „wie umgehe ich ihn", sondern
„welche Schicht fehlt".

Dazu kamen drei weitere messbare Befunde:

| Befund | Ausgangslage |
|---|---|
| Längste Funktion | 191 Zeilen, zyklomatische Komplexität 36 |
| Funktionen > 100 Zeilen | 12 |
| Identische Präambel in `deployments.py` | 11× „laden → 404 → Access-Guard" |
| Identisches Endpoint-Gerüst in `openstack_resources.py` | 10× |

Der elffach kopierte Access-Guard war dabei der einzige Befund mit
Sicherheitsrelevanz: eine Prüfung, die man beim Anlegen eines neuen
Endpoints schlicht vergessen kann.

---

## 2. Wie — die Methode

Die eigentliche Arbeit steckte nicht im Umschreiben, sondern im
Beweisen, dass sich nichts geändert hat. Das Vorgehen:

### 2.1 Zwei Sicherheitsnetze vor der ersten Änderung

**Netz 1 — die Testsuite.** Erst wurde die Baseline hergestellt:
Dev-Container hochgefahren, `make test-backend-isolated` ausgeführt,
Ergebnis festgehalten (635 passed / 4 skipped, 77,02 % Coverage). Ohne
grüne Baseline wäre abgebrochen worden.

**Netz 2 — das OpenAPI-Schema.** Das Schema wurde als JSON exportiert
(6311 Zeilen, `sort_keys=True`) und nach jeder Phase neu erzeugt und
gegen die Baseline gediffed. Das fängt eine Klasse von Fehlern ab, die
Tests übersehen: geänderte Pfade, verschobene Parameter, veränderte
Response-Modelle, umbenannte Tags.

Beide Netze liefen als ein Skript nach **jeder** Phase.

### 2.2 Der Ablauf pro Phase

Immer dieselben fünf Schritte: umbauen → `ruff` → Tests → OpenAPI-Diff →
Commit. Neun Commits, jeder einzeln lauffähig und einzeln
zurücknehmbar.

### 2.3 Verbatim schneiden statt neu tippen

Die großen Extraktionen (Phase 1 und 2) waren **reine Moves**. Der Code
wurde zeilenweise per Skript aus der Quelldatei geschnitten und
unverändert in die Zieldatei geschrieben — nicht abgetippt, nicht
„nebenbei verbessert". Umbenennungen wurden anschließend mit
wortgrenzen-verankerten Regexen angewandt (`\b_parse_marker\b`, damit
`_parse_marker_mode` nicht mitgefangen wird).

Verbessert wurde erst in einer **separaten** Phase mit eigenem Commit.
Der Skill nennt das die wichtigste Regel: *One thing at a time.*

### 2.4 Suchen-und-Ersetzen mit Sicherung

Jede skriptgesteuerte Ersetzung lief über einen Helfer, der zwei Dinge
prüft: dass das Suchmuster existiert, und dass es **genau einmal**
existiert. Bei Verletzung bricht das Skript ab, bevor es schreibt.

Das hat sich ausgezahlt: beim Umstellen der Endpoint-Signaturen schlug
eine Zusicherung fehl, das Skript brach vor dem Schreiben ab, und die
Datei blieb unversehrt. Eine Ersetzung, bei der die Zusicherung
vergessen wurde, ging prompt still daneben und fiel erst dem Linter auf.

---

## 3. Was — die neun Commits

### Phase 1 — HCL-Parser aus dem Router lösen
`app/routers/apps.py`: **1748 → 470 Zeilen**

Neu entstanden:

| Modul | Inhalt |
|---|---|
| `services/hcl/markers.py` | `@openstack`-Marker-Grammatik |
| `services/hcl/variables.py` | `variable {...}`-Blöcke → Wizard-Dicts |
| `services/hcl/packer.py` | Packer-Template-Discovery |
| `services/app_variables.py` | Git-Clone + Parser + HTTP-Mapping |

`serialize_app` wanderte nach `utils/app_image.py`, zu den anderen
Image-Helfern, die es ohnehin schon benutzte. Danach: **keine
Router→Router-Importe mehr.**

Die Schichtung ist jetzt explizit: `services/hcl/` kennt weder FastAPI
noch die Datenbank. `services/app_variables.py` ist die Grenze, an der
HTTP dazukommt. Darüber liegen die Router.

### Phase 2 — `deployments.py` aufteilen
`app/routers/deployments.py`: **2133 → 1453 Zeilen**

* `services/deployment_input.py` — Datei-Upload-Merge und
  `varScope`-Validierung
* `routers/deployments_stream.py` — der SSE-Live-Stream

Der Stream teilt mit den CRUD-Handlern nichts: async statt sync,
minutenlang offene Verbindungen, eigenes Vokabular. Er wird als
**Sub-Router** eingehängt statt in `main.py` registriert — so bleiben
Pfad und OpenAPI-Tag garantiert identisch.

### Phasen 3a–3d — die langen Funktionen

| Funktion | vorher | nachher |
|---|---|---|
| `validate_scoped_user_input` | 191 LOC / CX 36 | 37 LOC / CX 5 |
| `create_deployment` | 176 LOC / CX 19 | 72 LOC / CX 1 |
| `attach_files_to_user_input` | 171 LOC / CX 23 | 74 LOC / CX 9 |
| `_coerce_hcl_default` | 73 LOC / CX 20 | 35 LOC / CX 10 |
| `get_dashboard_stats` | 117 LOC / CX 9 | 12 LOC / CX 1 |

Die Muster dahinter:

* **`_reject()`** bündelt acht fast identische
  `raise HTTPException(..., detail={"reason": ...})`-Blöcke. Als
  `NoReturn` deklariert, damit die Aufrufstellen als Guard-Clauses
  lesbar sind.
* **Closures zu benannten Objekten.** `validate_scoped_user_input` hatte
  zwei Closures und ein Set mitten im Rumpf. Die drei gehören
  zusammen — sie beantworten dieselbe Frage — und heißen jetzt
  `_SlotRoster`.
* **Eine Prüfung pro Funktion**, in der Reihenfolge, die die
  Fehlermeldungen voraussetzen. Die Prüfreihenfolge ist Teil des
  Vertrags mit dem Frontend, nicht Zufall.
* **`_deployment_response`** ersetzte die zweite Kopie derselben
  Response-Projektion in `list_deployments`.

### Phase 4 — Guard in die Signatur ziehen

`app/routers/dependencies.py` macht aus der elffach kopierten Präambel
eine FastAPI-Dependency. Eine Factory baut aus einem Guard eine
Dependency; die sechs benutzten Kombinationen sind benannt exportiert.

Der eigentliche Gewinn ist nicht die Zeilenersparnis, sondern dass ein
Endpoint jetzt **deklarieren muss**, welchen Guard er will. Vergessen
geht nicht mehr still.

Analog, aber flacher (die Folge-Guards sind zu verschieden für eine
Factory): `_require_course` für 6 Stellen, `_require_app` für 5. Die
drei 404-Texte stehen jetzt einmal als Konstanten — sie sind Teil der
API-Oberfläche, das Frontend matcht darauf.

### Phase 5 — `openstack_resources.py`

Zehn Endpoints teilten sich dasselbe Gerüst: `fetch()`-Closure,
Connection öffnen, SDK-Collection durchlaufen, Objekt auf flaches Dict
projizieren. `_listing()` hält das Gerüst jetzt einmal; jeder Endpoint
liefert nur noch `source` und `row`.

**Bewusst *nicht* tabellengetrieben**, obwohl das im Plan stand: drei
Endpoints haben echte Query-Parameter-Logik. Eine Tabelle, die auch die
abbildet, wäre komplizierter geworden als der Code, den sie ersetzt. Die
Endpoints bleiben außerdem echte Funktionen mit eigenem Docstring — den
veröffentlicht FastAPI als API-Beschreibung.

**Diese Phase war die einzige, in der „Tests grün" nicht ausreichte** —
siehe Abschnitt 4.

### Phase 6 — Aufräumen

* Ein `if role == ADMIN:` mit neun Zeilen Kommentar und `pass` als
  einzigem Statement. Der Kommentar beschrieb korrekt, was die Funktion
  weiter unten tut — er steht jetzt dort, ohne den leeren Branch.
* Ein Docstring behauptete einen Lazy-Import von `difflib`, der seit
  jeher ganz oben stand.
* Zwei verirrte deutsche Kommentare übersetzt. Die deutschen
  MarkerError-*Meldungen* bleiben — die sieht der App-Autor im UI.

---

## 4. Wo „Tests grün" nicht genug war

In Phase 5 zeigte die Coverage, dass die Suite nur **3 der 10**
OpenStack-Endpoints anfährt. Für die anderen sieben war „659 passed"
schlicht kein Beleg — die Tests kamen an dem Code gar nicht vorbei.

Zwei zusätzliche Schritte:

1. **Maschineller Feld-Diff.** Ein Skript parste die alte und die neue
   Datei per `ast`, sammelte alle Dict-Literale je Endpoint und
   verglich die Key-Sets. Alle zehn identisch.

2. **24 Charakterisierungstests** (`tests/unit/test_openstack_row_shapes.py`)
   nageln die Wire-Shape fest: Feldnamen, Defaults bei fehlenden
   SDK-Attributen, und die beiden Felder, die unter zwei SDK-Namen
   auftreten können (`router:external`, `zoneState`).

Das ging überhaupt erst dadurch, dass die Projektionen keine Closures
mehr sind — der Refactor hat die Testbarkeit erzeugt, die ihn absichert.
Coverage dieser Datei: 56,6 % → 79,6 %.

---

## 5. Was bewusst nicht angefasst wurde

* **`utils/capabilities.py` und `utils/permissions.py`** — saubere
  `can_*`/`ensure_*`-Trennung, kein Grund.
* **`_commit_and_dispatch` / `_dispatch_lifecycle_task`** — bereits gut
  faktorisiert.
* **`services/deployment_status.py`, `tf_state_parser.py`, `models.py`,
  `schemas.py`** — unauffällig.
* **Der funktions-lokale Import in `users.py`** ist **tragend**, nicht
  Nachlässigkeit: ein Top-Level-Import würde den Namen beim Modul-Import
  binden und `patch("app.utils.keycloak_auth.sync_user_from_keycloak")`
  zu einem No-Op machen — genau darauf zielt `test_users_api.py`. Steht
  jetzt als Kommentar daneben, damit es niemand „aufräumt".
* **`NAME_ONLY_TYPES`** wird nirgends im Code gelesen, nur in zwei
  Docstrings referenziert. Als Dokumentation der Default-Entscheidung
  stehengelassen.

---

## 6. Ein Befund, der kein Refactoring ist

`_list_course_scope_deployments` nimmt `status_filter` entgegen, wendet
es aber nur im Admin-Fallback an. Für Lehrende wird
`?scope=course&status_filter=...` **still ignoriert**. Der Kommentar im
Code gibt das zu.

Das ist ein Verhaltensproblem, kein Strukturproblem — es zu beheben wäre
eine Verhaltensänderung und hätte in keinen dieser Commits gehört.
**Unverändert gelassen, hiermit gemeldet.**

---

## 7. Ergebnis

| Metrik | vorher | nachher |
|---|---:|---:|
| `routers/apps.py` | 1748 | **470** |
| `routers/deployments.py` | 2133 | **1453** |
| Längste Funktion | 191 LOC / CX 36 | **115 LOC / CX 8** |
| Funktionen > 100 LOC | 12 | **7** |
| Funktionen mit CX > 15 | 7 | **3** |
| Router→Router-Importe | 3 | **0** |
| Tests | 635 | **659** |
| Coverage | 77,02 % | **78,52 %** |
| OpenAPI-Schema | — | **byte-identisch** |

Die Gesamtzeilenzahl in `app/` ist von 13 464 auf 13 869 **gestiegen**.
Das ist kein Versehen: Funktionen aufteilen kostet Signaturen und
Docstrings. Weniger Zeilen war nie das Ziel — kürzere Funktionen,
klarere Schichten und ein Access-Guard, den man nicht vergessen kann,
schon.

---

## Reproduktion

```bash
# Baseline-Umgebung
docker compose -f deployment/docker-compose.dev.yml up -d postgres postgres-test redis backend

# Tests
cd backend && make test-backend-isolated

# Linter
docker compose -f ../deployment/docker-compose.dev.yml exec -T backend poetry run ruff check app/ tests/

# OpenAPI-Schema exportieren (für den Diff gegen main)
docker compose -f ../deployment/docker-compose.dev.yml exec -T \
  -e DISABLE_BACKGROUND_TASKS=1 backend poetry run python -c \
  "import json; from app.main import app; print(json.dumps(app.openapi(), indent=2, sort_keys=True))"
```
