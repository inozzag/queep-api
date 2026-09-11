import os
from functools import lru_cache
from typing import Literal

import firebase_admin
from firebase_admin import credentials, firestore, storage
from google.auth.credentials import AnonymousCredentials
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    environment: Literal["local", "production"] = Field("production", alias="QUEEP_ENV")
    project: str = Field(alias="GOOGLE_CLOUD_PROJECT")
    bucket: str = Field(alias="QUEEP_STORAGE_BUCKET")
    ai_enabled: bool = Field(False, alias="QUEEP_AI_ENABLED")
    openrouter_key: SecretStr | None = Field(None, alias="OPENROUTER_API_KEY")
    ai_daily_budget: float = Field(1.0, ge=0.01, le=100, alias="QUEEP_AI_DAILY_BUDGET_USD")
    queue: str = Field("queep-references", alias="QUEEP_TASK_QUEUE")
    region: str = Field("southamerica-east1", alias="QUEEP_REGION")
    worker_url: str = Field("", alias="QUEEP_WORKER_URL")
    task_identity: str = Field("", alias="QUEEP_TASK_IDENTITY")

    @model_validator(mode="after")
    def validate_emulators(self):
        if self.ai_enabled and not self.openrouter_key:
            raise ValueError("AI requires a backend OpenRouter key")
        hosts = [os.getenv(k) for k in
                 ("FIREBASE_AUTH_EMULATOR_HOST", "FIRESTORE_EMULATOR_HOST",
                  "FIREBASE_STORAGE_EMULATOR_HOST")]
        if self.environment == "local":
            if (not self.project.startswith("demo-") or not all(hosts)
                    or not self.bucket.startswith(self.project + ".")
                    or os.getenv("STORAGE_EMULATOR_HOST") != f"http://{hosts[2]}"):
                raise ValueError("Local mode requires a demo- project, bucket and all emulators")
        elif (any(hosts) or os.getenv("STORAGE_EMULATOR_HOST")
              or self.project.startswith("demo-")):
            raise ValueError("Emulators are forbidden in production")
        return self


class EmulatorCredential(credentials.Base):
    def get_credential(self):
        return AnonymousCredentials()


@lru_cache
def firebase_app():
    settings = Settings()
    cred = EmulatorCredential() if settings.environment == "local" else None
    return firebase_admin.initialize_app(
        cred, {"projectId": settings.project, "storageBucket": settings.bucket}, name="queep",
    )


@lru_cache
def database():
    return firestore.client(firebase_app())


@lru_cache
def image_bucket():
    return storage.bucket(app=firebase_app())
