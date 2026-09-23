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

full_id="$("$binary" backup --source "$work_dir/source" \
    --media-dir "$work_dir/media" --volume-size 256KiB --buffer-size 64KiB)"
printf 'after\n' > "$work_dir/source/change"
printf 'added\n' > "$work_dir/source/added"
rm "$work_dir/source/remove"
delta_id="$("$binary" backup --source "$work_dir/source" \
    --media-dir "$work_dir/media" --volume-size 256KiB --buffer-size 64KiB \
    --level incremental --base "$full_id")"
"$binary" restore --backup "$full_id" "$delta_id" \
    --destination "$work_dir/restored" \
    --media-dir "$work_dir/media"
diff -r "$work_dir/source" "$work_dir/restored"
"$binary" verify --backup "$delta_id" --media-dir "$work_dir/media" >/dev/null
printf 'Streaming full + incremental restore passed without Python or disk archive staging.\n'
