#!/usr/bin/env bash
# PyOS NOVA — One-command installer
# Usage:  curl -sL https://nova-os.dev/install.sh | bash
#         curl -sL https://nova-os.dev/install.sh | bash -s -- --full
#         curl -sL https://nova-os.dev/install.sh | bash -s -- --pi
set -euo pipefail

NOVA_REPO="https://github.com/nova-os/pyos-nova.git"
NOVA_DIR="$HOME/nova"
NOVA_VERSION="0.0008"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

say()   { echo -e "${GREEN}==>${NC} ${BOLD}$*${NC}"; }
warn()  { echo -e "${YELLOW}[warn]${NC} $*"; }
die()   { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

FULL=0; PI=0
for arg in "$@"; do
    case $arg in --full) FULL=1 ;; --pi) PI=1 ;; esac
done

echo -e "\n${CYAN}${BOLD}PyOS NOVA v${NOVA_VERSION} Installer${NC}\n"

# ── Detect platform ─────────────────────────────────────────────────────────
OS=$(uname -s)
ARCH=$(uname -m)
say "Platform: $OS $ARCH"

# ── Python check ─────────────────────────────────────────────────────────────
for PY in python3.12 python3.11 python3; do
    if command -v $PY &>/dev/null; then
        PY_CMD=$PY; break
    fi
done
[[ -z "${PY_CMD:-}" ]] && die "Python 3.11+ required. Install with: sudo apt install python3.11"

PY_VER=$($PY_CMD -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
say "Python: $PY_CMD ($PY_VER)"

# ── Clone or update ───────────────────────────────────────────────────────────
if [[ -d "$NOVA_DIR/.git" ]]; then
    say "Updating existing install at $NOVA_DIR"
    git -C "$NOVA_DIR" pull --ff-only
else
    say "Cloning PyOS NOVA to $NOVA_DIR"
    git clone --depth=1 "$NOVA_REPO" "$NOVA_DIR" 2>/dev/null \
        || { warn "git clone failed; trying zip download"
             curl -sL "https://github.com/nova-os/pyos-nova/archive/main.zip" -o /tmp/nova.zip
             unzip -q /tmp/nova.zip -d /tmp
             mv /tmp/pyos-nova-main "$NOVA_DIR"
             rm /tmp/nova.zip; }
fi
cd "$NOVA_DIR"

# ── Virtual environment ───────────────────────────────────────────────────────
VENV="$NOVA_DIR/.venv"
if [[ ! -d "$VENV" ]]; then
    say "Creating virtual environment"
    $PY_CMD -m venv "$VENV"
fi
source "$VENV/bin/activate"
pip install --upgrade pip setuptools wheel -q

# ── Install NOVA ─────────────────────────────────────────────────────────────
if [[ $FULL -eq 1 ]]; then
    say "Installing NOVA + all optional dependencies"
    pip install -e ".[full]" -q
elif [[ $PI -eq 1 ]]; then
    say "Installing NOVA for Raspberry Pi"
    pip install -e ".[ai,compress,ssh,i18n,rpi]" -q
else
    say "Installing NOVA (core)"
    pip install -e "." -q
fi

# ── Shell alias ───────────────────────────────────────────────────────────────
SHELL_RC="$HOME/.bashrc"
[[ "$SHELL" == */zsh ]] && SHELL_RC="$HOME/.zshrc"

ALIAS_LINE="alias nova='source $VENV/bin/activate && python3 $NOVA_DIR/main.py'"
if ! grep -q "alias nova=" "$SHELL_RC" 2>/dev/null; then
    echo "$ALIAS_LINE" >> "$SHELL_RC"
    say "Added 'nova' alias to $SHELL_RC"
fi

# ── Systemd service (Linux only) ─────────────────────────────────────────────
if [[ $OS == "Linux" ]] && command -v systemctl &>/dev/null; then
    SERVICE_FILE="/etc/systemd/system/nova.service"
    if [[ ! -f "$SERVICE_FILE" ]] && [[ $EUID -eq 0 ]]; then
        cat > "$SERVICE_FILE" << EOF
[Unit]
Description=PyOS NOVA
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$NOVA_DIR
ExecStart=$VENV/bin/python3 $NOVA_DIR/main.py --daemon
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        say "Systemd service installed (start with: sudo systemctl start nova)"
    fi
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
say "PyOS NOVA v${NOVA_VERSION} installed!"
echo ""
echo -e "  Start:   ${CYAN}nova${NC}   (after reloading shell)"
echo -e "  Or:      ${CYAN}source $VENV/bin/activate && python3 $NOVA_DIR/main.py${NC}"
echo -e "  Docs:    ${CYAN}$NOVA_DIR/INSTALL.md${NC}"
echo -e "  Help:    ${CYAN}nova> help${NC}"
echo ""
