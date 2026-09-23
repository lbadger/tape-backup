#!/usr/bin/env bash
# Build one executable; Docker gives a glibc 2.31 baseline on the host CPU arch.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

case "${1:-}" in
  "")
    docker build --file Dockerfile.binary --output type=local,dest=dist .
    ;;
  --local)
    build_python="${PYTHON:-python3}"
    "$build_python" -m venv --without-pip .venv-build
    "$build_python" -m pip --python .venv-build/bin/python install \
      --disable-pip-version-check -r requirements-build.txt
    .venv-build/bin/python -m PyInstaller \
      --noconfirm --clean --onefile --noupx --name tape-backup \
      --specpath build --workpath build/pyinstaller --distpath dist tape_backup.py
    ;;
  *)
    printf 'Usage: %s [--local]\n' "$0" >&2
    exit 2
    ;;
esac

chmod +x dist/tape-backup
(cd dist && sha256sum tape-backup > tape-backup.sha256)
printf 'Built %s/dist/tape-backup\n' "$project_dir"
