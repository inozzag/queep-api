import pytest
from pydantic import ValidationError

from queep.config import Settings


def test_production_rejects_emulators(monkeypatch):
    monkeypatch.setenv("QUEEP_ENV", "production")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "queep-production")
    with pytest.raises(ValidationError):
        Settings()


def test_local_cannot_point_to_real_project(monkeypatch):
    monkeypatch.setenv("QUEEP_ENV", "local")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "queep-production")
    with pytest.raises(ValidationError):
        Settings()


def test_local_requires_both_emulators(monkeypatch):
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_cloud_deletion_is_independent_of_vendor_configuration(monkeypatch):
    monkeypatch.setenv("QUEEP_ENV", "production")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "queep-dev-hstlx")
    monkeypatch.setenv("QUEEP_ACCOUNT_DELETION_ENABLED", "true")
    for key in ("REVENUECAT_PROJECT_ID", "REVENUECAT_DELETE_KEY", "FIREBASE_AUTH_EMULATOR_HOST",
                "FIRESTORE_EMULATOR_HOST", "FIREBASE_STORAGE_EMULATOR_HOST",
                "STORAGE_EMULATOR_HOST"):
        monkeypatch.delenv(key, raising=False)
    assert Settings().account_deletion_enabled
