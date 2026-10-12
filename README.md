# Arduino Exporter (Ubuntu)

One Ubuntu PC reads the status pages of several Arduino web servers (compressor
monitors such as `Newton`) every 10 seconds, and serves the metrics of each
Arduino on one port (`9102`), on its own route, for `Prometheus` and `Grafana`:

    http://<this PC>:9102/arduino_<IP>      e.g. /arduino_192.168.1.50
    http://<this PC>:9102/                  lists all routes

The exporter only reads the page `http://<Arduino IP>/`. It never opens the
"Turn ON / Turn OFF" links (`/?relay=...`, `/?monitoring=...`), so it cannot
switch a compressor or the monitoring.


## Requirements

- Ubuntu 20.04 or later (with systemd)
- Administrator access (`sudo`, one time, for the setup only)
- No internet: Python and all packages are in `python_installer/`
- This folder on a local disk, owned by the user who should run the exporter
  (for example in your home folder). The services run as the owner of the folder.
- The Arduino pages reachable from this PC (open `http://<Arduino IP>/` in a browser to check)
- A free TCP port on this PC (default `9102`), reachable from your Prometheus server
  (the setup opens it in the `ufw` firewall). The default is 9102 because the
  Bluefors exporter uses 9101, so the two never clash.


## Files in this folder

| File / Folder                 | Purpose |
|-------------------------------|---------|
| `get_arduino_metrics.py`      | Reads every Arduino in `arduino_ips.txt` every 10 s and writes one file per Arduino to `metrics/`. |
| `export_arduino_metrics.py`   | Flask + Waitress server: serves each file on `/arduino_<IP>`, all on one port. |
| `initial_setup.sh`            | Setup: Python, list of Arduinos, port, systemd services, firewall. Can be run again. |
| `arduino_ips.txt`             | The list of Arduinos, one IP per line (created by the setup; can be edited any time). |
| `python_installer/`           | Offline installer: `install_python_and_packages.sh`, a standalone Python 3.13 for Linux, and the `wheels/` of flask, waitress and their dependencies. |
| `metrics/`                    | Latest metrics, `arduino_<IP>.txt` per Arduino (overwritten on each read). |
| `venv/`, `python/`            | Python environment made by the installer (`python/` only if the bundled Python is used). |


## Services instead of .bat files

On Ubuntu the two Python scripts run as **systemd services**, so no wrapper
scripts are needed:

- `arduino-parser`   → `get_arduino_metrics.py`
- `arduino-exporter` → `export_arduino_metrics.py --port 9102`

systemd starts them at PC boot (before anyone logs in), restarts them 10 s after
they stop for any reason (with no limit on the number of restarts), never stops
them because of a time limit, and keeps their messages in the system journal.


## Metrics

Every metric has the labels `device` (the name shown on the Arduino page, e.g.
`Newton`) and `ip` (the address of the Arduino).

| Metric                                  | Arduino page row       | Values |
|-----------------------------------------|------------------------|--------|
| `arduino_up`                            | -                      | 1 = page read on the last try, 0 = Arduino not reachable (then all values below are NaN) |
| `arduino_compressor_running`            | Compressor running     | 1 / 0 |
| `arduino_unit_good`                     | Unit Good              | 1 / 0 |
| `arduino_helium_high_temperature_alarm` | Helium High Temp       | 1 / 0 |
| `arduino_helium_high_pressure_alarm`    | Helium High Pressure   | 1 / 0 |
| `arduino_battery_voltage_volts`         | Battery Voltage        | volts |
| `arduino_compressor_relay_state`        | Compressor (ON/OFF)    | ON = 1, OFF = 0 |
| `arduino_monitoring_state`              | Monitoring (ON/OFF)    | ON = 1, OFF = 0 |
| `arduino_metrics_age_seconds`           | -                      | seconds since the file was written (added by the server; if it keeps growing, the parser has stopped) |

If a new row appears on an Arduino page later, it is exported too, as
`arduino_<row name>` (for example `Coolant Flow` -> `arduino_coolant_flow`).

Example of `http://localhost:9102/arduino_192.168.1.50`:

