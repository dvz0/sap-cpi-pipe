import time
from typing import Any

import requests

from sap_pipe.log import get_logger

log = get_logger()

_RETRY_STATUS = {429, 502, 503, 504}
_RETRY_WAITS = (1, 2)


class ApiError(Exception):
    def __init__(self, call_type: str, status: int, body: bytes):
        self.call_type = call_type
        self.status = status
        self.body = body
        super().__init__(f"{call_type} call failed with response code = {status}")


class HttpExecuter:
    def __init__(
        self,
        host: str,
        oauth_host: str = "",
        oauth_path: str = "/oauth/token",
        client_id: str = "",
        client_secret: str = "",
        user_id: str = "",
        password: str = "",
        scheme: str = "https",
        port: int = 443,
    ):
        self.host = host
        self.scheme = scheme
        self.port = port
        self.session = requests.Session()
        self._token = ""
        self._token_expiry = 0.0
        self.oauth_host = oauth_host
        self.oauth_path = oauth_path or "/oauth/token"
        self.client_id = client_id
        self.client_secret = client_secret
        if oauth_host:
            self.auth_type = "OAUTH"
            log.debug("Initialising HTTP client with OAuth 2.0")
        else:
            self.auth_type = "BASIC"
            self.session.auth = (user_id, password)
            log.debug("Initialising HTTP client with Basic Authentication")
        self._csrf_token = ""
        self._csrf_cookies: list[Any] = []

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        cookies: list[Any] | None = None,
        timeout: int = 300,
    ) -> requests.Response:
        url = f"{self.scheme}://{self.host}:{self.port}{path}"
        log.debug("Executing HTTP request: %s %s", method, url)
        request_headers = dict(headers or {})
        if self.auth_type == "OAUTH":
            request_headers["Authorization"] = f"Bearer {self._access_token()}"
        jar = requests.cookies.RequestsCookieJar()
        for cookie in cookies or []:
            jar.set_cookie(cookie)
        return self._send(method, url, body if body else None, request_headers, jar if cookies else None, timeout)

    def _send(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
        cookies: requests.cookies.RequestsCookieJar | None,
        timeout: int,
    ) -> requests.Response:
        """Retry throttling and transient gateway errors.

        POST, PUT and DELETE are retried only on 429. A 5xx on those methods
        may already have changed the tenant, so it is not sent again.
        """
        response: requests.Response | None = None
        for attempt in range(3):
            try:
                response = self.session.request(
                    method,
                    url,
                    data=body,
                    headers=headers,
                    cookies=cookies,
                    timeout=timeout,
                )
            except (requests.ConnectionError, requests.Timeout):
                if method != "GET" or attempt == 2:
                    raise
                log.warning("HTTP %s %s failed before a response; retrying", method, url)
                self._pause(attempt, None)
                continue
            if response.status_code in _RETRY_STATUS and attempt < 2 and (method == "GET" or response.status_code == 429):
                log.warning("HTTP %s %s returned %s; retrying", method, url, response.status_code)
                self._pause(attempt, response)
                continue
            return response
        assert response is not None
        return response

    def _pause(self, attempt: int, response: requests.Response | None) -> None:
        wait = _RETRY_WAITS[min(attempt, len(_RETRY_WAITS) - 1)]
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                wait = min(int(retry_after), 30)
        time.sleep(wait)

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        token_url = f"{self.scheme}://{self.oauth_host}:{self.port}{self.oauth_path}"
        log.debug("Setting up OAuth 2.0 client with token URL %s", token_url)
        response = self.session.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=60,
        )
        if response.status_code >= 400:
            raise ApiError("OAuth token", response.status_code, response.content)
        payload = response.json()
        self._token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 3600))
        self._token_expiry = time.time() + max(expires_in - 30, 1)
        return self._token

    def read_body(self, response: requests.Response) -> bytes:
        return response.content

    def log_error(self, response: requests.Response, call_type: str) -> bytes:
        body = response.content
        if body:
            log.warning("Response body = %s", body.decode("utf-8", errors="replace"))
        return body

    def csrf(self) -> tuple[dict[str, str], list[Any]]:
        if self.auth_type != "BASIC":
            return {}, []
        if not self._csrf_token:
            log.debug("Get CSRF Token")
            response = self.request("GET", "/api/v1/", headers={"x-csrf-token": "fetch"})
            if response.status_code != 200:
                self.log_error(response, "Get CSRF Token")
                raise ApiError("Get CSRF Token", response.status_code, response.content)
            self._csrf_token = response.headers.get("x-csrf-token", "")
            self._csrf_cookies = list(response.cookies)
            log.debug("Received CSRF Token")
        return {"x-csrf-token": self._csrf_token}, self._csrf_cookies

    def modifying(
        self,
        method: str,
        path: str,
        content: bytes | None,
        success_code: int,
        call_type: str,
        content_type: str = "application/json",
    ) -> None:
        headers, cookies = self.csrf()
        headers["Accept"] = "application/json"
        body = content or b""
        if content:
            headers["Content-Type"] = content_type
            log.debug("Request body = %s", content[:500])
        response = self.request(method, path, body if content else None, headers, cookies)
        if response.status_code != success_code:
            self.log_error(response, call_type)
            raise ApiError(call_type, response.status_code, response.content)

    def read_only(
        self,
        path: str,
        call_type: str,
        content: bytes | None = None,
        accept: str | None = "application/json",
    ) -> requests.Response:
        headers: dict[str, str] = {}
        if accept:
            headers["Accept"] = accept
        if content:
            log.debug("Request body = %s", content[:500])
        response = self.request("GET", path, content, headers, None)
        if response.status_code != 200:
            if response.status_code == 404:
                log.debug("%s returned 404", call_type)
                raise ApiError(call_type, response.status_code, response.content)
            body = self.log_error(response, call_type)
            raise ApiError(call_type, response.status_code, body)
        return response
