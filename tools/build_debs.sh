#!/usr/bin/env bash
# Build packaging/debs/: the OS packages DYODE needs that do not come from
# pip, for offline installs.  Run on a machine WITH internet access and
# Docker (or Podman).  install.sh --offline installs from these files.
#
#   tools/build_debs.sh
#   TARGETS="ubuntu-24.04/amd64 debian-13/arm64" tools/build_debs.sh
#
# Targets (folder/CPU), each holding DYODE's own packages (udpcast,
# python3-venv) and the optional file-sharing services that
# `install.sh --with ssh,nfs,smb` installs, plus everything they depend on:
#   ubuntu-24.04/amd64  ubuntu-24.04/arm64      Ubuntu 24.04, Python 3.12
#   ubuntu-26.04/amd64  ubuntu-26.04/arm64      Ubuntu 26.04, Python 3.14
#   debian-12/arm64                             Raspberry Pi OS bookworm, 3.11
#   debian-13/arm64                             Raspberry Pi OS trixie, 3.13
#
# 64-bit Raspberry Pi OS takes Python and udpcast unchanged from Debian's
# arm64 archive and reports itself as Debian in /etc/os-release, so the
# debian-* folders serve it.  32-bit Raspberry Pi OS (armhf, "raspbian")
# is a different archive and is not covered.
#
# Each target is fetched inside a clean container of that release with
# apt's --download-only, so dependencies are resolved by apt itself rather
# than guessed.  That container is smaller than a real install, so the set
# is a superset of what a given host needs; install.sh lets apt pick only
# the missing ones and never downgrades anything.
#
# arm64 on an x86 machine needs QEMU emulation:
#   docker run --privileged --rm tonistiigi/binfmt --install arm64
# or run the script on an arm64 machine (the GitHub workflow does).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_BASE="$ROOT/packaging/debs"
TARGETS="${TARGETS:-ubuntu-24.04/amd64 ubuntu-24.04/arm64 ubuntu-26.04/amd64 ubuntu-26.04/arm64 debian-12/arm64 debian-13/arm64}"
# DYODE itself needs these.
CORE_PKGS="udpcast python3-venv nftables"
# Optional services for serving received files, installed only on request
# (install.sh --with).  Keep in step with service_pkgs() in install.sh.
# Clients are listed explicitly so a client-only install (--with
# ssh-client,...) works even where no server pulls them in.
SERVICE_PKGS="openssh-server openssh-sftp-server openssh-client nfs-common nfs-kernel-server samba cifs-utils smbclient"
PKGS="$CORE_PKGS $SERVICE_PKGS"

case "${1:-}" in
  -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

ENGINE="${ENGINE:-$(command -v docker || command -v podman || true)}"
[ -n "$ENGINE" ] || { echo "needs docker or podman" >&2; exit 1; }

fail=0
for target in $TARGETS; do
  dir="${target%/*}" arch="${target#*/}"
  image="${dir%%-*}:${dir#*-}"          # ubuntu-24.04 -> ubuntu:24.04
  out="$OUT_BASE/$dir/$arch"
  echo "== $dir $arch ($image): $PKGS"
  rm -rf "$out"; mkdir -p "$out"
  if ! "$ENGINE" run --rm --platform "linux/$arch" \
        -v "$out:/out" -e PKGS="$PKGS" \
        -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
        "$image" bash -c '
      set -e
      export DEBIAN_FRONTEND=noninteractive
      mkdir -p /out/partial          # apt refuses to download without it
      apt-get update -qq
      apt-get install -y -qq --no-install-recommends --download-only \
        -o Dir::Cache::archives=/out $PKGS
      rm -rf /out/partial /out/lock
      cd /out
      sha256sum -- *.deb > SHA256SUMS
      chown -R "$HOST_UID:$HOST_GID" /out
    '; then
    echo "   FAILED (for arm64 on x86, see the QEMU note in --help)" >&2
    fail=1
    continue
  fi
  {
    echo "$dir $arch ($image)"
    echo "built:     $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "requested: $PKGS"
    echo
    (cd "$out" && ls -1 -- *.deb)
  } > "$out/MANIFEST.txt"
  echo "   $(ls "$out"/*.deb | wc -l) packages, $(du -sh "$out" | cut -f1)"
done
[ "$fail" -eq 0 ] || { echo "some targets failed" >&2; exit 1; }
echo "Done. Commit packaging/debs/ to ship them."