```
# Arduino exporter - written by get_arduino_metrics.py
# source: http://192.168.1.50/
# time: 2026-10-12 15:01:02
# device: Newton
# status: OK - 7 parameters read
# HELP arduino_up 1 if the Arduino page was read on the last run, 0 if not
# TYPE arduino_up gauge
arduino_up{device="Newton",ip="192.168.1.50"} 1
...
arduino_battery_voltage_volts{device="Newton",ip="192.168.1.50"} 3.9
arduino_compressor_relay_state{device="Newton",ip="192.168.1.50"} 1
arduino_monitoring_state{device="Newton",ip="192.168.1.50"} 0
...
arduino_metrics_age_seconds{device="Newton",ip="192.168.1.50"} 4.2
```


## Installation

### 1. Copy this folder to the Ubuntu PC

For example to `~/arduino_exporter_ubuntu`, as the user who should run the exporter.

### 2. Run the setup

```bash
cd ~/arduino_exporter_ubuntu
sudo bash initial_setup.sh
```

The script:

1. Stops the services of an earlier setup (so it can be run again)
2. Sets up Python with flask and waitress, if this was not done before
   (`python_installer/install_python_and_packages.sh`, see below)
3. Asks for the IP addresses of the Arduinos, reads every page once to test them,
   and saves them in `arduino_ips.txt`
4. Checks that port 9102 is free (asks for another port if it is not)
5. Installs and enables the systemd services `arduino-parser` and `arduino-exporter`
6. Opens the port in the `ufw` firewall
7. Starts both services, checks every route, and prints the Prometheus config

The answers can also be given on the command line:

```bash
sudo bash initial_setup.sh --port 9102 192.168.1.50 192.168.1.51
```

### 3. Add the Arduinos to Prometheus

In `prometheus.yml`, under `scrape_configs:`, one job per Arduino (the setup prints
these lines for your addresses):

```yaml
  - job_name: arduino_192.168.1.50
    metrics_path: /arduino_192.168.1.50
    static_configs:
      - targets: ['<IP of this PC>:9102']
  - job_name: arduino_192.168.1.51
    metrics_path: /arduino_192.168.1.51
    static_configs:
      - targets: ['<IP of this PC>:9102']
```


## Python installer (offline)

`python_installer/install_python_and_packages.sh` makes the Python environment
`venv/` of the exporter. The setup runs it for you; it needs no administrator rights
and no internet:

1. It uses `python3` of Ubuntu if it is version 3.10 or newer (Ubuntu 22.04 and later).
   The Ubuntu package `python3-venv` is not needed.
2. Otherwise (Python missing, too old, or broken) it unpacks the standalone
   Python 3.13 of this folder to `python/` and uses that one.
3. flask, waitress and their dependencies are installed from `wheels/` (for Python
   3.10 - 3.14, x86_64 and ARM). Only if this fails, they are downloaded from pypi.org.

Options: `--bundled` always uses the standalone Python; `--python PATH` uses another Python.


## Adding or removing an Arduino

Edit `arduino_ips.txt` (one IP per line). The change is used within 10 seconds,
no restart needed: a new Arduino gets its route, a removed one loses it.
Or run `sudo bash initial_setup.sh` again. Add or remove the Prometheus job too.


## Managing the services

```bash
systemctl status arduino-parser arduino-exporter       # state
sudo systemctl restart arduino-parser arduino-exporter # restart
sudo systemctl stop arduino-parser arduino-exporter    # stop (they start again at the next boot)
journalctl -u arduino-parser -u arduino-exporter -f    # messages (Ctrl+C to quit)
```

To remove everything:

```bash
sudo systemctl disable --now arduino-parser arduino-exporter
sudo rm /etc/systemd/system/arduino-parser.service /etc/systemd/system/arduino-exporter.service
sudo systemctl daemon-reload
sudo ufw delete allow 9102/tcp
```


## Troubleshooting

- `curl http://localhost:9102/` lists the routes. `curl http://localhost:9102/arduino_<IP>` -
  the `# status:` line at the top shows the result of the last read, for example
  `ERROR - TimeoutError: timed out`.
- Read all Arduinos once by hand: `venv/bin/python get_arduino_metrics.py --once`
  (or give addresses: `... --once 192.168.1.50`)
- `arduino_up` is 0: this PC cannot read that Arduino page (wrong IP, Arduino off, network).
- `arduino_metrics_age_seconds` keeps growing: the `arduino-parser` service does not run
  (see `journalctl -u arduino-parser`).
- After an Ubuntu upgrade (e.g. 24.04 -> 26.04), or after moving this folder, run
  `sudo bash initial_setup.sh` again: it rebuilds the Python environment and the services.
