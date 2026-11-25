"""Shared test setup: point the app at a throwaway SQLite database before import."""

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_tmpdir = tempfile.mkdtemp(prefix='shore-tests-')
os.environ['DATABASE_URL'] = f"sqlite:///{_tmpdir}/test.db"
os.environ.setdefault('REDIS_URL', 'redis://localhost:1/0')
os.environ['SEED_DEMO'] = 'false'
os.environ['ARTIFACTS_DIR'] = f'{_tmpdir}/artifacts'

import pytest  # noqa: E402

import src.models.database as database  # noqa: E402


class _Cache:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


database.cache = _Cache()
import src.middlewares.http as middleware  # noqa: E402

middleware.cache = database.cache

from fastapi.testclient import TestClient  # noqa: E402

from src.models.tables import metadata  # noqa: E402
from scripts.seed_demo import seed  # noqa: E402


@pytest.fixture()
def client():
    metadata.drop_all(database.engine)
    metadata.create_all(database.engine)
    seed()
    from src.main import app
    return TestClient(app)
