# shellcheck shell=bash
# File shares for DYODE folders, sourced by install.sh.
#
# NFS goes in /etc/exports and SMB in /etc/samba/smb.conf, each inside one
# managed block that is rewritten in full on every run.  Everything outside
# the block -- shares you made yourself -- is left exactly as it was.  A
# run that declares NFS shares replaces the NFS block and does not touch
# smb.conf, and vice versa, so the flags describe the complete set of DYODE
# shares for that protocol and re-running the same command changes nothing.
#
# Specs, from the command line or built by the interactive questions:
#   NFS  PATH=CLIENT[,CLIENT...][:ro|:rw]    10.0.2.0/24  host.lan  10.0.2.7
#   SMB  PATH=USER[:ro|:rw]                  an existing Linux account
# Shares are read-only unless :rw is given.
#
# Safety rules, all enforced before anything is written:
#   - never a folder holding DYODE's staging area (*.incoming), whose files
#     are partial and unverified, and never /
#   - NFS never to everyone (*, 0.0.0.0/0, ::/0); clients are always named
#   - SMB never guest, never root; each share names one account, which must
#     have an SMB password (set from --smb-password-file or asked for)
#   - smb.conf is checked with testparm before it is installed; an export
#     that exportfs rejects is rolled back
#   - every file changed keeps a timestamped backup beside it

SHARE_ETC="${DYODE_ETC:-/etc}"           # override: tests
EXPORTS_FILE="$SHARE_ETC/exports"
SMB_CONF="$SHARE_ETC/samba/smb.conf"
BLOCK_BEGIN="# BEGIN DYODE (managed by install.sh; this block is rewritten on each run)"
BLOCK_END="# END DYODE"

NFS_SHARES=()      # "path|clients|mode"
SMB_SHARES=()      # "path|user|mode|name"
SMB_PASSWORD_FILE=""

share_mode_split() {             # "spec:rw" -> sets SPEC and MODE
  SPEC="$1" MODE_=ro
  case "$SPEC" in
    *:rw) MODE_=rw; SPEC="${SPEC%:rw}" ;;
    *:ro) SPEC="${SPEC%:ro}" ;;
  esac
}

