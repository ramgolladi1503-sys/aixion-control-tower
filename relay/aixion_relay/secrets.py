from __future__ import annotations

import os

import keyring
from keyring.errors import KeyringError

SERVICE_NAME = "com.aixion.agent-relay"


class SecretStoreError(RuntimeError):
    pass


class SecretStore:
    """OS-backed relay secret storage.

    The relay token is never written to the JSON configuration. The environment override
    exists for containerized deployments where the secret is mounted by an orchestrator.
    """

    def get(self, name: str) -> str:
        environment_name = "AIXION_RELAY_TOKEN"
        environment_value = os.getenv(environment_name, "").strip()
        if environment_value:
            return environment_value
        try:
            value = keyring.get_password(SERVICE_NAME, name)
        except KeyringError as error:
            raise SecretStoreError(
                "Unable to read the relay token from the operating-system keychain."
            ) from error
        if not value:
            raise SecretStoreError(
                f"Relay secret {name!r} is missing. Register the relay again or set "
                f"{environment_name} through a secret manager."
            )
        return value

    def set(self, name: str, value: str) -> None:
        if not value:
            raise SecretStoreError("Refusing to store an empty relay token.")
        try:
            keyring.set_password(SERVICE_NAME, name, value)
        except KeyringError as error:
            raise SecretStoreError(
                "Unable to store the relay token in the operating-system keychain."
            ) from error

    def delete(self, name: str) -> None:
        try:
            keyring.delete_password(SERVICE_NAME, name)
        except keyring.errors.PasswordDeleteError:
            return
        except KeyringError as error:
            raise SecretStoreError(
                "Unable to delete the relay token from the operating-system keychain."
            ) from error
