"""
core/clients.py - the one Firestore client every module shares (Oct 23,
cold-start work for regulator-dev at zero warm instances).

Until now audit.py, deps.py, tenancy.py and users.py each created their own
client when imported: four credential look-ups while the server starts, and
four separate connections to Firestore, each opened (with its own TLS
handshake) by the first request that happened to use that module. The first
Dashboard load after a cold start paid for two of them. One shared client
does the look-up once and opens one connection.

Modules still expose it as `db` (`from core.tenancy import db` keeps working
in tests and tools, and tests can still monkeypatch `<module>.db`).
Google's clients are safe to share across threads.
"""
from google.cloud import firestore

from core.config import GCP_PROJECT

db = firestore.Client(project=GCP_PROJECT)  # OPEN-11: per environment (core/config.py)
