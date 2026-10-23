#!/usr/bin/env bash
# Build packaging/wheels/: every Python package DYODE needs, as wheel files,
# for every Python version and CPU type it is deployed on.  Run this on a
# machine WITH internet access; `install.sh --offline` then installs from
# these files on machines without it.
#
#   tools/build_wheelhouse.sh            rebuild packaging/wheels/
#   tools/build_wheelhouse.sh --verify   only check the existing wheelhouse
#
# Override the matrix with PYTHONS="3.12 3.13" ARCHES="x86_64".
#
# Only PyYAML contains compiled code, so it is the only package needing one
# wheel per Python version and CPU type; the rest are pure Python.  Python
# 3.14 is included because Ubuntu 26.04 ships it as python3.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/packaging/wheels"
PYTHONS="${PYTHONS:-3.11 3.12 3.13 3.14}"
ARCHES="${ARCHES:-x86_64 aarch64}"
PYTHON="${PYTHON:-python3}"
REQS=(
  "$ROOT/DYODE_v1_full/requirements.txt"
  "$ROOT/DYODE_v2_light/in/requirements.txt"
  "$ROOT/DYODE_v2_light/out/requirements.txt"
)

VERIFY_ONLY=0
case "${1:-}" in
  --verify) VERIFY_ONLY=1 ;;
  "") ;;
  -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

pip() { "$PYTHON" -m pip --disable-pip-version-check "$@"; }

# pip's --platform needs every manylinux tag a wheel might carry.
platform_args() {
  local arch="$1"
  echo "--platform manylinux_2_17_$arch --platform manylinux2014_$arch" \
       "--platform manylinux_2_28_$arch --platform manylinux_2_34_$arch"
}

target_args() {           # $1 = python version, $2 = arch
  local py="$1" arch="$2"
  # shellcheck disable=SC2046
  echo "--only-binary=:all: --implementation cp --python-version $py" \
       "--abi cp${py//./} $(platform_args "$arch")"
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [ "$VERIFY_ONLY" -eq 0 ]; then
  command -v "$PYTHON" >/dev/null || { echo "$PYTHON not found" >&2; exit 1; }
  # Every requirement from every variant, comments stripped.
  sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "${REQS[@]}" | sort -u > "$WORK/all.txt"
  echo "Requirements:"; sed 's/^/  /' "$WORK/all.txt"

  mkdir -p "$OUT"
  rm -f "$OUT"/*.whl "$OUT/SHA256SUMS" "$OUT/MANIFEST.txt"

  # Pure-Python packages published only as source get built into a
  # py3-none-any wheel here, which then serves every target.  Compiled
  # wheels built this way only suit THIS machine, so they are dropped and
  # fetched per target below instead.
  echo; echo "== building wheels for source-only pure-Python packages"
  pip wheel --quiet --no-deps --wheel-dir "$WORK/local" -r "$WORK/all.txt"
  find "$WORK/local" -name '*-none-any.whl' -exec cp {} "$OUT/" \;

  for py in $PYTHONS; do
    for arch in $ARCHES; do
      echo "== downloading for Python $py on $arch"
      # shellcheck disable=SC2046
      pip download --quiet --dest "$OUT" --find-links "$OUT" \
        $(target_args "$py" "$arch") -r "$WORK/all.txt"
    done
  done
fi

[ -n "$(ls "$OUT"/*.whl 2>/dev/null)" ] || {
  echo "no wheels in $OUT; run without --verify first" >&2; exit 1; }

# The real test: can every requirements.txt be satisfied for every target
# from these files alone, with the network switched off (--no-index)?
echo; echo "== verifying offline installability"
fail=0
for py in $PYTHONS; do
  for arch in $ARCHES; do
    for req in "${REQS[@]}"; do
      rel="${req#"$ROOT"/}"
      rm -rf "$WORK/verify"
      # shellcheck disable=SC2046
      if pip download --quiet --no-index --find-links "$OUT" \
           --dest "$WORK/verify" $(target_args "$py" "$arch") \
           -r "$req" >"$WORK/log" 2>&1; then
        printf "  ok    Python %-5s %-8s %s\n" "$py" "$arch" "$rel"
      else
        printf "  FAIL  Python %-5s %-8s %s\n" "$py" "$arch" "$rel"
        sed 's/^/        /' "$WORK/log" | tail -3
        fail=1
      fi
    done
  done
done
[ "$fail" -eq 0 ] || { echo "wheelhouse is incomplete" >&2; exit 1; }

if [ "$VERIFY_ONLY" -eq 0 ]; then
  (cd "$OUT" && sha256sum -- *.whl > SHA256SUMS)
  {
    echo "DYODE offline wheelhouse"
    echo "built:   $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "pythons: $PYTHONS"
    echo "arches:  $ARCHES"
    echo "pip:     $(pip --version | cut -d' ' -f1-2)"
    echo
    (cd "$OUT" && ls -1 -- *.whl)
  } > "$OUT/MANIFEST.txt"
  echo; echo "Wrote $(ls "$OUT"/*.whl | wc -l) wheels to ${OUT#"$ROOT"/}"
  echo "($(du -sh "$OUT" | cut -f1)); commit packaging/wheels/ to ship them."
fi
