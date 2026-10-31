#!/bin/bash
# Hermes macOS browser node — idempotent installer (no sudo).
set -euo pipefail

HERMES_DIR="${HOME}/.hermes-mac"
VENV_DIR="${HERMES_DIR}/venv"
TOKEN_FILE="${HERMES_DIR}/token"
STATE_FILE="${HERMES_DIR}/state.json"
LOG_FILE="${HERMES_DIR}/node.log"
PLIST_PATH="${HOME}/Library/LaunchAgents/com.hermes.macnode.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIREMENTS="${SCRIPT_DIR}/requirements.txt"
NODE_PY="${SCRIPT_DIR}/node.py"
LABEL="com.hermes.macnode"

render_plist() {
  local python_bin="$1"
  local repo_dir="$2"
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProgramArguments</key>
  <array>
    <string>${python_bin}</string>
    <string>${repo_dir}/node.py</string>
    <string>--on-login</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${repo_dir}</string>
  <key>StandardOutPath</key>
  <string>${LOG_FILE}</string>
  <key>StandardErrorPath</key>
  <string>${LOG_FILE}</string>
</dict>
</plist>
PLIST
}

if [[ "${1:-}" == "--render-plist" ]]; then
  render_plist "${VENV_DIR}/bin/python3" "${SCRIPT_DIR}"
  exit 0
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Erreur : ce script doit être exécuté sur macOS (detecté : $(uname -s))." >&2
  exit 1
fi

MACOS_VERSION="$(sw_vers -productVersion 2>/dev/null || echo inconnu)"
ARCH="$(uname -m)"
echo "macOS ${MACOS_VERSION} (${ARCH})"

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null; then
  echo "Python 3.9+ est requis mais introuvable." >&2
  echo "Installez les outils de ligne de commande Xcode :" >&2
  echo "  xcode-select --install" >&2
  exit 1
fi

mkdir -p "${HERMES_DIR}/profile" "${HERMES_DIR}/dumps"
chmod 700 "${HERMES_DIR}" "${HERMES_DIR}/profile" "${HERMES_DIR}/dumps"

if [[ ! -f "${TOKEN_FILE}" ]]; then
  python3 -c 'import secrets; print(secrets.token_hex(32))' > "${TOKEN_FILE}"
  chmod 600 "${TOKEN_FILE}"
fi

touch "${STATE_FILE}" "${LOG_FILE}"
chmod 600 "${STATE_FILE}" 2>/dev/null || true

if [[ ! -d "${VENV_DIR}/bin" ]]; then
  python3 -m venv "${VENV_DIR}"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip -q
pip install -r "${REQUIREMENTS}" -q

PLAYWRIGHT_CACHE="${HOME}/.cache/ms-playwright"
CHROMIUM_INSTALLED=0
if compgen -G "${PLAYWRIGHT_CACHE}/chromium-*" > /dev/null 2>&1; then
  CHROMIUM_INSTALLED=1
fi
if [[ "${CHROMIUM_INSTALLED}" -eq 0 ]]; then
  python3 -m playwright install chromium
fi

PYTHON_BIN="${VENV_DIR}/bin/python3"
render_plist "${PYTHON_BIN}" "${SCRIPT_DIR}" > "${PLIST_PATH}"

launchctl unload "${PLIST_PATH}" 2>/dev/null || true
launchctl load -w "${PLIST_PATH}"

TAILSCALE_BIN=""
if command -v tailscale >/dev/null 2>&1; then
  TAILSCALE_BIN="tailscale"
elif [[ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ]]; then
  TAILSCALE_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
fi

TS_IP=""
if [[ -n "${TAILSCALE_BIN}" ]]; then
  TS_IP="$("${TAILSCALE_BIN}" ip -4 2>/dev/null | head -n 1 || true)"
  if [[ -n "${TS_IP}" ]]; then
    echo "Tailscale IPv4 : ${TS_IP}"
  fi
else
  echo ""
  echo "Tailscale n'est pas installé. Installez-le puis relancez ce script :" >&2
  echo "  brew install --cask tailscale" >&2
  echo "  ou via l'App Store (Tailscale)" >&2
  echo ""
fi

echo ""
echo "=== Hermes mac-node ==="
if [[ -n "${TS_IP}" ]]; then
  echo "URL du nœud : http://${TS_IP}:8765"
else
  echo "URL du nœud : http://<ip-tailscale>:8765 (installez Tailscale pour obtenir l'IP)"
fi
echo "Jeton (Bearer) : ${TOKEN_FILE}"
echo ""
echo "Étapes manuelles restantes :"
echo "  (a) ${VENV_DIR}/bin/python ${SCRIPT_DIR}/ig_export.py login"
echo "      → connectez votre compte Instagram une fois dans la fenêtre qui s'ouvre."
echo "  (b) rien d'autre — le LaunchAgent vérifie l'export toutes les connexions."
echo ""

if [[ -z "${TAILSCALE_BIN}" ]]; then
  exit 1
fi
