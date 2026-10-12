#!/usr/bin/env bash
# ============================================================
#  Offline Python + packages installer (Ubuntu)
#
#  Creates the Python environment of the Arduino exporter:
#      <exporter folder>/venv      with flask and waitress
#
#  Which Python is used:
#    1) python3 of Ubuntu, if it is version 3.10 or newer and works;
#    2) otherwise the standalone Python in this folder
#       (cpython-...-linux-gnu-....tar.gz, needs nothing from the
#       system), unpacked to <exporter folder>/python
#  The packages come from the wheels/ folder, so no internet is
#  needed. Only if that fails, they are downloaded from pypi.org.
#  The Ubuntu package python3-venv is not needed.
#
#  Required files in this folder:
#    1) wheels/                    .whl files of pip, flask, waitress
#                                  and all their dependencies
#    2) cpython-*-x86_64-unknown-linux-gnu-*.tar.gz   (standalone
#                                  Python from python-build-standalone)
#
#  No administrator rights are needed. Run it as the user that runs
#  the exporter (initial_setup.sh does this for you):
#
#      bash install_python_and_packages.sh [--bundled] [--python PATH]
#
#        --bundled       always use the standalone Python of this folder
#        --python PATH   use this Python instead of python3 of Ubuntu
# ============================================================
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$HERE")"
VENV="$APP_DIR/venv"
BUNDLED_DIR="$APP_DIR/python"
WHEELS="$HERE/wheels"
PACKAGES=(pip flask waitress)
MIN_MINOR=10                    # the wheels in this folder need Python 3.10 or newer

USE_BUNDLED=0
SYSTEM_PYTHON="python3"
while [ $# -gt 0 ]; do
    case "$1" in
        --bundled) USE_BUNDLED=1 ;;
        --python)  SYSTEM_PYTHON="${2:-}"; shift ;;
        *) echo "Unknown option: $1"; exit 2 ;;
    esac
    shift
done

shopt -s nullglob
PIP_WHEELS=("$WHEELS"/pip-*.whl)
PIP_WHEEL="${PIP_WHEELS[0]:-}"

# --- helpers -----------------------------------------------------------------

is_new_enough() {               # $1 = python: is it Python 3.10 or newer?
    "$1" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_MINOR) else 1)" >/dev/null 2>&1
}

install_packages() {            # into $VENV
    local py="$VENV/bin/python"
    if [ -n "$PIP_WHEEL" ]; then
        # pip runs directly from its wheel file, so the Python needs no pip of its own
        echo "  installing ${PACKAGES[*]} from the wheels folder ..."
        if "$py" "$PIP_WHEEL/pip" install --quiet --no-cache-dir --disable-pip-version-check \
                --no-index --find-links "$WHEELS" "${PACKAGES[@]}"; then
            return 0
        fi
        echo "  the wheels folder did not work - trying the internet (pypi.org) ..."
        "$py" "$PIP_WHEEL/pip" install --quiet --no-cache-dir --disable-pip-version-check "${PACKAGES[@]}"
    else
        echo "  no pip wheel in $WHEELS - trying the internet (pypi.org) ..."
        "$py" -m ensurepip >/dev/null 2>&1 &&
            "$py" -m pip install --quiet --no-cache-dir --disable-pip-version-check "${PACKAGES[@]}"
    fi
}

try_python() {                  # $1 = Python to build the environment from
    echo "Using $("$1" --version 2>&1) ($1)"
    rm -rf "$VENV"
    if ! "$1" -m venv --without-pip "$VENV"; then
        echo "  could not create $VENV"
        return 1
    fi
    if ! install_packages; then
        echo "  could not install the packages"
        return 1
    fi
    if ! "$VENV/bin/python" -c "import flask, waitress" >/dev/null 2>&1; then
        echo "  flask/waitress do not work in $VENV"
        return 1
    fi
}

unpack_bundled() {              # standalone Python -> $BUNDLED_DIR
    local tarballs=("$HERE"/cpython-*-"$(uname -m)"-unknown-linux-gnu-*.tar.gz)
    if [ "${#tarballs[@]}" -eq 0 ]; then
        echo "No standalone Python for this processor ($(uname -m)) in $HERE"
        return 1
    fi
    if [ -x "$BUNDLED_DIR/bin/python3" ] && "$BUNDLED_DIR/bin/python3" -c "pass" 2>/dev/null; then
        return 0                # unpacked before
    fi
    echo "Unpacking $(basename "${tarballs[0]}") to $BUNDLED_DIR ..."
    rm -rf "$BUNDLED_DIR" "$BUNDLED_DIR.unpack"
    if mkdir -p "$BUNDLED_DIR.unpack" &&
            tar -xzf "${tarballs[0]}" -C "$BUNDLED_DIR.unpack" &&
            mv "$BUNDLED_DIR.unpack/python" "$BUNDLED_DIR"; then
        rm -rf "$BUNDLED_DIR.unpack"
    else
        echo "  unpacking failed"
        rm -rf "$BUNDLED_DIR.unpack"
        return 1
    fi
}

# --- main --------------------------------------------------------------------

echo "============================================================"
echo " Python for the Arduino exporter"
echo "============================================================"

READY=0
if [ "$USE_BUNDLED" = 0 ]; then
    if ! command -v "$SYSTEM_PYTHON" >/dev/null 2>&1; then
        echo "$SYSTEM_PYTHON was not found."
    elif ! is_new_enough "$SYSTEM_PYTHON"; then
        echo "$("$SYSTEM_PYTHON" --version 2>&1) is too old (3.$MIN_MINOR or newer is needed)."
    elif try_python "$(command -v "$SYSTEM_PYTHON")"; then
        READY=1
    fi
    [ "$READY" = 1 ] || echo "Falling back to the standalone Python of this folder."
fi

if [ "$READY" = 0 ] && unpack_bundled && try_python "$BUNDLED_DIR/bin/python3"; then
    READY=1
fi

if [ "$READY" = 0 ]; then
    rm -rf "$VENV"
    echo
    echo "ERROR: could not set up Python with flask and waitress."
    echo "Check that this folder is complete (wheels/ and the cpython-...tar.gz file),"
    echo "or connect the PC to the internet, then run this installer"
    echo "(or initial_setup.sh) again."
    exit 1
fi

echo
"$VENV/bin/python" -c "import sys; from importlib.metadata import version as v; print('Ready: Python', sys.version.split()[0], '| flask', v('flask'), '| waitress', v('waitress'))"
echo "Environment: $VENV"
