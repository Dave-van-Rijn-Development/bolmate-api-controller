from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, cast
from uuid import UUID

from dvrd_pydate import PyDateTime

from bolmate_api_controller.bolmate_encryption import enc_data
from bolmate_api_controller.constants import BEARER_REFRESH_MARGIN

__all__ = ('ID', 'OAuthType', 'APIKey', 'OAuthClientCredentials', 'AuthRequestData', 'Result', 'ErrorCode')

type ID = str | UUID
type OAuthType = Literal['INVOICES', 'DATA_INSIGHTS']


class ErrorCode(StrEnum):
    REFRESH_EXPIRED = 'Refresh token expired'
    API_KEY_NOT_FOUND = 'API key not found in the database'
    API_KEY_DEACTIVATED = 'API key is deactivated'
    MISSING_LEGACY_CREDENTIALS = 'Missing legacy credentials'
    EXCEPTION_RAISED = 'Exception raised'
    MAX_ATTEMPTS = 'Max attempts reached'
    TOO_MANY_REQUESTS = 'Too many requests'
    SERVER_ERROR = 'Server error'
    UNEXPECTED_RESPONSE = 'Unexpected response'


@dataclass
class APIKey:
    api_key_id: ID
    api_bearer: str
    bearer_expires_at: PyDateTime | None
    refresh_token: str | None
    refresh_token_expires_at: PyDateTime | None
    oauth_type: OAuthType
    use_oauth: bool
    status: str  # For possible values see the ApiKey model in Bolmate

    # Legacy keys
    key: str | None
    secret: str | None

    @classmethod
    def from_dict(cls, *, data: dict):
        expires_at: str | datetime | None = data.get('bearer_expires_at')
        refresh_expires_at: str | datetime | None = data.get('oauth_refresh_token_expires_at')
        refresh_token = data.get('oauth_refresh_token')
        key: str | None = data.get('key')
        secret: str | None = data.get('secret')
        return cls(
            api_key_id=cast(ID, data.get('id')),
            api_bearer=enc_data.decrypt(encrypted_text=cast(str, data.get('api_bearer'))),
            bearer_expires_at=PyDateTime(expires_at) if expires_at else None,
            refresh_token=enc_data.decrypt(encrypted_text=refresh_token) if refresh_token else None,
            refresh_token_expires_at=PyDateTime(refresh_expires_at) if refresh_expires_at else None,
            oauth_type=cast(OAuthType, data.get('oauth_type')),
            use_oauth=cast(bool, data.get('use_oauth')),
            key=enc_data.decrypt(encrypted_text=key) if key else None,
            secret=enc_data.decrypt(encrypted_text=secret) if secret else None,
            status=data.get('status'),
        )

    @property
    def bearer_is_expired(self):
        current_time = PyDateTime()
        return (
                not self.api_bearer or not self.bearer_expires_at
                or current_time >= self.bearer_expires_at.subtract_seconds(BEARER_REFRESH_MARGIN)
        )


@dataclass
class OAuthClientCredentials:
    client_id: str
    client_secret: str
    oauth_type: OAuthType


@dataclass
class AuthRequestData:
    url: str
    data: dict
    headers: dict
    timeout: int


@dataclass
class Result:
    api_key: APIKey | None
    success: bool
    exception: Exception | None = None
    error: ErrorCode | None = None
    response_status: int | None = None
    response_text: str | None = None
    retryable: bool = False

    @property
    def is_json_response(self) -> bool:
        text = self.response_text.strip() if self.response_text else ''
        if not text:
            return False
        if text.startswith('{'):
            return text.endswith('}')
        elif text.startswith('['):
            return text.endswith(']')
        return False
