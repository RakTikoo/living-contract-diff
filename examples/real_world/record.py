#!/usr/bin/env python3
"""Record REAL responses from server.py into traffic.har.

Starts the fixture server on a local port, issues real HTTP requests with
urllib, and writes the actual status / Content-Type / body of each response
into a HAR file next to this script. No invented payloads.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
PATHS = ["/products/1", "/products/2", "/products/3", "/health", "/products/99"]


def main():
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py"), str(PORT)])
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(BASE + "/health", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        entries = []
        for path in PATHS:
            try:
                with urllib.request.urlopen(BASE + path, timeout=5) as r:
                    status, mime, text = r.status, r.headers.get_content_type(), \
                        r.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                status, mime, text = e.code, e.headers.get_content_type(), \
                    e.read().decode("utf-8")
            entries.append({
                "request": {"method": "GET", "url": BASE + path},
                "response": {"status": status,
                             "content": {"mimeType": mime, "text": text}}})
            print(f"GET {path} -> {status} ({mime}) {text[:60]}")
        har = {"log": {"version": "1.2",
                       "creator": {"name": "record.py (real local capture)", "version": "1"},
                       "entries": entries}}
        with open(os.path.join(HERE, "traffic.har"), "w", encoding="utf-8") as f:
            json.dump(har, f, indent=1)
        print("wrote traffic.har with", len(entries), "real entries")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    main()
