import json
import os
import runpy
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

import app.__main__ as app_main
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
            self.assertEqual(response.headers.get("Strict-Transport-Security"), "max-age=31536000; includeSubDomains")

    def test_unknown_endpoint_returns_not_found(self) -> None:
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.url}/unknown")

        self.assertEqual(error.exception.code, 404)

    def test_main_requires_tls_cert_and_key(self) -> None:
        with patch.dict(os.environ, {"PORT": "9000", "TLS_CERTFILE": "cert.pem"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "TLS_CERTFILE and TLS_KEYFILE must be set for HTTPS"):
                app_main.main()

    @patch("app.__main__.ssl.create_default_context")
    @patch("app.__main__.ThreadingHTTPServer")
    def test_main_uses_default_port_when_not_configured(self, mock_server, mock_create_context) -> None:
        server = Mock()
        original_socket = object()
        server.socket = original_socket
        wrapped_socket = object()
        mock_server.return_value = server
        context = Mock()
        context.options = 0
        context.wrap_socket.return_value = wrapped_socket
        mock_create_context.return_value = context

        with patch.dict(os.environ, {"TLS_CERTFILE": "cert.pem", "TLS_KEYFILE": "key.pem"}, clear=True):
            app_main.main()

        mock_server.assert_called_once_with(("0.0.0.0", 8080), ApplicationHandler)
        mock_create_context.assert_called_once_with(app_main.ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain.assert_called_once_with(certfile="cert.pem", keyfile="key.pem")
        self.assertEqual(context.minimum_version, app_main.ssl.TLSVersion.TLSv1_2)
        self.assertEqual(context.options, 0)
        context.wrap_socket.assert_called_once_with(original_socket, server_side=True)
        self.assertIs(server.socket, wrapped_socket)
        server.serve_forever.assert_called_once_with()

    @patch("app.__main__.ssl.create_default_context")
    @patch("app.__main__.ThreadingHTTPServer")
    def test_main_uses_configured_port(self, mock_server, mock_create_context) -> None:
        server = Mock()
        original_socket = object()
        server.socket = original_socket
        wrapped_socket = object()
        mock_server.return_value = server
        context = Mock()
        context.options = 0
        context.wrap_socket.return_value = wrapped_socket
        mock_create_context.return_value = context

        with patch.dict(os.environ, {"PORT": "9000", "TLS_CERTFILE": "cert.pem", "TLS_KEYFILE": "key.pem"}, clear=True):
            app_main.main()

        mock_server.assert_called_once_with(("0.0.0.0", 9000), ApplicationHandler)
        mock_create_context.assert_called_once_with(app_main.ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain.assert_called_once_with(certfile="cert.pem", keyfile="key.pem")
        self.assertEqual(context.minimum_version, app_main.ssl.TLSVersion.TLSv1_2)
        self.assertEqual(context.options, 0)
        context.wrap_socket.assert_called_once_with(original_socket, server_side=True)
        self.assertIs(server.socket, wrapped_socket)
        server.serve_forever.assert_called_once_with()

    @patch("http.server.ThreadingHTTPServer")
    @patch("ssl.create_default_context")
    def test_module_run_executes_main_entrypoint(self, mock_create_context, mock_server) -> None:
        server = Mock()
        original_socket = object()
        server.socket = original_socket
        wrapped_socket = object()
        mock_server.return_value = server
        context = Mock()
        context.options = 0
        context.wrap_socket.return_value = wrapped_socket
        mock_create_context.return_value = context

        script_path = Path(__file__).resolve().parents[1] / "app" / "__main__.py"
        with patch.dict(os.environ, {"PORT": "7000", "TLS_CERTFILE": "cert.pem", "TLS_KEYFILE": "key.pem"}, clear=True):
            runpy.run_path(str(script_path), run_name="__main__")

        self.assertEqual(mock_server.call_args.args[0], ("0.0.0.0", 7000))
        self.assertEqual(mock_server.call_args.args[1].__name__, "ApplicationHandler")
        mock_create_context.assert_called_once_with(app_main.ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain.assert_called_once_with(certfile="cert.pem", keyfile="key.pem")
        self.assertEqual(context.minimum_version, app_main.ssl.TLSVersion.TLSv1_2)
        self.assertEqual(context.options, 0)
        context.wrap_socket.assert_called_once_with(original_socket, server_side=True)
        self.assertIs(server.socket, wrapped_socket)
        server.serve_forever.assert_called_once_with()
