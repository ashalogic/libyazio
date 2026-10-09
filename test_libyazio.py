import asyncio
import io
import json
import unittest
from datetime import datetime
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from libyazio import AsyncYazioClient, YazioClient, YazioError


class Response(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value).encode() if value is not None else b"")


def error(status, value):
    return HTTPError("https://yzapi.yazio.com", status, "failure", {},
                     io.BytesIO(json.dumps(value).encode()))


class ClientTests(unittest.TestCase):
    @patch("libyazio.urlopen")
    def test_login_form_and_authenticated_query(self, http):
        http.side_effect = [Response({"access_token": "a", "refresh_token": "r", "expires_in": 3600}),
                            Response({"products": []})]
        client = YazioClient()
        client.login("a+b@example.com", "p&ss")
        form = parse_qs(http.call_args_list[0].args[0].data.decode())
        self.assertEqual(form["password"], ["p&ss"])
        self.assertEqual(form["username"], ["a+b@example.com"])
        self.assertEqual(client.get_diary("2026-10-09"), {"products": []})
        request = http.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer a")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {"date": ["2026-10-09"]})

    @patch("libyazio.urlopen")
    def test_refresh_get_once(self, http):
        http.side_effect = [error(401, {}), Response({"access_token": "new", "expires_in": 3600}),
                            Response({"email": "test"})]
        client = YazioClient(access_token="old", refresh_token="refresh")
        self.assertEqual(client.get_user(), {"email": "test"})
        self.assertEqual(http.call_count, 3)
        self.assertEqual(http.call_args.args[0].get_header("Authorization"), "Bearer new")

    @patch("libyazio.urlopen")
    def test_write_401_not_replayed(self, http):
        http.side_effect = error(401, {})
        client = YazioClient(access_token="old", refresh_token="r")
        with self.assertRaises(YazioError):
            client.add_consumed_items(products=[])
        self.assertEqual(http.call_count, 1)

    @patch("libyazio.urlopen")
    def test_food_payload_and_empty_success(self, http):
        http.return_value = Response(None)
        client = YazioClient(access_token="a")
        self.assertIsNone(client.log_food("product", 150, when=datetime(2026, 10, 9, 12), meal="lunch"))
        body = json.loads(http.call_args.args[0].data)
        self.assertEqual(body["products"][0]["date"], "2026-10-09 12:00:00")
        self.assertEqual(body["recipe_portions"], [])
        self.assertTrue(body["products"][0]["id"])

    @patch("libyazio.urlopen")
    def test_deletion_versions(self, http):
        http.side_effect = [Response(None), Response(None)]
        YazioClient(access_token="a").delete_diary_entry("entry")
        self.assertEqual(json.loads(http.call_args.args[0].data), {"products": "entry"})
        YazioClient(access_token="a", api_version=15).delete_diary_entry("entry")
        self.assertEqual(json.loads(http.call_args.args[0].data), ["entry"])

    @patch("libyazio.urlopen")
    def test_error_sanitized(self, http):
        http.side_effect = error(403, {"error": "version_blocked", "email": "private"})
        with self.assertRaises(YazioError) as raised:
            YazioClient(access_token="a").get_user()
        self.assertEqual(raised.exception.code, "version_blocked")
        self.assertNotIn("private", str(raised.exception))

    @patch("libyazio.urlopen")
    def test_transport_failure_not_replayed(self, http):
        http.side_effect = URLError("private")
        with self.assertRaises(YazioError) as raised:
            YazioClient(access_token="a").add_consumed_items()
        self.assertEqual(raised.exception.code, "transport_failed")
        self.assertEqual(http.call_count, 1)

    @patch("libyazio.urlopen")
    def test_expiry_refreshes_before_write(self, http):
        http.side_effect = [Response({"access_token": "a", "refresh_token": "r", "expires_in": 1}),
                            Response({"access_token": "b", "expires_in": 3600}), Response(None)]
        client = YazioClient()
        client.login("email", "password")
        client.add_consumed_items()
        self.assertEqual(http.call_args.args[0].get_header("Authorization"), "Bearer b")

    def test_validation_before_network(self):
        client = YazioClient(access_token="a")
        with self.assertRaises(ValueError):
            client.get_diary("bad")
        with self.assertRaises(ValueError):
            client.log_food("p", -1, when=datetime.now(), meal="lunch")

    @patch("libyazio.urlopen")
    def test_async_facade(self, http):
        http.return_value = Response({"products": []})
        client = AsyncYazioClient(access_token="a")
        self.assertEqual(asyncio.run(client.get_diary("2026-10-09")), {"products": []})


if __name__ == "__main__":
    unittest.main()

