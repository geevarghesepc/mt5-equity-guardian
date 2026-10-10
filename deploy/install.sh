#!/usr/bin/env bash
# Install the MT5 Guardian inside a Docker container running MetaTrader under Wine.
#
# Inside the container (drive_c):
#   bash deploy/install.sh
#
# From the host:
#   CONTAINER=your-container bash deploy/install.sh
#
# After a git pull:
#   bash deploy/install.sh
#
# This script:
#   - Ensures Wine is 10.1+ (numpy on Python 3.14 needs crealf).
#   - Installs tmux for background sessions.
#   - Installs Windows Python 3.14 inside the Wine prefix if missing.
#   - Installs requirements.txt.
#   - Copies the project tree into drive_c if not already there.
#   - Does not start the guardian.
#   - Does not print the account password.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC=$(cd "$SCRIPT_DIR/.." && pwd)

WINEPREFIX="${WINEPREFIX:-/config/.wine}"
WINE_USER="${WINE_USER:-abc}"
PY_EXE="${PY_EXE:-C:/Python314/python.exe}"
PY_VERSION="${PY_VERSION:-3.14.0}"
PY_INSTALLER_URL="https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-amd64.exe"

usage() {
  echo "Usage:"
  echo "  Inside the container:  bash $0"
  echo "  From the host:         CONTAINER=<name> bash $0"
  echo ""
  echo "Environment:"
  echo "  WINEPREFIX    Wine prefix (default: /config/.wine)"
  echo "  WINE_USER     Wine user (default: abc)"
  echo "  PY_EXE        Windows Python path (default: C:/Python314/python.exe)"
  echo "  CONTAINER     Docker container name (host-side deployment)"
  exit 0
}

run_as_abc() {
  su -s /bin/bash "$WINE_USER" -c "export WINEPREFIX=$(printf '%q' "$WINEPREFIX"); wine $*"
}

windows_python_installed() {
  run_as_abc "$PY_EXE" --version >/dev/null 2>&1
}

ensure_windows_python() {
  if windows_python_installed; then
    echo "Python is already installed at $PY_EXE"
    run_as_abc "$PY_EXE" --version
    return
  fi
  echo "Installing Python $PY_VERSION"
  local download="/tmp/python-installer.exe"
  if command -v wget >/dev/null 2>&1; then
    wget -q -O "$download" "$PY_INSTALLER_URL"
  elif command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$download" "$PY_INSTALLER_URL"
  else
    echo "wget or curl is required to download Python." >&2
    exit 1
  fi
  chown "$WINE_USER:$WINE_USER" "$download"
  local win_download
  win_download=$(winepath -w "$download" 2>/dev/null || echo "Z:${download}")
  run_as_abc "$win_download" /quiet InstallAllUsers=1 TargetDir=C:\\Python314 PrependPath=1
  sleep 2
  if ! windows_python_installed; then
    echo "Python installer finished but ${PY_EXE} is missing." >&2
    exit 1
  fi
  run_as_abc "$PY_EXE" --version
  run_as_abc "$PY_EXE" -m pip install --upgrade pip
  run_as_abc "$PY_EXE" -m pip install MetaTrader5
  run_as_abc "$PY_EXE" -m pip list
  rm -f "$download"
}

same_tree() {
  local left right
  left=$(cd "$1" && pwd -P)
  right=$(cd "$2" && pwd -P)
  [[ "$left" == "$right" ]]
}

note_login_file() {
  local dest=$1
  if [[ -f "$dest/config.yaml" ]]; then
    chmod 600 "$dest/config.yaml"
    echo "config.yaml is in the container. The password was not printed."
  else
    echo "No config.yaml. Put the login in that file, or export MT5_LOGIN, MT5_PASSWORD, and MT5_SERVER."
  fi
}

drive_c() {
  cd "${WINEPREFIX}/drive_c" && pwd -P
}

