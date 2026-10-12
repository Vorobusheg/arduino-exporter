#!/usr/bin/env bash
# ============================================================
#  Arduino exporter (Ubuntu) - setup
#  Run from this folder AS ROOT:
#
#      sudo bash initial_setup.sh [--port PORT] [IP ...]
#
#  The IP addresses can be given here, or typed in when asked.
#  --port sets the exporter port (default: 9102, or the port of the
#  earlier setup). Can be run again later, e.g. to change the list.
#
#  This script:
#    1) Stops the services of an earlier setup.
#    2) Sets up Python with flask and waitress (python_installer/),
#       if it is not ready yet.
#    3) Asks for the IP addresses of the Arduino web servers, reads
#       every page once to test it, and saves the list in
#       arduino_ips.txt. The list can also be edited later: changes
#       are used within 10 seconds, no restart needed.
#    4) Checks that TCP port 9102 is free, or asks for another port.
#    5) Installs two systemd services. They start at PC boot, before
#       any user logs in, are restarted whenever they stop, have no
#       time limit, and run as the owner of this folder:
#         - arduino-parser    -> get_arduino_metrics.py
#         - arduino-exporter  -> export_arduino_metrics.py
#    6) Opens the port in the firewall (ufw), if ufw is installed.
#    7) Starts both services and checks that the metrics are served.
# ============================================================
set -u

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR" || exit 1
PY="$APP_DIR/venv/bin/python"
IPS_FILE="$APP_DIR/arduino_ips.txt"
PARSER_UNIT="arduino-parser.service"
EXPORTER_UNIT="arduino-exporter.service"
UNIT_DIR="/etc/systemd/system"
DEFAULT_PORT=9102

fail() {
    echo
    echo "ERROR: $*"
    exit 1
}

REQUESTED_PORT=""
ARG_IPS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --port)    REQUESTED_PORT="${2:-}"; shift ;;
        --port=*)  REQUESTED_PORT="${1#--port=}" ;;
        *)         ARG_IPS+=("$1") ;;
    esac
    shift
done

# --- require root and systemd ---
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: this script must be run as root:"
    echo "    sudo bash $0"
    exit 1
fi
command -v systemctl >/dev/null 2>&1 || fail "systemd (systemctl) was not found."

# The services run as the owner of this folder, so every file in it
# stays editable by that user.
SERVICE_USER="$(stat -c %U "$APP_DIR")"
SERVICE_GROUP="$(stat -c %G "$APP_DIR")"
id -u "$SERVICE_USER" >/dev/null 2>&1 ||
    fail "the owner of $APP_DIR is not a user of this PC. Copy the folder to your home folder."

as_user() {                     # run a command as the service user
    if [ "$SERVICE_USER" = "root" ]; then "$@"; else runuser -u "$SERVICE_USER" -- "$@"; fi
}

echo
echo "============================================================"
echo " Arduino exporter (Ubuntu) - setup"
echo "============================================================"
echo " Folder:            $APP_DIR"
echo " Services run as:   $SERVICE_USER"
echo

# --- 1) stop the services of an earlier setup ---
echo "[1/7] Stopping the services of an earlier setup, if any ..."
systemctl stop "$PARSER_UNIT" "$EXPORTER_UNIT" >/dev/null 2>&1
OLD_PORT="$(sed -n 's/.*--port \([0-9][0-9]*\).*/\1/p' "$UNIT_DIR/$EXPORTER_UNIT" 2>/dev/null | head -n 1)"

# --- 2) Python with flask and waitress ---
echo
echo "[2/7] Checking Python ..."
if as_user "$PY" -c "import flask, waitress" >/dev/null 2>&1; then
    echo "      Ready: $(as_user "$PY" --version 2>&1) with flask and waitress ($APP_DIR/venv)"
else
    echo "      Python with flask and waitress is not set up yet - running the installer:"
    echo
    as_user bash "$APP_DIR/python_installer/install_python_and_packages.sh" ||
        fail "Python could not be set up (see the messages above)."
fi

# --- 3) Arduino IP addresses ---
echo
echo "[3/7] IP addresses of the Arduino web servers"
CURRENT_IPS=""
if [ -f "$IPS_FILE" ]; then
    CURRENT_IPS="$(sed 's/#.*//' "$IPS_FILE" | tr -s ',;[:space:]' ' ' | sed 's/^ *//; s/ *$//')"
