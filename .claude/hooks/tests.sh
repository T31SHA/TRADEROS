#!/usr/bin/env bash
# PostToolUse(Write|Edit) hook: run the test suite after Python edits under src/ or tests/.
#
# The suite is ~25s for 544 tests, so settings.json runs this with asyncRewake:
# it never blocks the edit, and a non-zero exit (2) wakes Claude with the failures.
#
# Only one run is in flight at a time. A run requested while another is active
# leaves a `pending` marker that the active run picks up on its next loop, so the
# final state of a burst of edits still gets tested instead of being silently skipped.
set -uo pipefail

root="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$root" || exit 0

file="$(jq -r '.tool_input.file_path // empty' 2>/dev/null)"
[ -n "$file" ] || exit 0
rel="${file#"$root"/}"
# `case` globs are not pathname expansion here, so * spans directory separators.
case "$rel" in
  src/*.py|tests/*.py) ;;
  *) exit 0 ;;
esac

if [ -x "$root/.venv/bin/python" ]; then
  py="$root/.venv/bin/python"
else
  py="$(command -v python3 || true)"
  [ -n "$py" ] || exit 0
fi
"$py" -c 'import pytest' 2>/dev/null || exit 0

state="${TMPDIR:-/tmp}/traderos-hooks-$(printf '%s' "$root" | cksum | cut -d' ' -f1)"
mkdir -p "$state" || exit 0

: > "$state/pending"
exec 9>"$state/lock"
flock -n 9 || exit 0  # a run is already active; it will consume our pending marker

out=""
status=0
while [ -e "$state/pending" ]; do
  rm -f "$state/pending"
  if out="$("$py" -m pytest -q 2>&1)"; then
    status=0
  else
    status=2
  fi
done

[ "$status" -eq 0 ] && exit 0
printf 'pytest failed after editing %s:\n%s\n' "$rel" "$(printf '%s' "$out" | tail -40)" >&2
exit 2
