#!/usr/bin/env python3
"""Tiny fixture API using only the Python standard library (http.server).

Run standalone:  python3 server.py [port]     (default 8765)
Endpoints:
  GET /products/{id}   one product as JSON
  GET /health          {"status": "ok"}
This server is test material for the real-world run — it was actually
executed locally and its real responses were recorded into traffic.har
by record.py.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PRODUCTS = {
    1: {"id": 1, "name": "Widget", "price": 9.99, "currency": "USD", "stock": 12},
    2: {"id": 2, "name": "Gadget", "price": 19.5, "currency": "USD", "stock": 0},
    3: {"id": 3, "name": "Sprocket", "price": 4.25, "currency": "EUR", "stock": 7},
}


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        elif self.path.startswith("/products/"):
            try:
                pid = int(self.path.rsplit("/", 1)[1])
            except ValueError:
                pid = -1
            if pid in PRODUCTS:
                self._json(200, PRODUCTS[pid])
            else:
                self._json(404, {"error": "not found"})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
