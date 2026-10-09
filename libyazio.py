"""Independent, standard-library-only client. Responses remain raw JSON."""
import asyncio
import json
import threading
import time
from datetime import date, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


class YazioError(Exception):
    """HTTP or transport failure; response bodies are omitted to avoid leaking data."""

    def __init__(self, status: int | None, code: str = "request_failed"):
        self.status = status
        self.code = code
        super().__init__(f"YAZIO: {code} (HTTP {status})")


def _day(value: str | date) -> str:
    if isinstance(value, datetime):
        value = value.date()
    return date.fromisoformat(str(value)).isoformat()


class YazioClient:
    """One account per client. Tokens stay in memory; passwords aren't stored.

    The lock serializes calls so token refresh is safe across worker threads.
    No automatic retry of writes, including after authentication failures.
    """

    def __init__(self, *, access_token: str | None = None,
                 refresh_token: str | None = None, api_version: int = 22,
                 client_id: str | None = None, client_secret: str | None = None,
                 user_agent: str = "YAZIO/26.30.1 (com.yazio.ios.YAZIO; build:2607271240; iOS 27.0.0) Ktor",
                 language: str = "en-US", timeout: float = 30):
        if api_version not in (15, 22) and (client_id is None or client_secret is None):
            raise ValueError("Supply OAuth client credentials for other API versions")
        defaults = (
            ("1_4hiybetvfksgw40o0sog4s884kwc840wwso8go4k8c04goo4c",
             "6rok2m65xuskgkgogw40wkkk8sw0osg84s8cggsc4woos4s8o")
            if api_version == 15 else
            ("3_5rbw4kehpugw8ogsc8ck8oo4ogswgckcskc04gcg8kk8k48ssw",
             "25gdtt1hvdi8gwowoww4oo88sgsw0oo04o0og0kkgwwks8k0k")
        )
        self._base = f"https://yzapi.yazio.com/v{api_version}"
        self._version = api_version
        self._client_id = client_id or defaults[0]
        self._client_secret = client_secret or defaults[1]
        self._headers = {"User-Agent": user_agent, "Accept-Language": language,
                         "Accept": "application/json"}
        self._timeout = timeout
        self._access = access_token
        self._refresh = refresh_token
        self._expires = None
        self._lock = threading.RLock()

    def _send(self, method, path, *, params=None, payload=None, form=None, auth=True):
        url = self._base + path
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        headers = dict(self._headers)
        if auth:
            if not self._access:
                raise YazioError(None, "login_required")
            headers["Authorization"] = f"Bearer {self._access}"
        body = None
        if form is not None:
            body = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif payload is not None:
            body = json.dumps(payload, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            # Only surface a small allowlist of service error codes.
            code = "request_failed"
            try:
                error = json.loads(exc.read()).get("error")
                if error in ("version_blocked", "invalid_grant", "invalid_token", "invalid_client"):
                    code = error
            except (ValueError, AttributeError):
                pass
            finally:
                exc.close()
            raise YazioError(exc.code, code) from None
        except (URLError, TimeoutError, OSError):
            raise YazioError(None, "transport_failed") from None
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise YazioError(None, "invalid_json_response") from None

    def _token(self, fields):
        result = self._send("POST", "/oauth/token", auth=False, form={
            "client_id": self._client_id, "client_secret": self._client_secret, **fields})
        if not isinstance(result, dict) or not result.get("access_token"):
            raise YazioError(None, "invalid_token_response")
        self._access = result["access_token"]
        self._refresh = result.get("refresh_token", self._refresh)
        self._expires = time.monotonic() + max(0, float(result.get("expires_in", 0)) - 30)
        return result

    def login(self, username: str, password: str) -> dict:
        with self._lock:
            return self._token({"grant_type": "password", "username": username, "password": password})

    def refresh(self) -> dict:
        with self._lock:
            if not self._refresh:
                raise YazioError(None, "refresh_token_required")
            return self._token({"grant_type": "refresh_token", "refresh_token": self._refresh})

    def request(self, method: str, path: str, *, params=None, payload=None):
        """Escape hatch for additional version-relative endpoints."""
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("Use a relative endpoint path and params for query arguments")
        method = method.upper()
        with self._lock:
            if self._refresh and (not self._access or
                                  (self._expires is not None and time.monotonic() >= self._expires)):
                self.refresh()
            try:
                return self._send(method, path, params=params, payload=payload)
            except YazioError as exc:
                if exc.status != 401 or method != "GET" or not self._refresh:
                    raise
                self.refresh()
                return self._send(method, path, params=params, payload=payload)

    def get_user(self):
        return self.request("GET", "/user")

    def get_diary(self, day: str | date):
        return self.request("GET", "/user/consumed-items", params={"date": _day(day)})

    def get_daily_summary(self, day: str | date):
        return self.request("GET", "/user/widgets/daily-summary", params={"date": _day(day)})

    def get_water(self, day: str | date):
        return self.request("GET", "/user/water-intake", params={"date": _day(day)})

    def get_exercises(self, day: str | date):
        return self.request("GET", "/user/exercises", params={"date": _day(day)})

    def get_goals(self, day: str | date):
        return self.request("GET", "/user/goals/unmodified", params={"date": _day(day)})

    def get_weight(self, day: str | date):
        return self.request("GET", "/user/bodyvalues/weight/last", params={"date": _day(day)})

    def search_products(self, query: str, *, sex: str, countries: str, locales: str = "en_US"):
        return self.request("GET", "/products/search", params={
            "query": query, "sex": sex, "countries": countries, "locales": locales})

    def get_product(self, product_id: str):
        return self.request("GET", "/products/" + quote(product_id, safe=""))

    def add_consumed_items(self, *, products=(), recipe_portions=(), simple_products=()):
        """Import caller-supplied API payloads; this is not a CSV importer."""
        return self.request("POST", "/user/consumed-items", payload={
            "products": list(products), "recipe_portions": list(recipe_portions),
            "simple_products": list(simple_products)})

    def log_food(self, product_id: str, amount: float, *, when: datetime,
                 meal: str, serving: str | None = None, serving_quantity: float | None = None):
        """Log amount in the product's base unit, using a local wall-clock timestamp."""
        if meal not in ("breakfast", "lunch", "dinner", "snack") or amount <= 0:
            raise ValueError("Provide a valid meal and positive amount")
        return self.add_consumed_items(products=[{
            "id": str(uuid4()), "product_id": product_id, "amount": amount,
            "date": when.strftime("%Y-%m-%d %H:%M:%S"), "daytime": meal,
            "serving": serving, "serving_quantity": serving_quantity}])

    def delete_diary_entry(self, entry_id: str, *, bucket: str = "products"):
        if bucket not in ("products", "recipe_portions", "simple_products"):
            raise ValueError("Invalid diary bucket")
        payload = [entry_id] if self._version == 15 else {bucket: entry_id}
        return self.request("DELETE", "/user/consumed-items", payload=payload)

    def add_body_value(self, entry: dict):
        """Pass a body-value payload matching your selected API version."""
        return self.request("POST", "/user/bodyvalues", payload=entry)


class AsyncYazioClient:
    """Same methods as YazioClient, awaited via asyncio worker threads.

    This is an async facade over blocking HTTP, not a native async transport.
    Cancelling an await does not cancel an already-started HTTP request.
    """

    def __init__(self, **kwargs):
        self._sync = YazioClient(**kwargs)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        method = getattr(self._sync, name)
        if not callable(method):
            raise AttributeError(name)

        async def call(*args, **kwargs):
            return await asyncio.to_thread(method, *args, **kwargs)

        return call
