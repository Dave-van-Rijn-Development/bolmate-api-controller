from random import random
from threading import Semaphore
from time import sleep
from typing import Literal

import requests
from dvrd_pydate import PyDateTime

from bolmate_api_controller.aio_bol_ratelimit import acquire_global_sync, bearer_limiter
from bolmate_api_controller.bolmate_encryption import enc_data
from bolmate_api_controller.constants import MAX_REQUEST_ATTEMPTS, SELECT_QUERY, UPDATE_QUERY, DEACTIVATE_QUERY, \
    ACCOUNT_DEACTIVATED
from bolmate_api_controller.database import sync_bolmate_connection
from bolmate_api_controller.logger import auth_exception, auth_error
from bolmate_api_controller.request_helper import client_credentials_from_oauth_type, build_oauth_request_data, \
    build_legacy_request_data
from bolmate_api_controller.response import SyncResponse
from bolmate_api_controller.types import ID, APIKey, Result, ErrorCode

__all__ = ('get_api_key_sync',)

# Allow up to semaphore API key fetches at a time per process.
# This protects the production database from overflowing in sessions
_semaphore = Semaphore(5)


def get_api_key_sync(*, api_key_id: ID) -> Result:
    with _semaphore, sync_bolmate_connection(isolate=True, auto_commit=False) as database:
        database.execute(SELECT_QUERY, {'api_key_id': api_key_id})
        if not (key_dict := database.first()):
            return Result(
                api_key=None,
                success=False,
                error=ErrorCode.API_KEY_NOT_FOUND,
                retryable=False,
            )

        api_key = APIKey.from_dict(data=key_dict)
        if api_key.status not in ('active', 'importing'):
            return Result(
                api_key=api_key,
                success=False,
                error=ErrorCode.API_KEY_DEACTIVATED,
                retryable=False
            )

        if not api_key.bearer_is_expired:
            return Result(
                api_key=api_key,
                success=True
            )

        if api_key.use_oauth:
            result = _request_oauth_api_key(api_key=api_key)
        else:
            result = _request_legacy_api_key(api_key=api_key)
        if result.success and result.api_key:
            api_key = result.api_key
            database.execute(UPDATE_QUERY,
                             {'api_bearer': enc_data.encrypt(plaintext=api_key.api_bearer),
                              'bearer_expires_at': api_key.bearer_expires_at,
                              'api_key_id': api_key.api_key_id})
        elif result.error == ErrorCode.API_KEY_DEACTIVATED:
            database.execute(DEACTIVATE_QUERY, {'api_key_id': api_key.api_key_id})
        return result


def _request_oauth_api_key(*, api_key: APIKey) -> Result:
    now = PyDateTime()
    refresh_token = api_key.refresh_token
    refresh_expires_at = api_key.refresh_token_expires_at
    if not refresh_token or not refresh_expires_at or refresh_expires_at <= now:
        return Result(
            api_key=api_key,
            success=False,
            error=ErrorCode.REFRESH_EXPIRED,
            retryable=False
        )

    credentials = client_credentials_from_oauth_type(oauth_type=api_key.oauth_type)
    request_data = build_oauth_request_data(refresh_token=refresh_token, credentials=credentials)
    last_exception: Exception | None = None
    last_result: Result | None = None
    for _ in range(MAX_REQUEST_ATTEMPTS):
        try:
            # Burst protection
            acquire_global_sync('oauth_bearer')
            with (bearer_limiter.ratelimit('oauth_bearer', delay=True),
                  requests.request(method='POST', url=request_data.url, data=request_data.data,
                                   headers=request_data.headers, timeout=request_data.timeout) as response):
                sync_response = SyncResponse.build(response=response)
            result = _process_auth_response(api_key=api_key, sync_response=sync_response, token_type='OAuth')
            last_result = result
            if result.success or not result.retryable:
                return result
            last_exception = None
        except Exception as exc:
            last_exception = exc
            auth_exception(f'Exception in refreshing OAuth token, API key ID: {api_key.api_key_id}')
            sleep(1 + random())

    return Result(
        api_key=api_key,
        success=False,
        retryable=False,
        error=ErrorCode.EXCEPTION_RAISED if last_exception is not None else ErrorCode.MAX_ATTEMPTS,
        exception=last_exception,
        response_status=last_result.response_status if last_result else None,
        response_text=last_result.response_text if last_result else None,
    )


