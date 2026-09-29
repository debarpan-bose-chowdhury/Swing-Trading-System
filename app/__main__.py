"""HTTPS entry point for the swing trading system."""

import json
import os
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


class ApplicationHandler(BaseHTTPRequestHandler):
    def version_string(self) -> str:
        return "app"  # do not leak server/Python versions

    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():  # applies to every response, incl. errors
            self.send_header(name, value)
        super().end_headers()

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/health":
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    certfile = os.environ.get("TLS_CERTFILE")
    keyfile = os.environ.get("TLS_KEYFILE")

    if not certfile or not keyfile:
        raise RuntimeError("TLS_CERTFILE and TLS_KEYFILE must be set for HTTPS.")

    server = ThreadingHTTPServer(("0.0.0.0", port), ApplicationHandler)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
