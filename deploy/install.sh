#!/usr/bin/env bash
# Install the MT5 Guardian inside a Docker container running MetaTrader under Wine.
# Repairs the WineHQ apt key, upgrades Wine to the stable build that provides
# ucrtbase.crealf, installs tmux, stops any running wineserver to prevent version
# mismatches, and installs Windows Python 3.14 when missing.
#
# Inside the container, on Wine drive C:
#   cd /config/.wine/drive_c
#   git clone https://github.com/geevarghesepc/mt5-equity-guardian.git guardian
#   cd guardian
#   bash deploy/install.sh
#
# Later, from that same folder:
#   git pull
#   bash deploy/install.sh
#
# From the host:
#   CONTAINER=your-container bash deploy/install.sh

set -euo pipefail

WINEPREFIX="${WINEPREFIX:-/config/.wine}"
WINE_USER="${WINE_USER:-abc}"
PY_EXE="${PY_EXE:-C:/Python314/python.exe}"
PY_VERSION="${PY_VERSION:-3.14.0}"

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC=$(cd "$SCRIPT_DIR/.." && pwd)

usage() {
  echo "Usage: CONTAINER=<name> bash deploy/install.sh" >&2
  echo "   or, inside the container: bash deploy/install.sh --inside" >&2
  echo ""
  echo "Environment:"
  echo "  WINEPREFIX    Wine prefix (default: /config/.wine)"
  echo "  WINE_USER     Wine user (default: abc)"
  echo "  PY_EXE        Windows Python path (default: C:/Python314/python.exe)"
  echo "  CONTAINER     Docker container name (host-side deployment)"
  exit 2
}

run_as_abc() {
  if [[ "$(id -un)" == "$WINE_USER" ]]; then
    WINEPREFIX="$WINEPREFIX" wine "$@"
    return
  fi
  local quoted
  quoted=$(printf '%q ' "$@")
  su -s /bin/bash "$WINE_USER" -c "export WINEPREFIX=$(printf '%q' "$WINEPREFIX"); wine ${quoted}"
}

windows_python_installed() {
  [[ -f "${WINEPREFIX}/drive_c/Python314/python.exe" ]]
}