fi
ANSWER_IPS="${ARG_IPS[*]}"      # addresses from the command line, if any
while true; do
    if [ -z "$ANSWER_IPS" ]; then
        echo "      Enter the addresses you open in the browser, separated by spaces,"
        echo "      for example:  192.168.1.50 192.168.1.51"
        if [ -n "$CURRENT_IPS" ]; then
            read -r -p "Arduino IPs [$CURRENT_IPS]: " ANSWER_IPS || fail "no answer"
            [ -n "$ANSWER_IPS" ] || ANSWER_IPS="$CURRENT_IPS"
        else
            read -r -p "Arduino IPs: " ANSWER_IPS || fail "no answer"
        fi
    fi
    # commas, semicolons and quotes are allowed between the addresses
    read -r -a IP_LIST <<< "$(echo "$ANSWER_IPS" | tr ',;"'"'" '    ')"
    if [ "${#IP_LIST[@]}" -eq 0 ]; then
        echo "      No IP entered."
        ANSWER_IPS=""
        continue
    fi

    echo
    echo "Reading every Arduino page once to test the addresses ..."
    echo
    if as_user "$PY" "$APP_DIR/get_arduino_metrics.py" --once "${IP_LIST[@]}"; then
        break
    fi
    echo "WARNING: not every Arduino page could be read (see the Status lines above)."
    read -r -p "Keep these addresses anyway? [y/N] " KEEP || fail "no answer"
    case "$KEEP" in
        [yY]*) break ;;
    esac
    ANSWER_IPS=""
done

{
    echo "# Arduino web servers read by the Arduino exporter: one address per line"
    echo "# (IP, or IP:port). Lines starting with # are ignored. Changes are used"
    echo "# within 10 seconds, no restart needed."
    printf '%s\n' "${IP_LIST[@]}"
} > "$IPS_FILE"
chown "$SERVICE_USER:$SERVICE_GROUP" "$IPS_FILE"
echo "      Saved in $IPS_FILE"

