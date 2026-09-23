#!/bin/sh
# Run in a minimal Linux container with GNU tar and no installed Python.
set -eu
binary="${1:-/opt/tape-backup/tape-backup}"
if command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1; then
    printf 'This check requires a runtime without Python installed.\n' >&2
    exit 1
fi
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT HUP INT TERM
mkdir "$work_dir/source"
dd if=/dev/zero of="$work_dir/source/keep" bs=1024 count=200 2>/dev/null
printf 'before\n' > "$work_dir/source/change"
printf 'remove\n' > "$work_dir/source/remove"

full_catalog="$("$binary" backup --source "$work_dir/source" \
    --state "$work_dir/state" --media-dir "$work_dir/media" --volume-size 64KiB)"
printf 'after\n' > "$work_dir/source/change"
printf 'added\n' > "$work_dir/source/added"
rm "$work_dir/source/remove"
delta_catalog="$("$binary" backup --source "$work_dir/source" \
    --state "$work_dir/state" --media-dir "$work_dir/media" --volume-size 64KiB \
    --level incremental)"
"$binary" restore --catalog "$full_catalog" "$delta_catalog" \
    --destination "$work_dir/restored" --work-dir "$work_dir/restore-work" \
    --media-dir "$work_dir/media"
diff -r "$work_dir/source" "$work_dir/restored"
"$binary" status --state "$work_dir/state" >/dev/null
printf 'Standalone full + incremental restore passed without Python installed.\n'
