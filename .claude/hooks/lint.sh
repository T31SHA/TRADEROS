#!/usr/bin/env bash
# PostToolUse(Write|Edit) hook: lint the edited Python file and type-check src.
#
# Runs inline (ruff is ~50ms; `mypy src` is ~0.2s warm, ~5s cold). Exits 2 with
# findings on stderr so Claude sees violations at the edit, not at phase end.
# Exits 0 quietly when the file is not Python or the dev tooling is absent.
set -uo pipefail

root="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$root" || exit 0

file="$(jq -r '.tool_input.file_path // empty' 2>/dev/null)"
[ -n "$file" ] || exit 0
case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0

# Prefer the project virtualenv, fall back to PATH, skip if neither has the tool.
if [ -x "$root/.venv/bin/ruff" ]; then ruff="$root/.venv/bin/ruff"; else ruff="$(command -v ruff || true)"; fi
if [ -x "$root/.venv/bin/mypy" ]; then mypy="$root/.venv/bin/mypy"; else mypy="$(command -v mypy || true)"; fi

out=""
status=0

if [ -n "$ruff" ]; then
  if ! ruff_out="$("$ruff" check --force-exclude "$file" 2>&1)"; then
    out+="ruff check failed for $file:"$'\n'"$ruff_out"$'\n'
    status=2
  fi
fi

# pyproject sets mypy files=["src"] with tests excluded, so only src edits type-check.
case "$file" in
  src/*|"$root"/src/*)
    if [ -n "$mypy" ]; then
      if ! mypy_out="$("$mypy" src 2>&1)"; then
        out+="mypy src failed:"$'\n'"$mypy_out"$'\n'
        status=2
      fi
    fi
    ;;
esac

[ "$status" -eq 0 ] && exit 0
printf '%s' "$out" >&2
exit 2
