import os
from functools import lru_cache
from typing import Literal

import firebase_admin
from firebase_admin import credentials, firestore
from google.auth.credentials import AnonymousCredentials
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    environment: Literal["local", "production"] = Field("production", alias="QUEEP_ENV")
    project: str = Field(alias="GOOGLE_CLOUD_PROJECT")

    @model_validator(mode="after")
    def validate_emulators(self):
        hosts = [os.getenv(k) for k in
                 ("FIREBASE_AUTH_EMULATOR_HOST", "FIRESTORE_EMULATOR_HOST")]
        if self.environment == "local":
            if not self.project.startswith("demo-") or not all(hosts):
                raise ValueError("Local mode requires a demo- project and both emulators")
        elif any(hosts) or self.project.startswith("demo-"):
            raise ValueError("Emulators are forbidden in production")
        return self


class EmulatorCredential(credentials.Base):
    def get_credential(self):
        return AnonymousCredentials()


@lru_cache
def firebase_app():
    settings = Settings()
    cred = EmulatorCredential() if settings.environment == "local" else None
    return firebase_admin.initialize_app(cred, {"projectId": settings.project}, name="queep")


@lru_cache
def database():
    return firestore.client(firebase_app())
