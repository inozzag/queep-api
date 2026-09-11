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


def test_cloud_deletion_requires_vendor_cleanup_configuration(monkeypatch):
    monkeypatch.setenv("QUEEP_ENV", "production")
    monkeypatch.setenv("QUEEP_ACCOUNT_DELETION_ENABLED", "true")
    monkeypatch.delenv("REVENUECAT_PROJECT_ID", raising=False)
    monkeypatch.delenv("REVENUECAT_DELETE_KEY", raising=False)
    with pytest.raises(ValueError, match="RevenueCat cleanup"):
        Settings()
