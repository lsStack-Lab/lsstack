#!/bin/bash
# Install lsstack commands for the current user (industry-simple: ~/.local/bin on PATH).
#
# Symlinks every bin/ls* wrapper into ~/.local/bin so you can run lsram / lsstack / …
# from any directory. Wrappers keep setting PYTHONPATH to this checkout.
#
# Usage (from anywhere):
#   /path/to/lsstack/scripts/install-user.sh
#   # or from the lsstack tree:
#   ./scripts/install-user.sh
#
# Later (GitHub/GitLab): same script after clone; pipx/console_scripts can replace this.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
BIN_DIR="$ROOT/bin"
DEST="${LSSTACK_BIN_DIR:-$HOME/.local/bin}"

if [[ ! -d "$BIN_DIR" ]]; then
  echo "error: bin dir not found: $BIN_DIR" >&2
  exit 1
fi

mkdir -p "$DEST"

# Ensure wrappers are executable before linking
for f in "$BIN_DIR"/*; do
  [[ -f "$f" ]] || continue
  chmod +x "$f"
done

linked=0
skipped=0
for f in "$BIN_DIR"/ls*; do
  [[ -f "$f" ]] || continue
  name="$(basename "$f")"
  target="$DEST/$name"
  if [[ -e "$target" || -L "$target" ]]; then
    if [[ -L "$target" ]]; then
      cur="$(readlink -f "$target" 2>/dev/null || readlink "$target")"
      want="$(readlink -f "$f" 2>/dev/null || echo "$f")"
      if [[ "$cur" == "$want" ]]; then
        skipped=$((skipped + 1))
        continue
      fi
    fi
    echo "replace: $target -> $f"
    rm -f "$target"
  fi
  ln -s "$f" "$target"
  echo "link:    $target -> $f"
  linked=$((linked + 1))
done

echo
echo "Installed $linked symlink(s) into $DEST ($skipped already up to date)."
echo "Commands: lsstack lsram lsgpu lslab lscatalog …"

# PATH check
case ":$PATH:" in
  *":$DEST:"*) 
    echo "PATH already includes $DEST — open a new terminal or run: hash -r"
    ;;
  *)
    echo
    echo "Add to PATH (once). For bash, append to ~/.bashrc:"
    echo "  export PATH=\"$DEST:\$PATH\""
    echo "Then: source ~/.bashrc   # or open a new terminal"
    echo
    echo "On many Linux desktops ~/.local/bin is already on PATH after login."
    ;;
esac
