"""
get_arduino_metrics.py
----------------------------------------------------------------------------
Reads the status pages of several Arduino web servers (compressor monitors)
and writes the values of each Arduino in Prometheus exposition format to its
own file:

    metrics/arduino_<IP>.txt

export_arduino_metrics.py serves each file on
http://<this PC>:9102/arduino_<IP>.

The Arduinos to read are listed in arduino_ips.txt (one IP per line; the file
is written by initial_setup.sh). The list is read again every 10 seconds, so
Arduinos can be added or removed without restarting anything.

Each Arduino page shows the device name (e.g. "Newton") and a small table:

    Parameter              Status   Action
    Compressor running     1
    Unit Good              1
    Helium High Temp       0
    Helium High Pressure   0
    Battery Voltage        3.90
    Compressor             ON       [Turn OFF]
    Monitoring             OFF      [Turn ON]

Every row becomes one metric (see PARAMETERS below); ON/OFF is written as
1/0. A row that is not listed in PARAMETERS (for example, one added to the
Arduino code later) is exported too, under a name made from its text.
Every metric has the labels device="<name shown on the page>" and ip="<IP>".

SAFETY: only the plain page  http://<IP>/  is requested. The "Turn ON/OFF"
links (/?relay=..., /?monitoring=...) are never followed, and any path or
query in an address is dropped, so this script cannot switch anything on
an Arduino.

If a page cannot be read, its values are written as NaN and arduino_up is 0.
Every Arduino is read in its own thread, so a slow or unreachable Arduino
does not delay the others.

Usage
-----
  python get_arduino_metrics.py [--once | --routes] [IP ...]

  (no option)  run until stopped, reading every Arduino every 10 seconds
               (this is what the systemd service arduino-parser runs)
  --once       read every Arduino once, print the values and exit
               (exit code 0 if all pages were read, 1 if not)
  --routes     print the web route of every Arduino (/arduino_<IP>) and exit
  IP ...       use these addresses instead of arduino_ips.txt
               (IP or IP:port, e.g. 192.168.1.50 or 192.168.1.50:8080)
"""

import argparse
import math
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

# --- USER CONFIGURATION -----------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
IPS_FILE = APP_DIR / "arduino_ips.txt"   # Arduinos to read (written by initial_setup.sh)
METRICS_DIR = APP_DIR / "metrics"        # one metrics file per Arduino
INTERVAL_SECONDS = 10           # time between two reads of the same Arduino
TIMEOUT_SECONDS = 5             # how long to wait for an Arduino to answer
ATTEMPTS = 3                    # tries per read before the read counts as failed
PAUSE_BETWEEN_ATTEMPTS = 1      # seconds
# ----------------------------------------------------------------------------

# -- Arduino table row -> metric ----------------------------------------------
# Key: text of the "Parameter" column in lower case.
# Value: (metric name, help text).
PARAMETERS = {
    "compressor running":   ("arduino_compressor_running",
                             "Compressor running signal (1 = running, 0 = not running)"),
    "unit good":            ("arduino_unit_good",
                             "Unit good signal (1 = good, 0 = not good)"),
    "helium high temp":     ("arduino_helium_high_temperature_alarm",
                             "Helium high temperature signal (1 = active, 0 = not active)"),
    "helium high pressure": ("arduino_helium_high_pressure_alarm",
                             "Helium high pressure signal (1 = active, 0 = not active)"),
    "battery voltage":      ("arduino_battery_voltage_volts",
                             "Battery voltage measured by the Arduino"),
    "compressor":           ("arduino_compressor_relay_state",
                             "Compressor relay, switched from the web page (1 = ON, 0 = OFF)"),
    "monitoring":           ("arduino_monitoring_state",
                             "Monitoring mode of the Arduino (1 = ON, 0 = OFF)"),
}

UP_METRIC = ("arduino_up",
             "1 if the Arduino page was read on the last run, 0 if not")

ON_WORDS = {"on", "true", "yes", "high"}
OFF_WORDS = {"off", "false", "no", "low"}
NUMBER = re.compile(r"[-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][-+]?\d+)?")
MAX_PAGE_BYTES = 1_000_000

NaN = float("nan")

Result = namedtuple("Result", "page_read device status samples")


# -- logging ------------------------------------------------------------------

def log(message):
    # The systemd journal adds its own time stamp.
    if "JOURNAL_STREAM" in os.environ:
        print(message, flush=True)
    else:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


# -- addresses ----------------------------------------------------------------

