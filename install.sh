#!/usr/bin/env bash
# DYODE installer: OS packages, a Python virtualenv, and DYODE's Python
# packages -- from the internet, or offline from the files in packaging/.
#
#   sudo ./install.sh                     asks everything it needs
#   sudo ./install.sh --offline           bundled files only, no network
#   sudo ./install.sh --online            apt and PyPI, as usual
#   sudo ./install.sh --offline --variant v2 --side out --yes --no-wizard
#
# Options:
#   --offline | --online      where packages come from (asked if omitted)
#   --variant v1|v2           DYODE v1 (full) or v2 (light)  (asked if omitted)
#   --side in|out             which box; needed for v2       (asked if omitted)
#   --python PATH             use this interpreter (3.11+) for the virtualenv
#   --with LIST               also install file-sharing services and/or
#                             clients, comma-separated       (asked if omitted)
#                               ssh         SSH/SFTP server (+ client)
#                               nfs         NFS server (+ client)
#                               smb         Samba server (+ clients)
#                               ssh-client  sftp, scp, ssh only
#                               nfs-client  mount.nfs only
#                               smb-client  mount.cifs, smbclient only
#                               all = ssh,nfs,smb   clients = the three
#                               clients   none = nothing
#   --share-nfs PATH=CLIENTS[:ro|:rw]
#                             export PATH over NFS to CLIENTS (address, host or
#                             subnet; comma-separated).  Repeatable.
#   --share-smb PATH=USER[:ro|:rw]
#                             share PATH over SMB to one existing account.
#                             Repeatable.  Shares are read-only unless :rw.
#   --smb-password-file FILE  'user:password' lines for SMB accounts, for
#                             unattended installs (keep it mode 600)
#   --no-shares               do not ask about file shares
#   --shares-only             only set up the shares; install nothing
#   --no-tuning               do not install the receive-side kernel settings
#                             (/etc/sysctl.d/90-dyode-receive.conf; see
#                             CAVEATS.md).  Never installed with --side in.
#   --tuning-only             only install those settings; install nothing
#   --no-firewall             do not install the diode-port firewall rules
#                             (nftables table "inet dyode": diode ports accept
#                             traffic only on the diode NIC from the input box).
#                             Installed by default except with --side in.
#   --firewall-only           only (re)apply the firewall rules from config.yaml
#   --config FILE             config.yaml to use with --firewall-only
#   --skip-os-packages        do not install udpcast / python venv support
#   --no-wizard               do not start the setup wizard afterwards
#   -y, --yes                 do not ask for confirmation
#   --dry-run                 show what would be done, change nothing
#   -h, --help                this text
#
# The virtualenv is created on THIS machine, from this machine's Python.
# Copying a venv/ between machines or folders is not supported: a venv
# records the absolute path it was created at and the exact interpreter.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WHEELS="$ROOT/packaging/wheels"
DEBS="$ROOT/packaging/debs"
OS_RELEASE="${DYODE_OS_RELEASE:-/etc/os-release}"

MODE="" VARIANT="" SIDE="" PYTHON="" WITH="" WITH_GIVEN=0
ASSUME_YES=0 RUN_WIZARD=1 SKIP_OS=0 DRY_RUN=0
SHARES_ONLY=0 NO_SHARES=0 NO_TUNING=0 TUNING_ONLY=0 NO_FIREWALL=0 FIREWALL_ONLY=0 FW_CONFIG="" SHARE_NFS_SPECS=() SHARE_SMB_SPECS=() SMB_PW_FILE=""

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf '\nerror: %s\n' "$*" >&2; exit 1; }
run()  {
  if [ "$DRY_RUN" -eq 1 ]; then printf '   would run: %s\n' "$*"; else "$@"; fi
}
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }
interactive() { [ -t 0 ] && [ -t 1 ]; }
# shellcheck source=lib/install-shares.sh
. "$ROOT/lib/install-shares.sh"
# shellcheck source=lib/install-tuning.sh
. "$ROOT/lib/install-tuning.sh"
# shellcheck source=lib/install-firewall.sh
. "$ROOT/lib/install-firewall.sh"

