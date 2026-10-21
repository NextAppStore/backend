#!/usr/bin/env python3
"""Put back an OpenTofu state the worker rescued but could not deliver.

If the API was unreachable at the end of an apply for longer than the
worker kept retrying, the worker stores the state encrypted under
``WORKER_STATE_RESCUE_DIR`` (a volume) and logs the file name in the task
log. Pipe it from the worker into the backend (``-`` reads stdin), e.g.
``make prod-restore-tfstate``:

    docker exec worker-prod cat <file> | \
        docker exec -i backend-prod python scripts/restore_tfstate.py <deployment-id> - [--force]

Refuses to overwrite a stored state of another lineage or a same/newer
serial unless ``--force``; always refuses while a task holds the lock.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import SessionLocal  # noqa: E402
from app.services import tf_state_store  # noqa: E402
from app.utils.crypto import decrypt  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("deployment_id", type=uuid.UUID)
    parser.add_argument("file", help="the rescued .tfstate.enc file from the worker, or - for stdin")
    parser.add_argument("--force", action="store_true", help="overwrite even a same/newer or foreign state")
    args = parser.parse_args()

    if args.file == "-":
        rescued = decrypt(sys.stdin.buffer.read())
    else:
        with open(args.file, "rb") as f:
            rescued = decrypt(f.read())
    with SessionLocal() as db:
        try:
            print(tf_state_store.restore(db, args.deployment_id, rescued, force=args.force))
        except tf_state_store.RestoreRefused as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
