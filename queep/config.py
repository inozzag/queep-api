import os
from functools import lru_cache
from typing import Literal

import firebase_admin
from firebase_admin import credentials, firestore, storage
from google.auth.credentials import AnonymousCredentials
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    environment: Literal["local", "production"] = Field("production", alias="QUEEP_ENV")
    project: str = Field(alias="GOOGLE_CLOUD_PROJECT")
    bucket: str = Field(alias="QUEEP_STORAGE_BUCKET")

    @model_validator(mode="after")
    def validate_emulators(self):
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
