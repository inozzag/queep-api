from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from queep.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as value:
        yield value


@pytest.fixture
def account():
    response = httpx.post(
        "http://127.0.0.1:9099/identitytoolkit.googleapis.com/v1/accounts:signUp?key=local",
        json={"email": f"{uuid4()}@example.test", "password": "queep-testing-123",
              "returnSecureToken": True},
    )
    response.raise_for_status()
    data = response.json()
    return data["localId"], {"Authorization": f"Bearer {data['idToken']}"}