def parse_address(address):
    """
    "192.168.1.50", "192.168.1.50:8080" or "http://192.168.1.50/..."
    -> (host, port).

    Any path or query (e.g. "/?relay=OFF") is dropped on purpose:
    only the plain page "/" is ever requested.
    """
    text = address.strip().strip('"\'').strip()
    if "://" not in text:
        text = "http://" + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme.lower() != "http":
        raise ValueError(f"only http:// addresses are supported, got '{address}'")
    host = parts.hostname
    if not host:
        raise ValueError(f"no IP address found in '{address}'")
    port = parts.port or 80          # raises ValueError for an invalid port
    return host, port


class Target:
    """One Arduino: its address, the metrics file and the route of the exporter."""

    def __init__(self, address):
        self.host, self.port = parse_address(address)
        self.ip = self.host if self.port == 80 else f"{self.host}:{self.port}"
        self.key = re.sub(r"[^A-Za-z0-9._-]", "_", self.ip)     # 192.168.1.50:8080 -> 192.168.1.50_8080
        shown_host = f"[{self.host}]" if ":" in self.host else self.host
        self.url = f"http://{shown_host}/" if self.port == 80 else f"http://{shown_host}:{self.port}/"
        self.route = f"/arduino_{self.key}"
        self.file = METRICS_DIR / f"arduino_{self.key}.txt"


def read_ip_list(path):
    """Addresses in arduino_ips.txt: one or more per line, '#' starts a comment."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    addresses = []
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        addresses.extend(item for item in re.split(r"[\s,;]+", line) if item)
    return addresses


def make_targets(addresses):
    """Returns ({key: Target}, [error messages for invalid addresses])."""
    targets, errors = {}, []
    for address in addresses:
        try:
            target = Target(address)
        except ValueError as exc:
            errors.append(f"{address}: ignored - {exc}")
            continue
        targets.setdefault(target.key, target)
    return targets, errors


# -- reading a page -----------------------------------------------------------

def fetch_page(host, port, timeout):
    """
    Download http://host:port/ and return the HTML text.

    A plain socket is used instead of urllib: small Arduino web servers often
    do not follow HTTP exactly (missing header lines, connection not closed,
    connection reset right after the page). The page text that arrived is
    used in all these cases. No proxy is used.
    """
    host_header = f"[{host}]" if ":" in host else host
    if port != 80:
        host_header += f":{port}"
    request = (
        "GET / HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        "User-Agent: arduino-exporter\r\n"
        "Accept: text/html\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")

    received = bytearray()
    deadline = time.monotonic() + 2 * timeout
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(request)
        while len(received) < MAX_PAGE_BYTES and time.monotonic() < deadline:
            try:
                chunk = sock.recv(4096)
            except (socket.timeout, ConnectionError):
                break        # connection kept open, or reset after the page
            if not chunk:
                break        # connection closed by the Arduino: page complete
            received += chunk

    if not received:
        raise ConnectionError("the Arduino accepted the connection but sent nothing")

    data = bytes(received)
    if data.startswith(b"HTTP/"):
        # Split off the HTTP header and check the status code.
        head, body = data, b""
        for separator in (b"\r\n\r\n", b"\n\n"):
            if separator in data:
                head, _, body = data.partition(separator)
                break
        status_line = head.split(b"\n", 1)[0].decode("latin-1").strip()
        fields = status_line.split()
        if len(fields) < 2 or fields[1] != "200":
            raise ConnectionError(f"the Arduino answered '{status_line}'")
        data = body
    return data.decode("utf-8", errors="replace")


class ArduinoPage(HTMLParser):
    """Collects the device name (text of <h1>) and the cells of all table rows."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._name_parts = []
        self._rows = []           # [[(tag, text), ...], ...]
        self._in_h1 = False
        self._row = None          # cells of the current <tr>
        self._cell = None         # text pieces of the current <td>/<th>
        self._cell_tag = None

    def handle_starttag(self, tag, attrs):
        if tag == "h1":
            self._in_h1 = True
        elif tag == "tr":
            self._end_row()
            self._row = []
        elif tag in ("td", "th"):
            self._end_cell()
            if self._row is None:
                self._row = []
            self._cell = []
            self._cell_tag = tag

    def handle_endtag(self, tag):
        if tag == "h1":
            self._in_h1 = False
        elif tag in ("td", "th"):
            self._end_cell()
        elif tag in ("tr", "thead", "tbody", "table"):
            self._end_row()

    def handle_data(self, data):
        if self._in_h1:
            self._name_parts.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def close(self):
        super().close()
        self._end_row()

    def _end_cell(self):
        if self._cell is not None and self._row is not None:
            self._row.append((self._cell_tag, " ".join("".join(self._cell).split())))
        self._cell = None

    def _end_row(self):
        self._end_cell()
        if self._row:
            self._rows.append(self._row)
        self._row = None

    @property
    def device_name(self):
        return " ".join("".join(self._name_parts).split())

    def parameters(self):
        """[(parameter, status text), ...] from the rows with data cells (<td>)."""
        result = []
        for row in self._rows:
            cells = [text for tag, text in row if tag == "td"]
            if len(cells) >= 2 and cells[0]:
                result.append((cells[0], cells[1]))
        return result