def _request_legacy_api_key(*, api_key: APIKey) -> Result:
    if not api_key.key or not api_key.secret:
        return Result(
            api_key=api_key,
            success=False,
            error=ErrorCode.MISSING_LEGACY_CREDENTIALS,
            retryable=False,
        )
    request_data = build_legacy_request_data(key=api_key.key, secret=api_key.secret)
    last_exception: Exception | None = None
    last_result: Result | None = None
    for _ in range(MAX_REQUEST_ATTEMPTS):
        try:
            # Burst protection
            acquire_global_sync('legacy_bearer')
            with (bearer_limiter.ratelimit('legacy_bearer', delay=True),
                  requests.request(method='POST', url=request_data.url, headers=request_data.headers,
                                   timeout=request_data.timeout) as response):
                sync_response = SyncResponse.build(response=response)
            result = _process_auth_response(api_key=api_key, sync_response=sync_response, token_type='legacy')
            last_result = result
            if result.success or not result.retryable:
                return result
            last_exception = None
        except Exception as exc:
            last_exception = exc
            auth_exception(f'Exception in refreshing legacy token, API key ID: {api_key.api_key_id}')
            sleep(1 + random())

    return Result(
        api_key=api_key,
        success=False,
        retryable=False,
        error=ErrorCode.EXCEPTION_RAISED if last_exception is not None else ErrorCode.MAX_ATTEMPTS,
        exception=last_exception,
        response_status=last_result.response_status if last_result else None,
        response_text=last_result.response_text if last_result else None,
    )


def _process_auth_response(*, api_key: APIKey, sync_response: SyncResponse,
                           token_type: Literal['OAuth', 'legacy']) -> Result:
    status = sync_response.status
    response_text = sync_response.text()
    if status == 200:
        response_data = sync_response.json()
        expires_in = int(response_data.get('expires_in') or 0)
        expires_at = PyDateTime().add_seconds(expires_in)
        api_key.api_bearer = response_data.get('access_token')
        api_key.bearer_expires_at = expires_at
        return Result(
            api_key=api_key,
            success=True,
            response_status=status,
            # Do not include the response body, it contains sensitive information
            response_text=None,
        )
    elif status == 429:
        # 429 is global, so it's fine to keep the lock whilst waiting. Calls for other API keys would just
        # result in the same 429 response
        retry_after: str | None = sync_response.headers.get('Retry-After')
        if retry_after:
            # Retry-After is in seconds
            sleep(int(retry_after))
            return Result(
                api_key=api_key,
                success=False,
                retryable=True,
                error=ErrorCode.TOO_MANY_REQUESTS,
                response_status=status,
                response_text=response_text,
            )
        else:
            return Result(
                api_key=api_key,
                success=False,
                retryable=False,
                error=ErrorCode.TOO_MANY_REQUESTS,
                response_status=status,
                response_text=response_text,
            )
    elif status >= 500:
        sleep(1 + random())
        return Result(
            api_key=api_key,
            success=False,
            retryable=True,
            error=ErrorCode.SERVER_ERROR,
            response_status=status,
            response_text=response_text,
        )
    else:
        auth_error(f'Error in refreshing {token_type} token: {response_text}, API key ID: {api_key.api_key_id}')
        error_code = (ErrorCode.API_KEY_DEACTIVATED if ACCOUNT_DEACTIVATED in response_text.casefold()
                      else ErrorCode.UNEXPECTED_RESPONSE)
        # No reason to retry on non-recoverable errors
        return Result(
            api_key=api_key,
            success=False,
            retryable=False,
            error=error_code,
            response_status=status,
            response_text=response_text,
        )