check_share_path() {             # check_share_path PATH [allow-missing]
  local path="$1" allow_missing="${2:-}" child
  case "$path" in /*) ;; *) die "share folder must be an absolute path: $path" ;; esac
  # The path is written into exports and smb.conf verbatim: anything beyond
  # this set could break a line or smuggle in a setting (e.g. guest ok).
  [[ "$path" =~ ^/[A-Za-z0-9._/+@-]*$ ]] ||
    die "share folders may contain only letters, digits and . _ / + @ - (got '$path')"
  path="${path%/}"
  [ -n "$path" ] || die "refusing to share /"
  case "$path" in
    *.incoming|*/.dyode_incoming)
      die "$path is a DYODE staging folder: it holds partial, unverified files and must not be shared" ;;
  esac
  if [ ! -d "$path" ]; then
    # From the command line a missing folder is created when the shares are
    # applied (DYODE would create its output folder on first run anyway).
    [ -n "$allow_missing" ] && return 0
    die "share folder $path does not exist"
  fi
  for child in "$path"/*.incoming "$path"/.dyode_incoming; do
    [ -d "$child" ] &&
      die "$path contains the staging folder $child, which holds partial, unverified files; share the folder inside it instead (e.g. the output folder itself)"
  done
  return 0
}

# Split "a,b,c" into the CLIENTS array WITHOUT wildcard expansion: an
# unquoted $list would turn a client of * into the names of local files.
split_clients() { IFS=',' read -r -a CLIENTS <<<"$1"; }

check_nfs_clients() {
  local list="$1" client
  [ -n "$list" ] || die "an NFS share needs the clients that may mount it, e.g. 10.0.2.0/24"
  split_clients "$list"
  [ "${#CLIENTS[@]}" -gt 0 ] || die "an NFS share needs at least one client"
  for client in "${CLIENTS[@]}"; do
    [ -n "$client" ] || die "empty NFS client in '$list'"
    case "$client" in
      0.0.0.0/0|::/0) die "refusing to export to everyone ($client); name the clients" ;;
    esac
    [[ "$client" =~ ^[*.]+$ ]] && die "refusing to export to everyone ($client); name the clients"
    [[ "$client" =~ ^[A-Za-z0-9._:/*-]+$ ]] || die "not a valid NFS client: '$client'"
  done
}

check_smb_user() {
  local user="$1"
  [[ "$user" =~ ^[a-z_][a-z0-9_.-]*$ ]] || die "not a valid account name: '$user'"
  [ "$user" != root ] || die "refusing to share over SMB as root; use a dedicated account"
  id -u "$user" >/dev/null 2>&1 ||
    die "account '$user' does not exist; create it first, e.g.: sudo useradd --system --no-create-home --shell /usr/sbin/nologin $user"
}

smb_share_name() {               # /srv/dyode/out -> dyode-out
  local base
  base="$(basename "$1" | tr -c 'A-Za-z0-9_-' '-' | sed 's/-*$//')"
  echo "dyode-${base:-share}" | cut -c1-32
}

add_nfs_share() {                # add_nfs_share "PATH=CLIENTS[:mode]"
  share_mode_split "$1"
  local path="${SPEC%%=*}" clients="${SPEC#*=}"
  [ "$path" != "$SPEC" ] || die "--share-nfs needs PATH=CLIENTS, got '$1'"
  check_share_path "$path" allow-missing; check_nfs_clients "$clients"
  NFS_SHARES+=("${path%/}|$clients|$MODE_")
}

add_smb_share() {                # add_smb_share "PATH=USER[:mode]" [name]
  share_mode_split "$1"
  local path="${SPEC%%=*}" user="${SPEC#*=}" name="${2:-}" s
  [ "$path" != "$SPEC" ] || die "--share-smb needs PATH=USER, got '$1'"
  check_share_path "$path" allow-missing; check_smb_user "$user"
  name="${name:-$(smb_share_name "$path")}"
  [[ "$name" =~ ^[A-Za-z0-9_-]{1,32}$ ]] || die "not a valid share name: '$name'"
  case "$name" in global|homes|printers|print\$) die "'$name' is reserved in smb.conf" ;; esac
  for s in "${SMB_SHARES[@]}"; do
    [ "${s##*|}" != "$name" ] || die "two SMB shares would both be named '$name'"
  done
  SMB_SHARES+=("${path%/}|$user|$MODE_|$name")
}

# True if PATH is the root of its own filesystem (a dedicated mount).
fs_root() {
  local p="$1" a b
  [ "$p" = / ] && return 0
  a="$(stat -c %d "$p" 2>/dev/null)" || return 1
  b="$(stat -c %d "$(dirname "$p")" 2>/dev/null)" || return 1
  [ "$a" != "$b" ]
}

nfs_block() {
  local s path clients mode opts line c check
  for s in "${NFS_SHARES[@]}"; do
    IFS='|' read -r path clients mode <<<"$s"
    # A folder inside a larger filesystem is exported with subtree_check, so
    # a client cannot use a guessed file handle to reach files outside it.
    # A dedicated filesystem needs no check (and is the stronger option).
    if fs_root "$path"; then check=no_subtree_check; else check=subtree_check; fi
    opts="$mode,sync,$check,root_squash"
    line="$path "
    split_clients "$clients"
    for c in "${CLIENTS[@]}"; do line="$line $c($opts)"; done
    echo "$line"
  done
}

smb_block() {
  local s path user mode name
  for s in "${SMB_SHARES[@]}"; do
    IFS='|' read -r path user mode name <<<"$s"
    printf '[%s]\n   path = %s\n   read only = %s\n   guest ok = no\n   valid users = %s\n   browseable = yes\n' \
      "$name" "$path" "$([ "$mode" = rw ] && echo no || echo yes)" "$user"
  done
}

# File content with the managed block replaced (or appended).
with_block() {                   # with_block FILE CONTENT -> stdout
  local file="$1" content="$2"
  if [ -f "$file" ]; then
    awk -v b="$BLOCK_BEGIN" -v e="$BLOCK_END" \
      '$0==b {skip=1; next} $0==e {skip=0; next} !skip' "$file" |
      sed -e :a -e '/^\n*$/{$d;N;ba' -e '}'      # drop trailing blank lines
  fi
  printf '\n%s\n%s\n%s\n' "$BLOCK_BEGIN" "$content" "$BLOCK_END"
}

install_file() {                 # install_file FILE NEWCONTENT -> echoes backup
  local file="$1" new="$2" backup=""
  # Unchanged: write nothing and keep no backup, so a repeat run is a no-op.
  if [ -f "$file" ] && cmp -s "$file" "$new"; then echo "unchanged"; return; fi
  mkdir -p "$(dirname "$file")"
  if [ -f "$file" ]; then
    backup="$file.dyode-bak-$(date +%Y%m%d-%H%M%S)"
    cp -p "$file" "$backup"
  fi
  cat "$new" > "$file"
  echo "$backup"
}

smb_password_for() {             # look up USER in --smb-password-file
  local user="$1" line
  [ -n "$SMB_PASSWORD_FILE" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    [ "${line%%:*}" = "$user" ] && { printf '%s' "${line#*:}"; return 0; }
  done < "$SMB_PASSWORD_FILE"
  return 1
}

smb_has_account() { pdbedit -L -u "$1" >/dev/null 2>&1; }

# Confirm every SMB user will end up with a password, before writing.
plan_smb_passwords() {
  local s user seen=" "
  SMB_PW_USERS=()
  for s in "${SMB_SHARES[@]}"; do
    IFS='|' read -r _ user _ _ <<<"$s"
    case "$seen" in *" $user "*) continue ;; esac
    seen="$seen$user "
    if smb_password_for "$user" >/dev/null; then SMB_PW_USERS+=("$user:file")
    elif smb_has_account "$user"; then SMB_PW_USERS+=("$user:keep")
    elif interactive; then SMB_PW_USERS+=("$user:ask")
    else die "no SMB password for '$user': add a '$user:PASSWORD' line to --smb-password-file"
    fi
  done
}

describe_shares() {
  local s path clients user mode name
  for s in "${NFS_SHARES[@]}"; do
    IFS='|' read -r path clients mode <<<"$s"
    say "  share:        NFS $path -> $clients ($mode)"
  done
  for s in "${SMB_SHARES[@]}"; do
    IFS='|' read -r path user mode name <<<"$s"
    say "  share:        SMB //$(hostname)/$name -> $path, account $user ($mode)"
  done
}

apply_shares() {
  local tmp backup out
  [ "${#NFS_SHARES[@]}" -gt 0 ] || [ "${#SMB_SHARES[@]}" -gt 0 ] || return 0
  step "file shares"
  if [ "${#NFS_SHARES[@]}" -gt 0 ]; then
    command -v exportfs >/dev/null 2>&1 || die "NFS shares need the NFS server: add --with nfs"
  fi
  if [ "${#SMB_SHARES[@]}" -gt 0 ]; then
    command -v testparm >/dev/null 2>&1 || die "SMB shares need Samba: add --with smb"
    plan_smb_passwords
  fi
  local s path
  for s in "${NFS_SHARES[@]}" "${SMB_SHARES[@]}"; do
    path="${s%%|*}"
    if [ ! -d "$path" ]; then
      if [ "$DRY_RUN" -eq 1 ]; then say "   would create $path"
      else mkdir -p "$path" && say "   created $path"; fi
    fi
  done
  tmp="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" RETURN

  if [ "${#NFS_SHARES[@]}" -gt 0 ]; then
    with_block "$EXPORTS_FILE" "$(nfs_block)" > "$tmp/exports"
    if [ "$DRY_RUN" -eq 1 ]; then
      say "   would write $EXPORTS_FILE:"; sed -n "/^$BLOCK_BEGIN/,/^$BLOCK_END/p" "$tmp/exports" | sed 's/^/     /'
    else
      backup="$(install_file "$EXPORTS_FILE" "$tmp/exports")"
      if [ "$backup" = unchanged ]; then
        say "   NFS: $EXPORTS_FILE already up to date"
      elif ! out="$(exportfs -ra 2>&1)"; then
        if [ -n "$backup" ]; then cp -p "$backup" "$EXPORTS_FILE"; exportfs -ra >/dev/null 2>&1 || true; fi
        die "exportfs rejected the new exports, so the previous $EXPORTS_FILE was restored: $out"
      else
        say "   NFS: $EXPORTS_FILE updated and exported${backup:+ (previous copy: $backup)}"
      fi
    fi
  fi

  if [ "${#SMB_SHARES[@]}" -gt 0 ]; then
    with_block "$SMB_CONF" "$(smb_block)" > "$tmp/smb.conf"
    # Checked before it replaces the live file, so a bad config never lands.
    if ! out="$(testparm -s "$tmp/smb.conf" 2>&1 >/dev/null)"; then
      die "testparm rejected the new smb.conf, which was not installed: $(printf '%s' "$out" | tail -3)"
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
      say "   would write $SMB_CONF:"; sed -n "/^$BLOCK_BEGIN/,/^$BLOCK_END/p" "$tmp/smb.conf" | sed 's/^/     /'
    else
      backup="$(install_file "$SMB_CONF" "$tmp/smb.conf")"
      if [ "$backup" = unchanged ]; then say "   SMB: $SMB_CONF already up to date"
      else say "   SMB: $SMB_CONF updated, passes testparm${backup:+ (previous copy: $backup)}"; fi
    fi
    local entry user how pw
    for entry in "${SMB_PW_USERS[@]}"; do
      user="${entry%%:*}" how="${entry#*:}"
      if [ "$DRY_RUN" -eq 1 ]; then say "   would set the SMB password for $user ($how)"; continue; fi
      case "$how" in
        file) pw="$(smb_password_for "$user")"
              printf '%s\n%s\n' "$pw" "$pw" | smbpasswd -s -a "$user" >/dev/null ||
                die "smbpasswd could not set the password for $user"
              say "   SMB: password for $user set from ${SMB_PASSWORD_FILE}" ;;
        ask)  say "   SMB: choose a password for $user (used to connect to the share)"
              smbpasswd -a "$user" || die "smbpasswd could not set the password for $user" ;;
        keep) say "   SMB: $user already has an SMB password; kept" ;;
      esac
    done
    if [ "$DRY_RUN" -eq 0 ]; then
      if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet smbd 2>/dev/null; then
        systemctl reload smbd && say "   SMB: smbd reloaded"
      else
        say "   SMB: smbd is not running; the shares apply when it starts"
      fi
    fi
  fi
  share_summary
}

share_summary() {
  local s path clients user mode name host
  host="$(hostname)"
  say ""
  say "Connect with:"
  for s in "${NFS_SHARES[@]}"; do
    IFS='|' read -r path clients mode <<<"$s"
    say "  sudo mount -t nfs $host:$path /mnt/dyode"
  done
  for s in "${SMB_SHARES[@]}"; do
    IFS='|' read -r path user mode name <<<"$s"
    say "  sudo mount -t cifs //$host/$name /mnt/dyode -o username=$user"
    say "  smbclient //$host/$name -U $user"
    [ "$mode" = rw ] && say "  (read-write: $user also needs write permission on $path itself)"
  done
  for s in "${NFS_SHARES[@]}"; do
    IFS='|' read -r path clients mode <<<"$s"
    [ "$mode" = rw ] && say "  (read-write NFS maps remote root to nobody; make $path writable by the clients' users)"
  done
  return 0
}

# Folder modules' in/out paths from config.yaml, as default suggestions.
config_folders() {
  local cfg="$1" python="$2"
  [ -f "$cfg" ] && [ -x "$python" ] || return 0
  "$python" - "$cfg" <<'PYEOF' 2>/dev/null || true
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
for name, props in (cfg.get("modules") or {}).items():
    if isinstance(props, dict) and str(props.get("type", "")).lower() == "folder":
        for key in ("out", "in"):
            if props.get(key):
                print("%s\t%s (%s folder of '%s')" % (props[key], key, key, name))
PYEOF
}

ask_line() {                     # ask_line "question" default -> stdout
  local answer
  printf '%s%s: ' "$1" "${2:+ [$2]}" >&2
  read -r answer || die "input ended"
  echo "${answer:-$2}"
}

interactive_shares() {           # interactive_shares CONFIG PYTHON
  local have_nfs=0 have_smb=0 folders default path mode proto clients user name
  command -v exportfs >/dev/null 2>&1 && have_nfs=1
  command -v testparm >/dev/null 2>&1 && have_smb=1
  [ "$have_nfs" -eq 1 ] || [ "$have_smb" -eq 1 ] || return 0
  say ""
  say "Set up a file share now?  Either box can share a DYODE folder: the output"
  say "box its received files (usually read-only), the input box a drop folder"
  say "for files to send (read-write)."
  [ "$(ask_line "Set up a share? (y/n)" n)" = y ] || return 0
  # Each run declares DYODE's complete set of shares per protocol, so say
  # what is there now and that it will be replaced.
  local current
  current="$( { [ -f "$EXPORTS_FILE" ] && sed -n "/^$BLOCK_BEGIN/,/^$BLOCK_END/{/^#/d;s/^/  NFS  /;p}" "$EXPORTS_FILE"
                [ -f "$SMB_CONF" ] && sed -n "/^$BLOCK_BEGIN/,/^$BLOCK_END/{/^\[/s/^/  SMB  /p}" "$SMB_CONF"; } 2>/dev/null)"
  if [ -n "$current" ]; then
    say ""
    say "DYODE shares already set up (for each protocol you configure now, these"
    say "are replaced by what you enter; include any you want to keep):"
    printf '%s\n' "$current" | cut -c1-100
  fi
  folders="$(config_folders "$1" "$2")"
  while true; do
    say ""
    default=""
    if [ -n "$folders" ]; then
      say "Folders in config.yaml:"
      printf '%s\n' "$folders" | cut -f2 | sed 's/^/  /' >&2
      default="$(printf '%s\n' "$folders" | head -1 | cut -f1)"
    fi
    path="$(ask_line "Folder to share" "$default")"
    if [ ! -d "$path" ] && [ -n "$path" ]; then
      [ "$(ask_line "  $path does not exist. Create it? (y/n)" y)" = y ] && mkdir -p "$path"
    fi
    check_share_path "$path"
    mode="$(ask_line "Read-only or read-write? (ro/rw)" ro)"
    case "$mode" in ro|rw) ;; *) die "answer ro or rw" ;; esac
    if [ "$have_nfs" -eq 1 ] && [ "$have_smb" -eq 1 ]; then
      proto="$(ask_line "Share over nfs, smb or both?" both)"
    elif [ "$have_nfs" -eq 1 ]; then proto=nfs
    else proto=smb; fi
    case "$proto" in nfs|smb|both) ;; *) die "answer nfs, smb or both" ;; esac
    if [ "$proto" != smb ]; then
      clients="$(ask_line "NFS: which machines may mount it (address, host or subnet; comma-separated)")"
      add_nfs_share "$path=$clients:$mode"
    fi
    if [ "$proto" != nfs ]; then
      user="$(ask_line "SMB: account allowed to connect (an existing Linux user)" "${SUDO_USER:-}")"
      name="$(ask_line "SMB: share name" "$(smb_share_name "$path")")"
      add_smb_share "$path=$user:$mode" "$name"
    fi
    [ "$(ask_line "Share another folder? (y/n)" n)" = y ] || break
  done
  say ""; describe_shares
}