# --- 4) exporter port ---
echo
echo "[4/7] Checking the port for the exporter ..."
port_problem() {                # prints why port $1 cannot be used; nothing if it is fine
    if ! [[ "$1" =~ ^[0-9]{1,5}$ ]] || [ "$((10#$1))" -lt 1 ] || [ "$((10#$1))" -gt 65535 ]; then
        echo "is not a valid port number"
    elif [ "$((10#$1))" -lt 1024 ] && [ "$SERVICE_USER" != "root" ]; then
        echo "is below 1024, which only root may use"
    elif ! "$PY" -c "
import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(('0.0.0.0', int(sys.argv[1])))
except OSError:
    sys.exit(1)
" "$((10#$1))" 2>/dev/null; then
        echo "is already used by another program on this PC"
    fi
}
PORT="${REQUESTED_PORT:-${OLD_PORT:-$DEFAULT_PORT}}"
while true; do
    PROBLEM="$(port_problem "$PORT")"
    [ -z "$PROBLEM" ] && break
    echo "      Port $PORT $PROBLEM."
    if [[ "$PORT" =~ ^[0-9]{1,5}$ ]] && [ "$((10#$PORT))" -lt 65535 ]; then
        SUGGESTED=$((10#$PORT + 1))
    else
        SUGGESTED=$((DEFAULT_PORT + 1))
    fi
    read -r -p "Port for the Arduino exporter [$SUGGESTED]: " ANSWER_PORT || fail "no answer"
    PORT="${ANSWER_PORT//[[:space:]]/}"
    [ -n "$PORT" ] || PORT="$SUGGESTED"
done
PORT=$((10#$PORT))
echo "      Using port $PORT."

# --- 5) systemd services ---
echo
echo "[5/7] Installing the systemd services $PARSER_UNIT and $EXPORTER_UNIT ..."
UNIT_APP_DIR="${APP_DIR//%/%%}"         # "%" has a special meaning in unit files
USER_LINES=""
if [ "$SERVICE_USER" != "root" ]; then
    USER_LINES="User=$SERVICE_USER
Group=$SERVICE_GROUP"
fi

cat > "$UNIT_DIR/$PARSER_UNIT" <<EOF
# Created by $UNIT_APP_DIR/initial_setup.sh
[Unit]
Description=Arduino exporter: reads the Arduino web pages (get_arduino_metrics.py)
Wants=network-online.target
After=network-online.target
# never give up restarting
StartLimitIntervalSec=0

[Service]
Type=simple
$USER_LINES
WorkingDirectory=$UNIT_APP_DIR
ExecStart="$UNIT_APP_DIR/venv/bin/python" -u "$UNIT_APP_DIR/get_arduino_metrics.py"
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

cat > "$UNIT_DIR/$EXPORTER_UNIT" <<EOF
# Created by $UNIT_APP_DIR/initial_setup.sh
[Unit]
Description=Arduino exporter: serves the Arduino metrics on port $PORT (export_arduino_metrics.py)
After=network.target
# never give up restarting
StartLimitIntervalSec=0

[Service]
Type=simple
$USER_LINES
WorkingDirectory=$UNIT_APP_DIR
ExecStart="$UNIT_APP_DIR/venv/bin/python" -u "$UNIT_APP_DIR/export_arduino_metrics.py" --port $PORT
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload || fail "systemctl daemon-reload failed."
systemctl enable "$PARSER_UNIT" "$EXPORTER_UNIT" >/dev/null 2>&1 ||
    fail "could not enable the services."
echo "      Done (they start at every boot)."

# --- 6) firewall ---
echo
echo "[6/7] Firewall ..."
if command -v ufw >/dev/null 2>&1; then
    if [ -n "$OLD_PORT" ] && [ "$OLD_PORT" != "$PORT" ]; then
        ufw delete allow "$OLD_PORT/tcp" >/dev/null 2>&1
    fi
    if ufw allow "$PORT/tcp" comment "Arduino exporter" >/dev/null 2>&1; then
        echo "      ufw: TCP port $PORT is allowed."
        if grep -q '^ENABLED=no' /etc/ufw/ufw.conf 2>/dev/null; then
            echo "      (ufw is not active on this PC; the rule is kept for when it is.)"
        fi
    else
        echo "      WARNING: could not add the ufw rule for port $PORT."
    fi
else
    echo "      ufw is not installed - nothing to do."
fi

# --- 7) start and check ---
echo
echo "[7/7] Starting the services ..."
systemctl restart "$PARSER_UNIT" "$EXPORTER_UNIT" || fail "could not start the services."

mapfile -t ROUTES < <(as_user "$PY" "$APP_DIR/get_arduino_metrics.py" --routes 2>/dev/null)
CHECK_FAILED=0
for ROUTE in "${ROUTES[@]}"; do
    if "$PY" -c "
import sys, time, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # no proxy for localhost
for _ in range(20):
    try:
        with opener.open(sys.argv[1], timeout=3) as response:
            if response.status == 200:
                sys.exit(0)
    except Exception:
        pass
    time.sleep(1)
sys.exit(1)
" "http://127.0.0.1:$PORT$ROUTE"; then
        echo "      OK         http://localhost:$PORT$ROUTE"
    else
        echo "      NO ANSWER  http://localhost:$PORT$ROUTE"
        CHECK_FAILED=1
    fi
done

THIS_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
THIS_IP="${THIS_IP:-<IP of this PC>}"

echo
echo "============================================================"
if [ "$CHECK_FAILED" = 0 ]; then
    echo " Setup complete."
else
    echo " Setup complete, but the check failed - see the logs:"
    echo "     journalctl -u arduino-parser -u arduino-exporter -n 50"
fi
echo "------------------------------------------------------------"
echo " Arduinos : ${IP_LIST[*]}"
echo "            (list: $IPS_FILE)"
echo " Port     : $PORT  (firewall: ufw rule for TCP $PORT)"
echo " Services : arduino-parser, arduino-exporter"
echo "            (start at PC boot, restart always, run as $SERVICE_USER)"
echo " Logs     : journalctl -u arduino-parser -u arduino-exporter -f"
echo " Routes   : http://$THIS_IP:$PORT/   lists them all"
echo "------------------------------------------------------------"
echo " Prometheus scrape config (prometheus.yml, under scrape_configs);"
echo " use the address of this PC that the Prometheus server can reach:"
for ROUTE in "${ROUTES[@]}"; do
    echo "   - job_name: ${ROUTE#/}"
    echo "     metrics_path: $ROUTE"
    echo "     static_configs:"
    echo "       - targets: ['$THIS_IP:$PORT']"
done
echo "============================================================"