# ---------------------------------------------------------------- arguments
while [ $# -gt 0 ]; do
  case "$1" in
    --offline) MODE=offline ;;
    --online)  MODE=online ;;
    --mode)    MODE="${2:-}"; shift ;;
    --mode=*)  MODE="${1#*=}" ;;
    --variant) VARIANT="${2:-}"; shift ;;
    --variant=*) VARIANT="${1#*=}" ;;
    --side)    SIDE="${2:-}"; shift ;;
    --side=*)  SIDE="${1#*=}" ;;
    --python)  PYTHON="${2:-}"; shift ;;
    --python=*) PYTHON="${1#*=}" ;;
    --with)    WITH="${2:-}"; WITH_GIVEN=1; shift ;;
    --with=*)  WITH="${1#*=}"; WITH_GIVEN=1 ;;
    --share-nfs) SHARE_NFS_SPECS+=("${2:-}"); shift ;;
    --share-nfs=*) SHARE_NFS_SPECS+=("${1#*=}") ;;
    --share-smb) SHARE_SMB_SPECS+=("${2:-}"); shift ;;
    --share-smb=*) SHARE_SMB_SPECS+=("${1#*=}") ;;
    --smb-password-file) SMB_PW_FILE="${2:-}"; shift ;;
    --smb-password-file=*) SMB_PW_FILE="${1#*=}" ;;
    --no-shares) NO_SHARES=1 ;;
    --shares-only) SHARES_ONLY=1 ;;
    --no-tuning) NO_TUNING=1 ;;
    --tuning-only) TUNING_ONLY=1 ;;
    --no-firewall) NO_FIREWALL=1 ;;
    --firewall-only) FIREWALL_ONLY=1 ;;
    --config) FW_CONFIG="${2:-}"; shift ;;
    --config=*) FW_CONFIG="${1#*=}" ;;
    --skip-os-packages) SKIP_OS=1 ;;
    --no-wizard) RUN_WIZARD=0 ;;
    -y|--yes)  ASSUME_YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
  shift
done
case "$MODE"    in ""|offline|online) ;; *) die "--mode must be offline or online" ;; esac
case "$VARIANT" in ""|v1|v2) ;; *) die "--variant must be v1 or v2" ;; esac
case "$SIDE"    in ""|in|out) ;; *) die "--side must be in or out" ;; esac

# Optional services for serving received files (NFS, SFTP, SMB shares).
# Keep in step with SERVICE_PKGS in tools/build_debs.sh, which bundles them.
service_pkgs() {
  case "$1" in
    # Servers bring their own client, so a box can test its shares locally.
    ssh) echo "openssh-server openssh-sftp-server openssh-client" ;;
    nfs) echo "nfs-kernel-server nfs-common" ;;
    smb) echo "samba cifs-utils smbclient" ;;
    # Clients alone: for a box that only connects out, with no server.
    ssh-client) echo "openssh-client" ;;
    nfs-client) echo "nfs-common" ;;
    smb-client) echo "cifs-utils smbclient" ;;
  esac
}
SERVICE_NAMES="ssh nfs smb ssh-client nfs-client smb-client"
normalize_with() {     # "SSH, smb-client" -> "ssh smb-client"; all; clients; none
  local list out="" item add items
  list="$(printf '%s' "$1" | tr 'A-Z,' 'a-z ' )"
  read -r -a items <<<"$list"          # no wildcard expansion, unlike $list
  for item in "${items[@]}"; do
    case "$item" in
      all)     add="ssh nfs smb" ;;
      clients) add="ssh-client nfs-client smb-client" ;;
      none)    add="" ;;
      *) case " $SERVICE_NAMES " in
           *" $item "*) add="$item" ;;
           *) die "--with takes ssh, nfs, smb, ssh-client, nfs-client, smb-client, all, clients or none (got '$item')" ;;
         esac ;;
    esac
    for a in $add; do
      case " $out " in *" $a "*) ;; *) out="$out $a" ;; esac
    done
  done
  echo "${out# }"
}
WITH="$(normalize_with "$WITH")"
[ "$SKIP_OS" -eq 1 ] && [ -n "$WITH" ] &&
  die "--with installs OS packages, so it cannot be combined with --skip-os-packages"

