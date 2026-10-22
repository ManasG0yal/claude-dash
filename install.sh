#!/usr/bin/env bash
# claude-dash installer, for macOS, Linux and Windows (WSL).
#
#   From a clone of the repo:  ./install.sh
#   In one line:               curl -fsSL https://raw.githubusercontent.com/OWNER/claude-dash/main/install.sh | bash
#   Remove it again:           ./install.sh --uninstall
#
# Puts the app in ~/.claude/claude-dash/app and a `claude-dash` command in ~/.local/bin.
set -euo pipefail

REPO="${CLAUDE_DASH_REPO:-OWNER/claude-dash}"   # set to your GitHub "user/repo" before publishing
BRANCH="${CLAUDE_DASH_BRANCH:-main}"
APP="$HOME/.claude/claude-dash/app"
BIN="${CLAUDE_DASH_BIN:-$HOME/.local/bin}"

bold=$'\033[1m'; cyan=$'\033[36m'; green=$'\033[32m'; yellow=$'\033[33m'; red=$'\033[31m'; off=$'\033[0m'
step() { printf '%s›%s %s\n' "$cyan" "$off" "$*"; }
ok()   { printf '%s✓%s %s\n' "$green" "$off" "$*"; }
warn() { printf '%s!%s %s\n' "$yellow" "$off" "$*"; }
die()  { printf '%s✗ %s%s\n' "$red" "$*" "$off" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  rm -rf "$APP" "$BIN/claude-dash"
  ok "Removed the app and the claude-dash command."
  echo "  Your dashboard data (pins, summaries) is still in ~/.claude/claude-dash; delete that folder to remove it too."
  exit 0
fi

printf '\n%sclaude-dash%s · a terminal dashboard for Claude Code\n\n' "$bold" "$off"

# 1. What this machine has
case "$(uname -s)" in
  Darwin) os="macOS" ;;
  Linux)  if grep -qi microsoft /proc/version 2>/dev/null; then os="WSL"; else os="Linux"; fi ;;
  *)      die "Windows needs WSL: open an Ubuntu (WSL) terminal and run the installer there." ;;
esac
ok "System: $os"

PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 is missing. Install Python 3.9 or newer first."
"$PY" -c 'import sys, curses; sys.exit(sys.version_info < (3, 9))' 2>/dev/null \
  || die "python3 must be 3.9 or newer, with curses ($PY)."
ok "Python: $("$PY" -c 'import platform; print(platform.python_version())')"

if ! command -v tmux >/dev/null 2>&1; then
  case "$os" in
    macOS) die "tmux is missing. Install it with: brew install tmux" ;;
    *)     die "tmux is missing. Install it with: sudo apt install tmux   (or your distro's package manager)" ;;
  esac
fi
ok "tmux: $(tmux -V)"

command -v claude >/dev/null 2>&1 \
  || die "Claude Code is missing. Install it first: https://code.claude.com/docs/en/setup"
ok "Claude Code: $(claude --version 2>/dev/null | head -1)"

# 2. The files: from this folder when run from a clone, otherwise downloaded from GitHub
here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$here" ] && [ -f "$here/dash.py" ] && [ -d "$here/mod" ]; then
  src="$here"
else
  [ "$REPO" != "OWNER/claude-dash" ] || die "Set CLAUDE_DASH_REPO=user/repo, or run ./install.sh from a clone."
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  step "Downloading $REPO ($BRANCH)…"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$BRANCH" | tar -xz -C "$tmp"
  src="$(find "$tmp" -maxdepth 1 -mindepth 1 -type d | head -1)"
  [ -f "$src/dash.py" ] || die "The download has no dash.py; check CLAUDE_DASH_REPO and CLAUDE_DASH_BRANCH."
fi

step "Installing to $APP"
mkdir -p "$APP"
cp "$src/dash.py" "$APP/dash.py"
rm -rf "$APP/mod"
cp -R "$src/mod" "$APP/mod"
rm -rf "$APP/mod/.claude-plugin/types"   # Claude Code regenerates these for its own version

# 3. The command
mkdir -p "$BIN"
cat > "$BIN/claude-dash" <<EOF
#!/usr/bin/env bash
# claude-dash: start or reopen the dashboard. Try: claude-dash help
exec "$PY" "$APP/dash.py" "\$@"
EOF
chmod +x "$BIN/claude-dash"
ok "Command: $BIN/claude-dash"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    rc="$HOME/.bashrc"; [ "$(basename "${SHELL:-}")" = "zsh" ] && rc="$HOME/.zshrc"
    if ! grep -qs "claude-dash: PATH" "$rc"; then
      printf '\nexport PATH="%s:$PATH"  # claude-dash: PATH\n' "$BIN" >> "$rc"
      warn "Added $BIN to your PATH in $rc. Open a new terminal (or run: source $rc)."
    fi
    ;;
esac

printf '\n%sDone.%s Start it with:  %sclaude-dash%s\n' "$green$bold" "$off" "$bold" "$off"
echo "  Try a preview with made-up data first:  claude-dash demo"
echo "  All commands:                           claude-dash help"