def read_arduino(host, port):
    """Read and parse the page. Returns (device name, [(parameter, status), ...])."""
    page = ArduinoPage()
    page.feed(fetch_page(host, port, TIMEOUT_SECONDS))
    page.close()
    parameters = page.parameters()
    if not parameters:
        raise ValueError("no parameter table found on the page")
    return page.device_name, parameters


# -- converting values ---------------------------------------------------------

def to_value(text):
    """'1' -> 1.0,  '3.90' -> 3.9,  'ON' -> 1.0,  'OFF' -> 0.0,  anything else -> NaN."""
    word = text.strip().lower()
    if word in ON_WORDS:
        return 1.0
    if word in OFF_WORDS:
        return 0.0
    match = NUMBER.match(word)
    if match:
        return float(match.group(0).replace(",", "."))
    return NaN


def generic_metric_name(parameter):
    """'Some New Value' -> 'arduino_some_new_value'."""
    slug = re.sub(r"[^a-z0-9]+", "_", parameter.lower()).strip("_")
    return "arduino_" + (slug or "unnamed")


def build_samples(parameters, page_read):
    """
    Returns [(metric name, help text, value, page text), ...].
    Known parameters always appear (NaN when missing), extra rows follow.
    """
    found = {}
    for parameter, status in parameters:
        key = " ".join(parameter.lower().split())
        found.setdefault(key, (parameter, status))

    samples = [(UP_METRIC[0], UP_METRIC[1], 1.0 if page_read else 0.0, "")]
    used_names = {UP_METRIC[0]}
    for key, (name, help_text) in PARAMETERS.items():
        status = found[key][1] if key in found else ""
        samples.append((name, help_text, to_value(status) if key in found else NaN, status))
        used_names.add(name)

    for key, (parameter, status) in found.items():
        if key in PARAMETERS:
            continue
        name = base = generic_metric_name(parameter)
        number = 2
        while name in used_names:
            name = f"{base}_{number}"
            number += 1
        used_names.add(name)
        help_text = f"Value of '{parameter}' on the Arduino page (ON = 1, OFF = 0)"
        samples.append((name, help_text, to_value(status), status))
    return samples


# -- metrics writer ------------------------------------------------------------

def fmt(value):
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def escape_label(value):
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def one_line(text):
    return " ".join(str(text).split())


def escape_help(text):
    return one_line(text).replace("\\", "\\\\")


def previous_device_name(path):
    """Device name in the previous metrics file (kept while the Arduino is unreachable)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r"^# device: (.*)$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def render(samples, labels, header):
    lines = [f"# {one_line(item)}" for item in header]
    label_text = "{" + ",".join(f'{k}="{escape_label(v)}"' for k, v in labels.items()) + "}"
    for name, help_text, value, _ in samples:
        lines.append(f"# HELP {name} {escape_help(help_text)}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name}{label_text} {fmt(value)}")
    return "\n".join(lines) + "\n"


def write_file(path, text):
    """Replace *path* in one step, so the exporter never serves a half-written file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


# -- one Arduino ----------------------------------------------------------------

def update(target):
    """Read one Arduino and rewrite its metrics file."""
    device, parameters, error = "", [], ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            device, parameters = read_arduino(target.host, target.port)
            error = ""
            break
        except Exception as exc:        # any problem -> NaN values, arduino_up 0
            error = f"{type(exc).__name__}: {exc}"
            if attempt < ATTEMPTS:
                time.sleep(PAUSE_BETWEEN_ATTEMPTS)

    page_read = not error
    if not device:
        device = previous_device_name(target.file) or "unknown"

    samples = build_samples(parameters, page_read)
    status = f"OK - {len(parameters)} parameters read" if page_read else f"ERROR - {error}"
    header = [
        "Arduino exporter - written by get_arduino_metrics.py",
        f"source: {target.url}",
        f"time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"device: {device}",
        f"status: {status}",
    ]
    write_file(target.file, render(samples, {"device": device, "ip": target.ip}, header))
    return Result(page_read, device, status, samples)