ensure_windows_python() {
  if windows_python_installed; then
    echo "Windows Python is already at ${PY_EXE}"
    return
  fi
  local installer="python-${PY_VERSION}-amd64.exe"
  local url="https://www.python.org/ftp/python/${PY_VERSION}/${installer}"
  # A leftover installer in /tmp is often owned by abc and not writable, and /tmp
  # may be noexec. Put this copy on drive C, where the Wine user can read it natively.
  local download="${WINEPREFIX}/drive_c/${installer}"
  rm -f "/tmp/${installer}" "$download" || true
  echo "Downloading ${url}"
  if command -v wget >/dev/null 2>&1; then
    wget -O "$download" "$url"
  elif command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$download" "$url"
  else
    echo "wget or curl is required to download Python." >&2
    exit 1
  fi
  chown "${WINE_USER}:${WINE_USER}" "$download"
  chmod 644 "$download"
  echo "Installing Python ${PY_VERSION} into C:\\Python314"
  echo "If a Wine Mono window is open, click Cancel. Python does not need it."
  # mscoree is the Mono prompt. Leaving it enabled blocks this quiet install on the desktop.
  local quiet_install
  quiet_install=$(printf '%q ' "C:/${installer}" /quiet InstallAllUsers=1 PrependPath=1 'TargetDir=C:\Python314')
  if [[ "$(id -un)" == "$WINE_USER" ]]; then
    WINEPREFIX="$WINEPREFIX" WINEDLLOVERRIDES="mscoree=d" wine "C:/${installer}" /quiet InstallAllUsers=1 PrependPath=1 'TargetDir=C:\Python314'
  else
    su -s /bin/bash "$WINE_USER" -c "export WINEPREFIX=$(printf '%q' "$WINEPREFIX"); export WINEDLLOVERRIDES=mscoree=d; wine ${quiet_install}"
  fi
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
  elif [[ -f "$dest/config.local.yaml" ]]; then
    chmod 600 "$dest/config.local.yaml"
    echo "config.local.yaml is in the container. The password was not printed."
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
  # Exit 1 from the reader means a file changed during the read. Exit 2 is a real failure.
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

check_wine_working() {
  su -s /bin/bash "$WINE_USER" -c "export WINEPREFIX=$(printf '%q' "$WINEPREFIX"); wine cmd.exe /c exit 0" >/dev/null 2>&1
}

stop_and_upgrade_wineserver() {
  echo "Stopping the Wine server that was running before the upgrade (prevents version mismatch)"
  su -s /bin/bash "$WINE_USER" -c "export WINEPREFIX=$(printf '%q' "$WINEPREFIX"); wineserver -k" 2>/dev/null || true
  pkill -u "$WINE_USER" -f wineserver 2>/dev/null || true
  sleep 1
  # Wine 11's advapi32 forwards SystemFunction036 to cryptbase. A prefix built
  # on Wine 10 still has the old forward and the Python installer aborts.
  echo "Updating the Wine prefix to this Wine build (wineboot -u)"
  su -s /bin/bash "$WINE_USER" -c "export WINEPREFIX=$(printf '%q' "$WINEPREFIX"); export WINEBOOTSTRAPMODE=1; export WINEDLLOVERRIDES=mscoree=d; wineboot -u"
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

  local list found=0
  shopt -s nullglob
  for list in /etc/apt/sources.list.d/*[Ww]ine* /etc/apt/sources.list; do
    [[ -f "$list" ]] || continue
    if grep -q "dl.winehq.org" "$list"; then
      found=1
      sed -i 's|^Signed-By:.*|Signed-By: /etc/apt/keyrings/winehq-archive.gpg|' "$list"
      sed -i 's|signed-by=[^] ]*|signed-by=/etc/apt/keyrings/winehq-archive.gpg|' "$list"
    fi
  done
  shopt -u nullglob

  if [[ "$found" -eq 0 ]]; then
    cat > "/etc/apt/sources.list.d/winehq-${codename}.sources" <<EOF
Types: deb
URIs: ${wine_uri}
Suites: ${codename}
Components: main
Architectures: amd64 i386
Signed-By: /etc/apt/keyrings/winehq-archive.gpg
EOF
  fi

  dpkg --add-architecture i386
  apt-get update
}

ensure_wine() {
  local before after
  before=$(wine_release || true)
  fix_winehq_apt
  echo "Wine is $(wine --version 2>/dev/null || echo not installed)"
  echo "Installing winehq-stable. Upgrading packages may overlap files, so dpkg will force-overwrite."
  set +e
  apt-get install -y -o Dpkg::Options::="--force-overwrite" winehq-stable
  apt-get install -f -y -o Dpkg::Options::="--force-overwrite"
  local status=$?
  set -e
  if [[ "$status" -ne 0 ]] || ! wine_supports_crealf; then
    echo "Wine is $(wine --version 2>/dev/null || echo missing). MetaTrader5 on Python 3.14 needs Wine 10.1 or newer." >&2
    exit 1
  fi
  after=$(wine_release || true)
  echo "Wine is now $(wine --version)"

  # If Wine version did not change AND Wine is currently functional (no version mismatch with a running wineserver),
  # then we can keep the running server/terminal intact.
  if [[ -n "$before" && "$before" == "$after" ]] && check_wine_working; then
    echo "Wine did not change and is responsive. The running terminal stays open."
    return
  fi

  # Either Wine was upgraded, or a running wineserver daemon has a protocol version mismatch (e.g. 855 vs 930).
  # We must kill the old wineserver and update the prefix.
  stop_and_upgrade_wineserver
}

ensure_tmux() {
  if command -v tmux >/dev/null 2>&1; then
    return
  fi
  echo "Installing tmux so the guardian can run in the background"
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

  if [[ -f "${dest}/requirements-prod.txt" ]]; then
    echo "Installing requirements-prod.txt"
    run_as_abc "$PY_EXE" -m pip install -r "${app_win}/requirements-prod.txt"
  elif [[ -f "${dest}/requirements.txt" ]]; then
    echo "Installing requirements.txt"
    run_as_abc "$PY_EXE" -m pip install -r "${app_win}/requirements.txt"
  fi

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
  echo "Start the guardian in tmux only when you want it running:"
  echo "  bash '$unix_app/deploy/run.sh'"
  echo "Attach to session:"
  echo "  su -s /bin/bash $WINE_USER -c \"tmux attach -t guardian\""
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