# File shares: validate every spec now, before anything is installed.
if [ -n "$SMB_PW_FILE" ]; then
  [ -r "$SMB_PW_FILE" ] || die "cannot read --smb-password-file $SMB_PW_FILE"
  case "$(stat -c %a "$SMB_PW_FILE")" in
    *00) ;; *) warn "$SMB_PW_FILE is readable by other users; chmod 600 it" ;;
  esac
  SMB_PASSWORD_FILE="$SMB_PW_FILE"
fi
for spec in "${SHARE_NFS_SPECS[@]}"; do add_nfs_share "$spec"; done
for spec in "${SHARE_SMB_SPECS[@]}"; do add_smb_share "$spec"; done
if [ "${#NFS_SHARES[@]}" -gt 0 ] && ! command -v exportfs >/dev/null 2>&1; then
  case " $WITH " in *" nfs "*) ;; *) die "--share-nfs needs the NFS server: add --with nfs" ;; esac
fi
if [ "${#SMB_SHARES[@]}" -gt 0 ] && ! command -v testparm >/dev/null 2>&1; then
  case " $WITH " in *" smb "*) ;; *) die "--share-smb needs Samba: add --with smb" ;; esac
fi

# --tuning-only: install the receive-side kernel settings and stop.
if [ "$TUNING_ONLY" -eq 1 ]; then
  [ "$NO_TUNING" -eq 0 ] || die "--tuning-only and --no-tuning contradict each other"
  [ "$SIDE" != in ] || die "--tuning-only is for receiving boxes; this one was given --side in"
  [ "$(id -u)" -eq 0 ] || [ "$DRY_RUN" -eq 1 ] || [ -n "${DYODE_ETC:-}" ] ||
    die "installing kernel settings needs root: run with sudo"
  apply_tuning
  exit 0
fi

# --firewall-only: (re)apply the diode-port firewall from config.yaml and stop.
if [ "$FIREWALL_ONLY" -eq 1 ]; then
  [ "$NO_FIREWALL" -eq 0 ] || die "--firewall-only and --no-firewall contradict each other"
  [ "$SIDE" != in ] || die "--firewall-only is for receiving boxes; this one was given --side in"
  [ "$(id -u)" -eq 0 ] || [ "$DRY_RUN" -eq 1 ] || [ -n "${DYODE_ETC:-}" ] ||
    die "installing firewall rules needs root: run with sudo"
  FW_CFG="" FW_PY="${PYTHON:-python3}"
  if [ -n "$FW_CONFIG" ]; then
    FW_CFG="$FW_CONFIG"
    d="$(dirname "$FW_CONFIG")"
    [ -x "$d/venv/bin/python" ] && FW_PY="$d/venv/bin/python"
  else
    find_config_and_python || die "no config.yaml found; run the setup wizard first, or pass --config FILE"
  fi
  [ -f "$FW_CFG" ] || die "no such config file: $FW_CFG"
  apply_firewall "$FW_CFG" "$FW_PY"
  exit 0
fi

# --shares-only: set up file shares on an installed box and stop.
if [ "$SHARES_ONLY" -eq 1 ]; then
  [ "$(id -u)" -eq 0 ] || [ "$DRY_RUN" -eq 1 ] || [ -n "${DYODE_ETC:-}" ] ||
    die "setting up shares needs root: run with sudo"
  if [ "${#NFS_SHARES[@]}" -eq 0 ] && [ "${#SMB_SHARES[@]}" -eq 0 ]; then
    interactive || die "--shares-only needs --share-nfs / --share-smb (no terminal to ask on)"
    cfg="" py=""
    for t in "$ROOT/DYODE_v1_full" "$ROOT/DYODE_v2_light/in" "$ROOT/DYODE_v2_light/out"; do
      if [ -f "$t/config.yaml" ] && [ -x "$t/venv/bin/python" ]; then cfg="$t/config.yaml" py="$t/venv/bin/python"; break; fi
    done
    interactive_shares "$cfg" "$py"
  fi
  apply_shares
  exit 0