def worker(target, stop):
    """Read one Arduino every INTERVAL_SECONDS until *stop* is set."""
    last_message = None
    while not stop.is_set():
        started = time.monotonic()
        try:
            result = update(target)
            message = f"{result.status} (device: {result.device})"
        except Exception as exc:        # e.g. the metrics folder is not writable
            message = f"ERROR - {type(exc).__name__}: {exc}"
        if message != last_message:     # log changes only, not every read
            log(f"{target.ip}: {message}")
            last_message = message
        stop.wait(max(1.0, INTERVAL_SECONDS - (time.monotonic() - started)))
    # Removed from the list: delete its file so it is no longer served.
    try:
        target.file.unlink()
    except FileNotFoundError:
        pass


def remove_old_files(keys):
    """Delete metrics files of Arduinos that are no longer listed, and old temporary files."""
    for path in METRICS_DIR.glob("arduino_*.txt"):
        if path.name[len("arduino_"):-len(".txt")] not in keys:
            try:
                path.unlink()
                log(f"Deleted {path.name} (this Arduino is no longer listed)")
            except FileNotFoundError:
                pass
    for path in METRICS_DIR.glob("*.tmp"):
        try:
            if time.time() - path.stat().st_mtime > 60:
                path.unlink()
        except FileNotFoundError:
            pass


# -- modes ----------------------------------------------------------------------

def run_forever(fixed_addresses):
    METRICS_DIR.mkdir(exist_ok=True)
    source = "the command line" if fixed_addresses else str(IPS_FILE)
    log(f"Started: reading the Arduinos listed in {source} every {INTERVAL_SECONDS} s")
    workers = {}                    # key -> (target, stop event, thread)
    last_errors, last_keys = None, None
    while True:
        addresses = fixed_addresses or read_ip_list(IPS_FILE)
        targets, errors = make_targets(addresses)
        if errors != last_errors:
            for error in errors:
                log(error)
            last_errors = errors
        if set(targets) != last_keys:
            if not targets:
                log(f"No Arduino addresses in {source}")
            else:
                log("Arduinos: " + ", ".join(t.ip for t in targets.values()))
            last_keys = set(targets)

        for key in list(workers):                   # Arduinos removed from the list
            if key not in targets:
                workers.pop(key)[1].set()
        for key, target in targets.items():         # new Arduinos (or a stopped thread)
            if key not in workers or not workers[key][2].is_alive():
                stop = threading.Event()
                thread = threading.Thread(target=worker, args=(target, stop),
                                          name=f"arduino-{key}", daemon=True)
                thread.start()
                workers[key] = (target, stop, thread)

        remove_old_files(set(targets))
        time.sleep(INTERVAL_SECONDS)


def run_once(addresses):
    targets, errors = make_targets(addresses)
    for error in errors:
        print(error)
    if not targets:
        print(f"No Arduino addresses: give them as arguments or list them in {IPS_FILE}")
        return 1
    METRICS_DIR.mkdir(exist_ok=True)

    def safe_update(target):
        try:
            return update(target)
        except Exception as exc:        # e.g. the metrics folder is not writable
            return Result(False, "unknown", f"ERROR - {type(exc).__name__}: {exc}", [])

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        results = list(pool.map(safe_update, targets.values()))

    for target, result in zip(targets.values(), results):
        print(f"Arduino page: {target.url}   (device: {result.device})")
        for name, _, value, page_text in result.samples[1:]:
            shown = page_text if page_text else "-"
            print(f"  {name:<42} {fmt(value):>6}   (page: {shown})")
        print(f"Status:       {result.status}")
        print(f"Written to:   {target.file}   (route {target.route})\n")
    return 0 if all(r.page_read for r in results) and not errors else 1


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(
        description="Read the Arduino web pages and write Prometheus metrics files.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="read every Arduino once, print the values and exit")
    mode.add_argument("--routes", action="store_true",
                      help="print the web route of every Arduino and exit")
    parser.add_argument("addresses", nargs="*", metavar="IP",
                        help="Arduino addresses (default: the ones in arduino_ips.txt)")
    args = parser.parse_args()
    addresses = args.addresses or read_ip_list(IPS_FILE)

    if args.routes:
        targets, errors = make_targets(addresses)
        for error in errors:
            print(error, file=sys.stderr)
        for target in targets.values():
            print(target.route)
        return 0
    if args.once:
        return run_once(addresses)
    run_forever(args.addresses)
    return 0


if __name__ == "__main__":
    sys.exit(main())