on_drive_c() {
  local here drive
  here=$(cd "$1" && pwd -P)
  drive=$(drive_c)
  [[ "$here" == "$drive" || "$here" == "$drive"/* ]]
}

win_path() {
  local here drive
  here=$(cd "$1" && pwd -P)
  drive=$(drive_c)
  echo "C:/${here#"$drive"/}"
}

copy_tree() {
  local dest=$1
  mkdir -p "$dest"
  if same_tree "$SRC" "$dest"; then
    echo "The guardian is already at $dest. Leaving those files in place."
    note_login_file "$dest"
    return
  fi
  set +e
  set +o pipefail
  tar -C "$SRC" \
    --exclude=state \
    --exclude=logs \
    --exclude=.git \
    --exclude=.pytest_cache \
    --exclude=__pycache__ \
    --exclude='*.pyc' \
    --exclude=venv \
    -cf - . | tar -C "$dest" -xf -
  local read_status=${PIPESTATUS[0]} write_status=${PIPESTATUS[1]}
  set -o pipefail
  set -e
  if [[ "$write_status" -ne 0 || "$read_status" -gt 1 ]]; then
    echo "Copying the guardian failed." >&2
    exit 1
  fi
  note_login_file "$dest"
}

wine_release() {
  wine --version 2>/dev/null | sed -n 's/^wine-\([0-9][0-9.]*\).*/\1/p' | head -n 1
}

wine_supports_crealf() {
  local current
  current=$(wine_release)
  [[ -n "$current" ]] || return 1
  printf '%s\n%s\n' "10.1" "$current" | sort -V -C
}

detect_distro() {
  local os_id="debian"
  local codename="bookworm"
  if [[ -f /etc/os-release ]]; then
    # shellcheck source=/dev/null
    . /etc/os-release
    os_id="${ID:-debian}"
    codename="${VERSION_CODENAME:-${UBUNTU_CODENAME:-bookworm}}"
  fi
  echo "$os_id" "$codename"
}

fix_winehq_apt() {
  if [[ "$(id -u)" -ne 0 ]] || ! command -v apt-get >/dev/null 2>&1; then
    echo "Root and apt-get are required to prepare Wine." >&2
    exit 1
  fi

  read -r os_id codename < <(detect_distro)
  local wine_uri="https://dl.winehq.org/wine-builds/${os_id}"

  mkdir -pm755 /etc/apt/keyrings
  echo "Installing the WineHQ signing key"
  rm -f /etc/apt/keyrings/winehq-archive.gpg
  if command -v wget >/dev/null 2>&1; then
    wget -qO- https://dl.winehq.org/wine-builds/winehq.key | gpg --batch --dearmor -o /etc/apt/keyrings/winehq-archive.gpg
  elif command -v curl >/dev/null 2>&1; then
    curl -fsSL https://dl.winehq.org/wine-builds/winehq.key | gpg --batch --dearmor -o /etc/apt/keyrings/winehq-archive.gpg
  else
    echo "wget or curl is required to download the WineHQ key." >&2
    exit 1
  fi
  chmod 644 /etc/apt/keyrings/winehq-archive.gpg

  cat > "/etc/apt/sources.list.d/winehq-${codename}.sources" <<EOF
Types: deb
URIs: ${wine_uri}
Suites: ${codename}
Components: main
Architectures: amd64 i386
Signed-By: /etc/apt/keyrings/winehq-archive.gpg
EOF

  dpkg --add-architecture i386
  apt-get update
}

ensure_wine() {
  if command -v wine >/dev/null 2>&1; then
    if wine_supports_crealf; then
      echo "Wine is already installed and meets version requirement (>= 10.1): $(wine --version)"
      return 0
    fi
    echo "Wine is installed ($(wine --version)) but < 10.1. Upgrading to winehq-stable..."
  else
    echo "Wine is not installed. Installing winehq-stable..."
  fi

  fix_winehq_apt
  set +e
  apt-get install -y -o Dpkg::Options::="--force-overwrite" winehq-stable
  local status=$?
  set -e
  if [[ "$status" -ne 0 ]] || ! command -v wine >/dev/null 2>&1; then
    echo "Failed to install Wine." >&2
    exit 1
  fi
  if ! wine_supports_crealf; then
    echo "Wine installed ($(wine --version)) does not satisfy >= 10.1 requirement." >&2
    exit 1
  fi
  echo "Wine installed: $(wine --version)"
}

ensure_tmux() {
  if command -v tmux >/dev/null 2>&1; then
    return
  fi
  echo "Installing tmux"
  if ! apt-get install -y tmux; then
    echo "tmux was not installed. The guardian stops when this terminal closes."
  fi
}

install_inside() {
  if [[ ! -d "$WINEPREFIX" ]]; then
    echo "Wine prefix not found at $WINEPREFIX. Open the MetaTrader container first." >&2
    exit 1
  fi
  if ! id "$WINE_USER" >/dev/null 2>&1; then
    echo "User $WINE_USER does not exist." >&2
    exit 1
  fi

  ensure_wine
  ensure_tmux
  ensure_windows_python

  local dest="$SRC"
  if on_drive_c "$SRC"; then
    echo "Using the clone at $dest"
    note_login_file "$dest"
  else
    dest="$(drive_c)/guardian"
    echo "Copying the guardian to $dest"
    copy_tree "$dest"
  fi
  chown -R "$WINE_USER:$WINE_USER" "$dest"
  local app_win
  app_win=$(win_path "$dest")

  echo "Checking $PY_EXE"
  run_as_abc "$PY_EXE" --version

  echo "Installing requirements.txt"
  run_as_abc "$PY_EXE" -m pip install -r "${app_win}/requirements.txt"

  echo "Checking imports"
  if ! run_as_abc "$PY_EXE" -c "import MetaTrader5, yaml, requests; print('imports ok')"; then
    echo "The import check failed. Wine is $(wine --version 2>/dev/null || echo missing)." >&2
    echo "Open the MetaTrader window and confirm it still logs in." >&2
    exit 1
  fi

  local unix_app="$dest"
  echo
  echo "Installed."
  echo "Open the MetaTrader window again after this script finishes."
  echo "Confirm the account is logged in. This script does not start the guardian."
  echo "Health check, no orders:"
  echo "  su -s /bin/bash $WINE_USER -c \"cd '$unix_app' && WINEPREFIX=$WINEPREFIX wine $PY_EXE health.py\""
  echo "Start the guardian in tmux:"
  echo "  bash '$unix_app/deploy/run.sh'"
}

deploy_from_host() {
  local container=$1
  local stage=/tmp/guardian-src
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker is not on PATH. Run this inside the container with --inside." >&2
    exit 1
  fi
  echo "Copying $SRC into $container:$stage"
  docker exec "$container" rm -rf "$stage"
  docker exec "$container" mkdir -p "$stage"
  tar -C "$SRC" \
    --exclude=state \
    --exclude=logs \
    --exclude=.git \
    --exclude=.pytest_cache \
    --exclude=__pycache__ \
    --exclude='*.pyc' \
    --exclude=venv \
    -cf - . | docker exec -i "$container" tar -C "$stage" -xf -
  docker exec -u root -e "WINEPREFIX=$WINEPREFIX" -e "WINE_USER=$WINE_USER" \
    "$container" bash "$stage/deploy/install.sh" --inside
  docker exec "$container" rm -rf "$stage"
}

if [[ "${1:-}" == "--inside" ]]; then
  install_inside
elif [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
elif [[ -n "${CONTAINER:-}" ]]; then
  deploy_from_host "$CONTAINER"
elif [[ -d /config/.wine ]]; then
  install_inside
else
  usage
fi