fi

# Numbered choice on the terminal.  Prints the chosen number (1-based).
choose() {                       # choose "question" default "opt1" "opt2" ...
  local question="$1" default="$2" answer i; shift 2
  say "" >&2; say "$question" >&2
  i=1; for opt in "$@"; do say "  $i) $opt" >&2; i=$((i + 1)); done
  while true; do
    printf 'Choice [%s]: ' "$default" >&2
    read -r answer || die "input ended"
    answer="${answer:-$default}"
    if [[ "$answer" =~ ^[0-9]+$ ]] && [ "$answer" -ge 1 ] && [ "$answer" -le $# ]; then
      echo "$answer"; return
    fi
    say "Please enter a number from 1 to $#." >&2
  done
}

# ---------------------------------------------------------------- the host
OS_ID="" OS_VER=""
if [ -r "$OS_RELEASE" ]; then
  OS_ID="$(. "$OS_RELEASE" && echo "${ID:-}")"
  OS_VER="$(. "$OS_RELEASE" && echo "${VERSION_ID:-}")"
fi
ARCH="${DYODE_ARCH:-$(dpkg --print-architecture 2>/dev/null || true)}"   # override: tests
if [ -z "$ARCH" ]; then
  case "$(uname -m)" in x86_64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; *) ARCH="$(uname -m)" ;; esac
fi
DEB_DIR="$DEBS/$OS_ID-$OS_VER/$ARCH"   # e.g. ubuntu-24.04/amd64, debian-13/arm64

