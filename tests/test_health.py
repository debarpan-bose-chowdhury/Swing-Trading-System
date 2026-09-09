import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

from app.__main__ import ApplicationHandler


class HealthEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ApplicationHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def test_health_endpoint_returns_ok(self) -> None:
        with urlopen(f"{self.url}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response), {"status": "ok"})

    def test_unknown_endpoint_returns_not_found(self) -> None:
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.url}/unknown")

        self.assertEqual(error.exception.code, 404)
