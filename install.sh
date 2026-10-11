#!/data/data/com.termux/files/usr/bin/bash
# aninext installer for Termux (also works on plain Linux).
#
#   bash install.sh            # install aninext itself
#   bash install.sh --with-extras   # also install ffmpeg, aria2, yt-dlp, termux-api
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-/usr/local}"
BIN_DIR="$PREFIX/bin"
WITH_EXTRAS=0

for arg in "$@"; do
  case "$arg" in
    --with-extras|--extras) WITH_EXTRAS=1 ;;
    -h|--help)
      sed -n '2,6p' "$0"
      exit 0
      ;;
  esac
done

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m ok \033[0m %s\n' "$*"; }
warn() { printf '\033[33m warn\033[0m %s\n' "$*"; }
die()  { printf '\033[31m fail\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- python check
PY=""
for candidate in python3 python python3.12 python3.11; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
[ -n "$PY" ] || die "python not found - run: pkg install python"
say "using $($PY -V 2>&1)"

# ---------------------------------------------------------------- termux setup
if [ -n "${TERMUX_VERSION:-}" ] || [ -d /data/data/com.termux ]; then
  say "Termux detected"
  if [ "$WITH_EXTRAS" = "1" ]; then
    say "installing optional tools (ffmpeg, aria2, yt-dlp, termux-api)"
    pkg update -y || warn "pkg update failed, continuing"
    pkg install -y ffmpeg aria2 python-pip termux-api || warn "some packages failed"
    command -v yt-dlp >/dev/null 2>&1 || pip install --quiet yt-dlp || warn "yt-dlp not installed"
  fi
  if [ ! -d "$HOME/storage" ]; then
    say "run 'termux-setup-storage' once so downloads land in your shared storage"
    warn "skipping automatically - it needs your permission tap"
  fi
fi

# ---------------------------------------------------------------- install
say "installing aninext into $BIN_DIR"
mkdir -p "$BIN_DIR" 2>/dev/null || die "cannot write to $BIN_DIR"

LAUNCHER="$BIN_DIR/aninext"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env sh
# aninext launcher (installed by install.sh)
# the package lives in $SRC_DIR; aninext-cli adds it to sys.path
exec "$PY" "$SRC_DIR/aninext-cli" "\$@"
EOF
chmod +x "$LAUNCHER"
ok "installed: $LAUNCHER"

# keep the package importable when the checkout moves
if [ -d "$SRC_DIR/aninext" ]; then
  SITE="$($PY -c 'import site,sys; print(site.getusersitepackages())' 2>/dev/null || true)"
  if [ -n "$SITE" ]; then
    mkdir -p "$SITE" 2>/dev/null || true
    PTH="$SITE/aninext-dev.pth"
    printf '%s\n' "$SRC_DIR" > "$PTH" 2>/dev/null && ok "import path registered: $PTH" || true
  fi
fi

# ---------------------------------------------------------------- done
printf '\n'
say "checking the install"
"$PY" -c "import sys; sys.path.insert(0, '$SRC_DIR'); import aninext; print('  aninext', aninext.__version__)" \
  || die "import failed"

printf '\n'
ok "all set. try:"
printf '    aninext doctor\n'
printf '    aninext search "frieren"\n'
printf '    aninext --provider demo play frieren -e 1\n'
printf '    aninext --provider archive search "night of the living dead"\n'
printf '\n'
if [ -z "${TERMUX_VERSION:-}" ]; then
  warn "downloads go to $HOME/.aninext/downloads on this system"
fi