wheel_count() { ls "$WHEELS"/*.whl 2>/dev/null | wc -l | tr -d ' '; }
debs_present() { [ -n "$OS_ID" ] && ls "$DEB_DIR"/*.deb >/dev/null 2>&1; }
# What packaging/debs/ is built for (tools/build_debs.sh).  64-bit Raspberry
# Pi OS reports itself as Debian, so the debian-* folders serve it.
SUPPORTED="Ubuntu 24.04 and 26.04 (amd64, arm64), Raspberry Pi OS 12 bookworm and 13 trixie (64-bit)"
internet() {
  getent hosts pypi.org >/dev/null 2>&1 &&
    timeout 5 bash -c 'exec 3<>/dev/tcp/pypi.org/443' 2>/dev/null
}

py_ok() {                        # a Python of at least 3.11
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null
}
py_ver() { "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])'; }
venv_ok() {                      # Debian strips ensurepip without pythonX.Y-venv
  "$1" -c 'import ensurepip, venv' 2>/dev/null
}
find_python() {
  local c p
  if [ -n "$PYTHON" ]; then
    py_ok "$PYTHON" && { echo "$PYTHON"; return 0; }
    die "--python $PYTHON is not Python 3.11 or newer"
  fi
  # The distribution's own python3 first: it is what python3-venv targets.
  for c in python3 python3.14 python3.13 python3.12 python3.11; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    py_ok "$p" && { echo "$p"; return 0; }
  done
  return 1
}

# ---------------------------------------------------------------- questions
say "DYODE $(cat "$ROOT/VERSION" 2>/dev/null || echo) installer"
say "  host:    ${OS_ID:-unknown} ${OS_VER:-?} ($ARCH)"
say "  bundle:  $(wheel_count) Python wheel(s); $(debs_present && echo "OS packages for $OS_ID $OS_VER $ARCH" || echo "no OS packages for this system")"

if [ -z "$MODE" ]; then
  interactive || die "choose --offline or --online (no terminal to ask on)"
  if [ "$(wheel_count)" -gt 0 ] && ! internet; then default=1; net="not reachable"
  else default=2; net="reachable"; fi
  say "  internet: $net"
  n="$(choose "Where should packages come from?" "$default" \
        "Offline - only the files bundled in packaging/ (no network needed)" \
        "Online  - apt and PyPI over the internet")"
  [ "$n" = 1 ] && MODE=offline || MODE=online
fi

if [ -z "$VARIANT" ]; then
  interactive || die "choose --variant v1 or v2 (no terminal to ask on)"
  n="$(choose "Which DYODE is this box?" 1 \
        "DYODE v1 (full)  - Ethernet + optical link: files, Modbus, screen" \
        "DYODE v2 (light) - optocoupler serial link: Modbus only")"
  [ "$n" = 1 ] && VARIANT=v1 || VARIANT=v2
fi

# v1 keeps both sides in one folder; v2 has one folder per side.
if [ "$VARIANT" = v2 ] && [ -z "$SIDE" ]; then
  interactive || die "v2 needs --side in or out (no terminal to ask on)"
  n="$(choose "Which side of the diode is this box?" 1 \
        "Input (sending)" "Output (receiving)")"
  [ "$n" = 1 ] && SIDE=in || SIDE=out
fi

# File-sharing services: asked only on a terminal and only if --with was
# not given.  Unattended installs add none unless asked to.
if [ "$WITH_GIVEN" -eq 0 ] && [ "$SKIP_OS" -eq 0 ] && interactive; then
  say ""
  say "Also install file-sharing services or clients?"
  say "  ssh  nfs  smb                     servers (each with its client)"
  say "  ssh-client  nfs-client  smb-client  clients only, no server"
  say "  all = every server    clients = every client"
  printf 'Enter a comma-separated list, or nothing for none: '
  read -r answer || die "input ended"
  WITH="$(normalize_with "$answer")"
fi

if [ "$VARIANT" = v1 ]; then TARGET="$ROOT/DYODE_v1_full"
else TARGET="$ROOT/DYODE_v2_light/$SIDE"; fi
VENV="$TARGET/venv"
REQ="$TARGET/requirements.txt"
[ -f "$REQ" ] || die "missing $REQ"

# ---------------------------------------------------------------- plan
PY="$(find_python || true)"
PKGS=()
if [ "$SKIP_OS" -eq 0 ]; then
  if [ "$VARIANT" = v1 ] && ! command -v udp-sender >/dev/null 2>&1; then
    PKGS+=(udpcast)
  fi
  if firewall_wanted && ! command -v nft >/dev/null 2>&1; then
    PKGS+=(nftables)
  fi
  if [ -z "$PY" ]; then
    die "no Python 3.11 or newer found (supported: $SUPPORTED); install one, or pass --python"
  elif ! venv_ok "$PY"; then
    PKGS+=("python$(py_ver "$PY")-venv")
  fi
elif [ -z "$PY" ]; then
  die "no Python 3.11 or newer found (and --skip-os-packages was given)"
fi

pkg_installed() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}
SERVICE_NEW=() SERVICE_HAVE=()
SEEN=" "
for service in $WITH; do
  for pkg in $(service_pkgs "$service"); do
    case "$SEEN" in *" $pkg "*) continue ;; esac   # in two groups
    SEEN="$SEEN$pkg "
    if pkg_installed "$pkg"; then SERVICE_HAVE+=("$pkg")
    else SERVICE_NEW+=("$pkg"); PKGS+=("$pkg"); fi
  done
done

say ""
say "Plan ($MODE install of DYODE $VARIANT${SIDE:+, $SIDE side}):"
if [ "${#PKGS[@]}" -gt 0 ]; then
  say "  OS packages:  ${PKGS[*]}  (from $([ "$MODE" = offline ] && echo "${DEB_DIR#"$ROOT"/}" || echo apt))"
else
  say "  OS packages:  nothing needed"
fi
if [ -n "$WITH" ]; then
  say "  services:     ${WITH// /, }  (${#SERVICE_NEW[@]} package(s) to install, ${#SERVICE_HAVE[@]} already present)"
fi
say "  Python:       $PY ($(py_ver "$PY"))"
say "  virtualenv:   ${VENV#"$ROOT"/}"
say "  packages:     ${REQ#"$ROOT"/}  (from $([ "$MODE" = offline ] && echo "${WHEELS#"$ROOT"/}" || echo PyPI))"
[ "$RUN_WIZARD" -eq 1 ] && say "  then:         the setup wizard"
describe_tuning
describe_firewall
describe_shares

if [ "$ASSUME_YES" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  interactive || die "pass --yes to proceed without a terminal"
  printf '\nProceed? [Y/n] '
  read -r answer || die "input ended"
  case "${answer:-y}" in y|Y|yes|YES) ;; *) say "Nothing changed."; exit 1 ;; esac
fi

if [ "${#PKGS[@]}" -gt 0 ] && [ "$(id -u)" -ne 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  die "installing ${PKGS[*]} needs root: run with sudo (or --skip-os-packages)"
fi

check_sums() {                   # catch files damaged on the way across
  local dir="$1"
  [ -f "$dir/SHA256SUMS" ] || die "no SHA256SUMS in ${dir#"$ROOT"/}; the bundle is incomplete"
  (cd "$dir" && sha256sum --quiet -c SHA256SUMS) ||
    die "files in ${dir#"$ROOT"/} do not match SHA256SUMS; copy the bundle again"
  # Every package must be listed: an unlisted one would be installed unchecked.
  local unlisted
  unlisted="$(cd "$dir" && comm -23 <(ls -1 -- *.deb *.whl 2>/dev/null | sort) \
                                    <(sed 's/^[0-9a-f]*  \*\{0,1\}//' SHA256SUMS | sort))"
  [ -z "$unlisted" ] ||
    die "package(s) in ${dir#"$ROOT"/} not listed in its SHA256SUMS, so they cannot be checked: $(echo $unlisted | head -c 300)"
}

# Offline: check the whole bundle BEFORE changing anything, so a damaged or
# incomplete copy stops the install instead of leaving it half done.
if [ "$MODE" = offline ]; then
  step "checking the offline bundle"
  [ "$(wheel_count)" -gt 0 ] || die "no wheels in ${WHEELS#"$ROOT"/}; build them with tools/build_wheelhouse.sh on a machine with internet access"
  check_sums "$WHEELS"
  say "   $(wheel_count) wheels intact"
  if [ "${#PKGS[@]}" -gt 0 ]; then
    if [ "$OS_ID" = raspbian ]; then
      die "32-bit Raspberry Pi OS is not covered by the bundle; use the 64-bit edition, or install ${PKGS[*]} by hand and rerun with --skip-os-packages"
    fi
    debs_present || die "no bundled OS packages for ${OS_ID:-this system} ${OS_VER} $ARCH (bundled for: $SUPPORTED). Install ${PKGS[*]} by hand, then rerun with --skip-os-packages"
    check_sums "$DEB_DIR"
    say "   $(ls "$DEB_DIR"/*.deb | wc -l | tr -d ' ') OS packages for $OS_ID $OS_VER $ARCH intact"
  fi
fi

# ---------------------------------------------------------------- OS packages
# Offline, apt is pointed at a private repository built from the bundled
# .deb files and nothing else.  apt then resolves dependencies itself,
# installs only what is actually missing, never downgrades a package the
# host already has newer, and cannot reach the network.
install_offline_debs() {          # bundle already checked above
  local tmp; tmp="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" RETURN
  mkdir -p "$tmp/repo" "$tmp/lists/partial" "$tmp/parts"
  cp "$DEB_DIR"/*.deb "$tmp/repo/"
  chmod 755 "$tmp" "$tmp/repo"; chmod 644 "$tmp/repo"/*.deb
  # A Packages index from the .deb files themselves, so it can never
  # disagree with them.  Only dpkg-deb is needed, which every host has.
  for deb in "$tmp/repo"/*.deb; do
    dpkg-deb -f "$deb"
    printf 'Filename: ./%s\nSize: %s\nSHA256: %s\n\n' "$(basename "$deb")" \
      "$(stat -c %s "$deb")" "$(sha256sum "$deb" | cut -d' ' -f1)"
  done > "$tmp/repo/Packages"
  echo "deb [trusted=yes] file:$tmp/repo ./" > "$tmp/local.list"
  local opts=(-o "Dir::Etc::SourceList=$tmp/local.list"
              -o "Dir::Etc::SourceParts=$tmp/parts"
              -o "Dir::State::Lists=$tmp/lists"
              -o "Dir::Cache::pkgcache=" -o "Dir::Cache::srcpkgcache="
              -o "APT::Sandbox::User=root" -o "Acquire::Languages=none")
  apt-get "${opts[@]}" -qq update
  DEBIAN_FRONTEND=noninteractive apt-get "${opts[@]}" install -y \
    --no-install-recommends "${PKGS[@]}" ||
    die "the bundled packages for $OS_ID $OS_VER $ARCH cannot satisfy ${PKGS[*]} on this host (see apt's message above). Rebuild them with tools/build_debs.sh, or install the missing packages by hand and rerun with --skip-os-packages"
}

if [ "${#PKGS[@]}" -gt 0 ]; then
  step "OS packages: ${PKGS[*]}"
  if [ "$MODE" = offline ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      say "   would install from ${DEB_DIR#"$ROOT"/} via a private local apt repository"
    else
      install_offline_debs
    fi
  else
    run apt-get update
    DEBIAN_FRONTEND=noninteractive run apt-get install -y --no-install-recommends "${PKGS[@]}"
  fi
  if [ "$DRY_RUN" -eq 0 ]; then
    PY="$(find_python)" || die "still no Python 3.11 or newer after installing ${PKGS[*]}"
    venv_ok "$PY" || die "$PY still cannot create virtualenvs"
  fi
fi

# ---------------------------------------------------------------- virtualenv
step "virtualenv ${VENV#"$ROOT"/} ($PY)"
reuse=0
if [ -x "$VENV/bin/python" ] && [ -n "$PY" ] &&
   [ "$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" = "$(py_ver "$PY")" ]; then
  # Python 3.11+ records the path the venv was created at.  A venv carried
  # over from another folder or revision is rebuilt, not trusted.
  made_at="$(sed -n 's/^command = .* -m venv \(.*\)$/\1/p' "$VENV/pyvenv.cfg" 2>/dev/null)"
  if [ -z "$made_at" ] || [ "$made_at" = "$VENV" ]; then reuse=1; fi
fi
if [ "$reuse" -eq 1 ]; then
  say "   reusing the existing virtualenv"
else
  if [ -e "$VENV" ]; then
    say "   replacing an existing virtualenv made elsewhere or with another Python"
    run rm -rf "$VENV"
  fi
  run "$PY" -m venv "$VENV"
fi

# ---------------------------------------------------------------- packages
step "Python packages from ${REQ#"$ROOT"/}"
PIP=("$VENV/bin/python" -m pip --disable-pip-version-check)
if [ "$MODE" = offline ]; then
  if ! run "${PIP[@]}" install --no-index --find-links "$WHEELS" -r "$REQ"; then
    die "the bundled wheels do not cover this system (Python $(py_ver "$PY"), $ARCH); see packaging/wheels/MANIFEST.txt and rebuild with tools/build_wheelhouse.sh"
  fi
else
  run "${PIP[@]}" install -r "$REQ"
fi

# ---------------------------------------------------------------- verify
step "checking the installation"
if [ "$DRY_RUN" -eq 1 ]; then
  say "   would import every package in ${REQ#"$ROOT"/}"
else
  "$VENV/bin/python" - "$REQ" <<'PYEOF' || die "installed packages failed to import"
import importlib, importlib.metadata as md, re, sys
modules = {"pyyaml": "yaml", "pymodbus": "pymodbus",
           "inotify-simple": "inotify_simple", "inotify_simple": "inotify_simple",
           "pyserial": "serial"}
for line in open(sys.argv[1]):
    line = line.split("#")[0].strip()
    if not line:
        continue
    name = re.split(r"[\s<>=!~;\[]", line)[0]
    module = modules.get(name.lower(), name.lower().replace("-", "_"))
    importlib.import_module(module)
    print("   ok  %-16s %s" % (name, md.version(name)))
PYEOF
  if [ "$VARIANT" = v1 ] && ! command -v udp-sender >/dev/null 2>&1; then
    warn "udp-sender is not installed; folder transfers will not work until udpcast is"
  fi
fi

apply_tuning

say ""
say "Installed: DYODE $VARIANT${SIDE:+ ($SIDE side)}, $MODE, in ${TARGET#"$ROOT"/}"

# Debian and Ubuntu start these services the moment they are installed, with
# their stock settings.  Say so, and where each is configured.
if [ -n "$WITH" ]; then
  say ""
  say "File sharing (servers run with their default settings; review them):"
  for service in $WITH; do
    case "$service" in
      ssh) say "  ssh  SFTP/SSH on port 22     /etc/ssh/sshd_config (consider key-only logins)" ;;
      nfs) say "  nfs  NFS server              /etc/exports (or: ./install.sh --shares-only)" ;;
      smb) say "  smb  Samba server            /etc/samba/smb.conf (or: ./install.sh --shares-only)" ;;
      ssh-client) say "  ssh-client  sftp, scp, ssh  (no server)" ;;
      nfs-client) say "  nfs-client  mount -t nfs    (no server)" ;;
      smb-client) say "  smb-client  mount -t cifs, smbclient  (no server)" ;;
    esac
  done
fi

# ---------------------------------------------------------------- wizard
if [ "$RUN_WIZARD" -eq 1 ]; then
  args=(--variant "$VARIANT" --install-mode "$MODE")
  [ -n "$SIDE" ] && args+=(--side "$SIDE")
  if [ "$DRY_RUN" -eq 1 ]; then
    say "   would run: $VENV/bin/python dyode_setup.py ${args[*]}"
  elif interactive; then
    step "setup wizard"
    "$VENV/bin/python" "$ROOT/dyode_setup.py" "${args[@]}" ||
      warn "the setup wizard did not finish; rerun it with: $VENV/bin/python $ROOT/dyode_setup.py ${args[*]}"
  else
    say "Next: $VENV/bin/python $ROOT/dyode_setup.py ${args[*]}"
  fi
fi

# The message authentication key (v1): DYODE refuses to start without it.
if [ "$VARIANT" = v1 ] && [ "$DRY_RUN" -eq 0 ] && [ -f "$TARGET/config.yaml" ] &&
   [ ! -f "$TARGET/dyode.key" ] && ! grep -q '^ *key_file:' "$TARGET/config.yaml"; then
  warn "no message authentication key at $TARGET/dyode.key; DYODE will not start without it.
   First box: $PY $ROOT/dyode_setup.py --new-key, then copy the key to the other box.
   Second box: copy dyode.key from the first box beside config.yaml (chmod 600)."
fi

# ---------------------------------------------------------------- firewall
# After the wizard, which writes the config.yaml the rules are built from.
apply_firewall "$TARGET/config.yaml" "$([ -x "$VENV/bin/python" ] && echo "$VENV/bin/python" || echo "$PY")"

# ---------------------------------------------------------------- shares
# After the wizard, so its config.yaml can suggest the folders to share.
if [ "${#NFS_SHARES[@]}" -eq 0 ] && [ "${#SMB_SHARES[@]}" -eq 0 ] &&
   [ "$NO_SHARES" -eq 0 ] && [ "$DRY_RUN" -eq 0 ] && interactive; then
  interactive_shares "$TARGET/config.yaml" "$VENV/bin/python"
fi
apply_shares
