"""
export_arduino_metrics.py
----------------------------------------------------------------------------
Serves the metrics files written by get_arduino_metrics.py for Prometheus:
one route per Arduino, all on one port.

    http://<this PC>:9102/arduino_<IP>     e.g. /arduino_192.168.1.50
    http://<this PC>:9102/                 list of all routes

One metric is added on every request: arduino_metrics_age_seconds, the time
since the file was last written. If it keeps growing, the parser (service
arduino-parser) has stopped and the other values are old.

Usage
-----
  python export_arduino_metrics.py [--port PORT]      (default port 9102)

The server does not start if another program already listens on the port.
"""

import argparse
import os
import re
import socket
import sys
import time
from pathlib import Path

from flask import Flask, request
from waitress import serve

APP_DIR = Path(__file__).resolve().parent
METRICS_DIR = APP_DIR / "metrics"           # written by get_arduino_metrics.py
DEFAULT_PORT = 9102
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
VALID_KEY = re.compile(r"^[A-Za-z0-9._-]+$")

app = Flask(__name__)


@app.after_request
def log_failed_request(response):
    # Only failed requests are logged, to keep the systemd journal short.
    if response.status_code >= 400:
        print(f"{request.method} {request.path} - {response.status_code}", flush=True)
    return response


def available_routes():
    return sorted("/" + path.stem for path in METRICS_DIR.glob("arduino_*.txt"))


def plain_text(text, status=200):
    return text, status, {'Content-Type': 'text/plain; charset=utf-8'}


@app.route('/')
def index():
    routes = available_routes()
    lines = ["Arduino exporter", "", "Metrics of each Arduino:"]
    lines += [f"  {route}" for route in routes] or [
        "  (none yet - is the arduino-parser service running?)"]
    return plain_text("\n".join(lines) + "\n")


@app.route('/arduino_<key>')
def arduino_metrics(key):
    path = METRICS_DIR / f"arduino_{key}.txt"
    try:
        if not VALID_KEY.match(key):
            raise FileNotFoundError
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
            mtime = os.fstat(fh.fileno()).st_mtime
    except FileNotFoundError:
        routes = "\n".join(available_routes()) or "(none)"
        return plain_text(f"No metrics for '{key}'. Available routes:\n{routes}\n", 404)

    # Same labels as the other metrics of this Arduino (device and ip).
    match = re.search(r"^arduino_up(\{.*\}) ", content, flags=re.MULTILINE)
    labels = match.group(1) if match else ""
    age = max(0.0, time.time() - mtime)
    content = content.rstrip("\n") + "\n" + (
        "# HELP arduino_metrics_age_seconds Seconds since this metrics file "
        "was last written by get_arduino_metrics.py\n"
        "# TYPE arduino_metrics_age_seconds gauge\n"
        f"arduino_metrics_age_seconds{labels} {age:.1f}\n"
    )
    return content, 200, {'Content-Type': CONTENT_TYPE}


def port_in_use(port):
    """True if another program on this PC already listens on *port*."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Serve the Arduino metrics files.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"TCP port (default {DEFAULT_PORT})")
    port = parser.parse_args().port
    if not 1 <= port <= 65535:
        sys.exit(f"ERROR: invalid port {port}")
    if port_in_use(port):
        sys.exit(f"ERROR: port {port} is already used by another program on this PC. "
                 f"Run initial_setup.sh to choose another port.")
    METRICS_DIR.mkdir(exist_ok=True)
    print(f"Serving {METRICS_DIR} at http://0.0.0.0:{port}/arduino_<IP>", flush=True)
    serve(app, host='0.0.0.0', port=port)
