#!/bin/bash
#  files.sh - the engine behind the dashboard's Files page (Space-Lens-style
#  disk map + find / back up / remove)
#  ============================================================================
#  index  : one pass over the startup disk -> $OUTDIR/dirs.tsv (kB<TAB>path for
#           EVERY folder) + files.tsv (bytes<TAB>mtime<TAB>path for files >= 1 MB)
#           + meta.txt. The page then navigates instantly from the index; the
#           web server never has to read a protected folder itself.
#  backup : rsync the selected paths onto a destination volume, keeping the
#           folder structure, then verify (count + size, optional checksum);
#           manifest + report on the stick.
#  trash  : move selected paths into ~/.Trash/BenchTool-Files-<time>/ (recoverable)
#  delete : rm -rf selected paths - PERMANENT, only after the page's double confirm;
#           refuses the roots that hold macOS or every user's data.
#
#    files.sh index  <outdir>  [sudo] [log]
#    files.sh backup <job.json> <out.json> [sudo] [log]     {"paths":[..],"dest":"/Volumes/X","checksum":false}
#    files.sh trash  <job.json> <out.json> [sudo] [log]     {"paths":[..]}
#    files.sh delete <job.json> <out.json> [sudo] [log]     {"paths":[..]}
#    files.sh prune  <outdir> <out.json.removed>             drop the removed paths from the index, mark it stale
#  bash 3.2, BSD tools (GNU fallbacks only for the Linux sandbox). No python.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/../_shared/benchmac.sh" ] && . "$HERE/../_shared/benchmac.sh"
CMD="${1:-index}"; shift
HAVE_SUDO=0; PROG=""
FV_VERSION="1.0"
TS="$(date +%Y%m%d-%H%M%S)"
REPORT_DIR="$HERE/Files-Reports"
log() { printf '%s\n' "$*" >&2; [ -n "$PROG" ] && printf '%s %s\n' "$(date '+%H:%M:%S')" "$*" >> "$PROG"; return 0; }
js() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
num() { case "$1" in ''|*[!0-9.-]*) printf '0' ;; *) printf '%s' "$1" ;; esac; }
su_() { if [ "$HAVE_SUDO" = "1" ]; then sudo -n "$@"; else "$@"; fi; }
human() { awk -v b="${1:-0}" 'BEGIN{ if (b>=1073741824) printf "%.2f GB", b/1073741824; else if (b>=1048576) printf "%.0f MB", b/1048576; else if (b>=1024) printf "%.0f KB", b/1024; else printf "%d B", b }'; }
psize() { local k; k="$(su_ du -sk "$1" 2>/dev/null | awk '{ print $1 }')"; printf '%s' "$(( ${k:-0} * 1024 ))"; }
hostname_safe() { local h; h="$(scutil --get ComputerName 2>/dev/null | tr -d '\n' | tr -c 'A-Za-z0-9._-' '_' | cut -c1-40)"; [ -n "$h" ] || h="$(hostname -s 2>/dev/null | tr -c 'A-Za-z0-9._-' '_')"; printf '%s' "${h:-mac}"; }
is_bsd_stat() { stat -f '%z' / >/dev/null 2>&1; }
# JSON list field -> lines (paths may contain spaces; JSON escapes \" and \/ are undone)
jget_list() { grep -o "\"$1\" *: *\[[^]]*\]" "$2" | sed 's/^[^[]*\[//; s/\]$//' | tr ',' '\n' | sed 's/^ *"//; s/" *$//; s/\\\//\//g; s/\\"/"/g' | grep .; }
jget_str()  { grep -o "\"$1\" *: *\"[^\"]*\"" "$2" | head -n1 | sed 's/^[^:]*: *"//; s/"$//; s/\\\//\//g'; }
jget_bool() { grep -o -E "\"$1\" *: *(true|false)" "$2" | head -n1 | grep -q true; }

# Paths that must never be moved/deleted as a whole (the page can still go INTO them)
protected() {
  local p="${1%/}"
  case "$p" in /Users/*/*) ;; /Users/*) return 0 ;; esac      # a whole user folder is never removable; things inside it are
  case "$p" in
    ""|/|/System|/System/*|/Library|/Users|/Applications|/private|/private/*|/usr|/usr/*|/bin|/sbin|/etc|/var|/var/*|/opt|/Volumes|/dev|/cores|"$HOME/Library"|*".photoslibrary"|*"/.Trash"|/Volumes/*/.Trashes) return 0 ;;
    /Volumes/*/*) return 1 ;;
    /Volumes/*) return 0 ;;                                        # a whole volume root
  esac
  return 1
}

# ---- index --------------------------------------------------------------------
do_index() {
  local out="$1" tot used root="${BM_FILES_ROOT:-/}"     # BM_FILES_ROOT: test hook only
  mkdir -p "$out" 2>/dev/null
  log "index: folder sizes (du over the startup disk - a minute or two on a full SSD)..."
  # Every folder with its size. Skip other mounted volumes (and the Data volume's second
  # path under /System/Volumes - it is reached through the firmlinks /Users, /Applications...).
  if du -I x /dev/null >/dev/null 2>&1; then
    su_ du -k -I Volumes -I dev -I .Spotlight-V100 -I .fseventsd -I .DocumentRevisions-V100 "$root" 2>/dev/null > "$out/dirs.tmp"
  else   # GNU du (sandbox)
    su_ du -k --exclude=/proc --exclude=/sys --exclude=/dev --exclude=/run --exclude='*/Volumes' "$root" 2>/dev/null > "$out/dirs.tmp"
  fi
  mv -f "$out/dirs.tmp" "$out/dirs.tsv"
  log "index: $(wc -l < "$out/dirs.tsv" | tr -d ' ') folders sized"
  log "index: files over 1 MB..."
  if is_bsd_stat; then
    su_ find "$root" \( -path /Volumes -o -path /System/Volumes -o -path /dev -o -path /private/var/vm -o -name .Spotlight-V100 -o -name .fseventsd \) -prune -o -type f -size +1M -print0 2>/dev/null \
      | su_ xargs -0 stat -f '%z%t%m%t%N' 2>/dev/null > "$out/files.tmp"
  else
    su_ find "$root" \( -path /proc -o -path /sys -o -path /dev -o -path /run -o -path '*/Volumes' \) -prune -o -type f -size +1M -print0 2>/dev/null \
      | su_ xargs -0 stat -c '%s	%Y	%n' 2>/dev/null > "$out/files.tmp"
  fi
  mv -f "$out/files.tmp" "$out/files.tsv"
  log "index: $(wc -l < "$out/files.tsv" | tr -d ' ') large files listed"
  tot="$(df -k / 2>/dev/null | tail -n1 | awk '{ print $2 * 1024 }')"; used="$(df -k / 2>/dev/null | tail -n1 | awk '{ print $3 * 1024 }')"
  printf 'ts=%s\nhome=%s\nhost=%s\ntotal=%s\nused=%s\nsudo=%s\ndirs=%s\nfiles=%s\nvolume=%s\nroot=%s\n' "$(date +%s)" "$HOME" "$(hostname_safe)" "$tot" "$used" "$HAVE_SUDO" \
    "$(wc -l < "$out/dirs.tsv" | tr -d ' ')" "$(wc -l < "$out/files.tsv" | tr -d ' ')" "$(diskutil info / 2>/dev/null | grep -m1 'Volume Name' | sed 's/.*: *//')" "$root" > "$out/meta.txt"
  log "index done: $(human "$used") used of $(human "$tot")"
}

# ---- manifest helper: bytes<TAB>path for every file under a path -------------
manifest_of() {   # $1 root path  -> lines
  if is_bsd_stat; then su_ find "$1" -type f -print0 2>/dev/null | su_ xargs -0 stat -f '%z%t%N' 2>/dev/null
  else su_ find "$1" -type f -print0 2>/dev/null | su_ xargs -0 stat -c '%s	%n' 2>/dev/null; fi
}

# ---- backup -------------------------------------------------------------------
do_backup() {
  local job="$1" out="$2" dest paths p host base root n=0 nb=0 errors=0 verified=0 mismatched=0 chk=0 rel dst_p
  dest="$(jget_str dest "$job")"; paths="$(jget_list paths "$job")"; jget_bool checksum "$job" && chk=1
  host="$(hostname_safe)"; mkdir -p "$REPORT_DIR" 2>/dev/null
  [ -n "$dest" ] && [ -d "$dest" ] || { printf '{"ok":false,"error":"destination not mounted"}' > "$out"; log "destination missing: $dest"; return 1; }
  case "$dest" in /Volumes/*|/Users/*|/private/tmp/*|/tmp/*) ;; *) printf '{"ok":false,"error":"destination must be a mounted volume"}' > "$out"; return 1 ;; esac
  root="$dest/BenchTool-Backup-${host}-${TS}"; mkdir -p "$root" 2>/dev/null || { printf '{"ok":false,"error":"cannot write to destination"}' > "$out"; return 1; }
  base="$REPORT_DIR/Backup_${host}_${TS}"; : > "$base.manifest"
  log "backup -> $root"
  local list=""
  while IFS= read -r p; do
    [ -n "$p" ] && [ -e "$p" ] || continue
    case "$p" in "$dest"*|"$root"*) log "skipped (inside the destination): $p"; continue;; esac
    rel="${p#/}"; dst_p="$root/$rel"; mkdir -p "$(dirname "$dst_p")" 2>/dev/null
    log "copying $p ($(human "$(psize "$p")"))..."
    if [ -d "$p" ]; then
      su_ rsync -aE --no-perms --no-owner --no-group "$p/" "$dst_p/" 2>>"${PROG:-/dev/null}" || errors=$((errors+1))
    else
      su_ rsync -aE --no-perms --no-owner --no-group "$p" "$dst_p" 2>>"${PROG:-/dev/null}" || errors=$((errors+1))
    fi
    # verify: every source file present at the destination with the same size (+ checksum when asked)
    local sc=0 dc=0 sb=0 db=0
    while IFS="$(printf '\t')" read -r b f; do
      [ -n "$f" ] || continue; sc=$((sc+1)); sb=$((sb+b))
      local df_="$root/${f#/}" bb
      if [ -f "$df_" ]; then
        if is_bsd_stat; then bb="$(stat -f %z "$df_" 2>/dev/null)"; else bb="$(stat -c %s "$df_" 2>/dev/null)"; fi
        if [ "$bb" = "$b" ]; then dc=$((dc+1)); db=$((db+b)); verified=$((verified+1))
          if [ "$chk" = 1 ]; then
            local h1 h2; h1="$(su_ shasum -a 256 "$f" 2>/dev/null | cut -d' ' -f1)"; h2="$(shasum -a 256 "$df_" 2>/dev/null | cut -d' ' -f1)"
            [ -n "$h1" ] && [ "$h1" = "$h2" ] || { mismatched=$((mismatched+1)); printf 'MISMATCH\t%s\n' "$f" >> "$base.manifest"; }
          fi
        else mismatched=$((mismatched+1)); printf 'MISMATCH\t%s\n' "$f" >> "$base.manifest"; fi
      else mismatched=$((mismatched+1)); printf 'MISSING\t%s\n' "$f" >> "$base.manifest"; fi
      printf '%s\t%s\n' "$b" "$f" >> "$base.manifest"
    done <<EOF_M
$(manifest_of "$p")
EOF_M
    n=$((n+1)); nb=$((nb+sb))
    log "  $sc file(s) / $(human "$sb") -> $dc verified$( [ "$sc" != "$dc" ] && printf ' (%s MISSING/MISMATCH)' "$((sc-dc))")"
    list="$list$([ -n "$list" ] && printf ',')$(printf '{"p":"%s","files":%s,"bytes":%s,"verified":%s}' "$(js "$p")" "$sc" "$sb" "$dc")"
  done <<EOF_P
$paths
EOF_P
  sync
  cp -f "$base.manifest" "$root/BenchTool-manifest.txt" 2>/dev/null
  local ok=true; { [ "$errors" -gt 0 ] || [ "$mismatched" -gt 0 ]; } && ok=false
  {
    printf 'Backup report  -  BenchTool Mac Diagnostics Files %s\n======================================================================\n' "$FV_VERSION"
    printf 'Generated : %s\nComputer  : %s\nTo        : %s\nVerify    : size%s\n\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$host" "$root" "$([ "$chk" = 1 ] && printf ' + SHA-256')"
    printf 'RESULT    : %s  -  %s item(s), %s, %s file(s) verified, %s missing/mismatched, %s rsync error(s)\n' "$([ "$ok" = true ] && printf 'VERIFIED' || printf 'CHECK - NOT FULLY VERIFIED')" "$n" "$(human "$nb")" "$verified" "$mismatched" "$errors"
    printf '\n---- ITEMS ----\n'; printf '%s\n' "$paths"
    printf '\n---- PROBLEMS ----\n'; grep -E '^(MISSING|MISMATCH)' "$base.manifest" | head -n 200; grep -q -E '^(MISSING|MISMATCH)' "$base.manifest" || printf '(none)\n'
    printf '\n(full file manifest: %s and BenchTool-manifest.txt inside the backup folder)\n' "$(basename "$base.manifest")"
    [ -n "$PROG" ] && { printf '\n---- LOG ----\n'; cat "$PROG"; }
  } > "$base.txt"
  printf '{"tool":"Files","kind":"backup","version":"%s","generated":"%s","host":"%s","verdict":"%s","dest":"%s","items":%s,"bytes":%s,"verified":%s,"mismatched":%s,"errors":%s,"paths":[%s]}\n' \
    "$FV_VERSION" "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$(js "$host")" "$([ "$ok" = true ] && printf 'BACKUP VERIFIED' || printf 'BACKUP NOT VERIFIED')" "$(js "$root")" "$n" "$nb" "$verified" "$mismatched" "$errors" "$list" > "$base.json"
  printf '{"ok":%s,"dest":"%s","items":%s,"bytes":%s,"verified":%s,"mismatched":%s,"errors":%s,"report":"%s"}\n' "$ok" "$(js "$root")" "$n" "$nb" "$verified" "$mismatched" "$errors" "$(js "$(basename "$base.txt")")" > "$out.tmp" && mv -f "$out.tmp" "$out"
  log "backup done: $n item(s), $(human "$nb"), $verified verified, $mismatched problems"
}

# ---- trash / delete -------------------------------------------------------------
do_remove() {   # $1 = trash|delete
  local mode="$1" job="$2" out="$3" p n=0 nb=0 errors=0 refused=0 sz host base list="" troot free0 free1
  paths="$(jget_list paths "$job")"; host="$(hostname_safe)"; mkdir -p "$REPORT_DIR" 2>/dev/null
  troot="$HOME/.Trash/BenchTool-Files-$TS"; free0="$(df -k / 2>/dev/null | tail -n1 | awk '{ print $4 * 1024 }')"
  base="$REPORT_DIR/$([ "$mode" = delete ] && printf 'Delete' || printf 'Trash')_${host}_${TS}"; : > "$base.manifest"; : > "$out.removed"
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    if protected "$p"; then log "REFUSED (protected - macOS, all users, the Photos library, the Trash): $p"; errors=$((errors+1)); refused=$((refused+1)); printf 'REFUSED\t0\t%s\n' "$p" >> "$base.manifest"; continue; fi
    [ -e "$p" ] || { log "not found: $p"; errors=$((errors+1)); continue; }
    sz="$(psize "$p")"
    if [ "$mode" = "delete" ]; then
      if su_ rm -rf "$p" 2>/dev/null && [ ! -e "$p" ]; then n=$((n+1)); nb=$((nb+sz)); log "deleted $p ($(human "$sz"))"; printf 'DELETED\t%s\t%s\n' "$sz" "$p" >> "$base.manifest"; printf '%s\n' "$p" >> "$out.removed"
      else errors=$((errors+1)); log "could not delete: $p"; fi
    else
      case "$p" in "$HOME"/*) ;; *) log "trash only takes paths inside the home folder (use Delete for others): $p"; errors=$((errors+1)); continue;; esac
      mkdir -p "$troot" 2>/dev/null
      if su_ mv -f "$p" "$troot/" 2>/dev/null; then n=$((n+1)); nb=$((nb+sz)); log "moved to Trash: $p ($(human "$sz"))"; printf 'TRASHED\t%s\t%s\n' "$sz" "$p" >> "$base.manifest"; printf '%s\n' "$p" >> "$out.removed"
      else errors=$((errors+1)); log "could not move: $p"; fi
    fi
    list="$list$([ -n "$list" ] && printf ',')$(printf '{"p":"%s","bytes":%s}' "$(js "$p")" "$sz")"
  done <<EOF_P
$paths
EOF_P
  sync; sleep 1; free1="$(df -k / 2>/dev/null | tail -n1 | awk '{ print $4 * 1024 }')"
  local freed=$((free1 - free0)); [ "$freed" -lt 0 ] && freed=0
  {
    printf '%s report  -  BenchTool Mac Diagnostics Files %s\n======================================================================\n' "$([ "$mode" = delete ] && printf 'Delete' || printf 'Move to Trash')" "$FV_VERSION"
    printf 'Generated : %s\nComputer  : %s\nMode      : %s\n\nRESULT    : %s item(s), %s  (free before %s, after %s)\nErrors    : %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$host" "$([ "$mode" = delete ] && printf 'PERMANENT delete' || printf "moved to Trash (recoverable in $troot)")" "$n" "$(human "$nb")" "$(human "$free0")" "$(human "$free1")" "$errors"
    printf '\n---- ITEMS (action, size, path) ----\n'; awk -F'\t' '{ printf "%-8s %12s  %s\n", $1, $2, $3 }' "$base.manifest"
    [ -n "$PROG" ] && { printf '\n---- LOG ----\n'; cat "$PROG"; }
  } > "$base.txt"
  printf '{"tool":"Files","kind":"%s","version":"%s","generated":"%s","host":"%s","verdict":"%s","items":%s,"bytes":%s,"errors":%s,"freed":%s,"trash_folder":"%s","paths":[%s]}\n' \
    "$mode" "$FV_VERSION" "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$(js "$host")" "$([ "$mode" = delete ] && printf '%s DELETED' "$(human "$nb")" || printf '%s TO TRASH' "$(human "$nb")")" "$n" "$nb" "$errors" "$freed" "$(js "$troot")" "$list" > "$base.json"
  printf '{"ok":%s,"mode":"%s","items":%s,"bytes":%s,"errors":%s,"refused":%s,"freed":%s,"trash_folder":"%s","report":"%s"}\n' "$([ "$errors" = 0 ] && printf true || printf false)" "$mode" "$n" "$nb" "$errors" "$refused" "$freed" "$(js "$troot")" "$(js "$(basename "$base.txt")")" > "$out.tmp" && mv -f "$out.tmp" "$out"
  log "$mode done: $n item(s), $(human "$nb"), $errors error(s)"
}

# after a trash/delete: drop the removed paths (and everything under them) from the
# index so the page keeps working, and flag the index stale (parent sizes are now off)
do_prune() {
  local idx="$1" list="$2"      # list = one removed path per line (do_remove writes <out>.removed)
  [ -s "$idx/dirs.tsv" ] && [ -s "$list" ] || return 0
  awk -F'\t' -v L="$list" 'BEGIN{ while ((getline p < L) > 0) if (p != "") P[++n]=p }
    { for (i=1;i<=n;i++) if ($2==P[i] || index($2, P[i] "/")==1) next; print }' "$idx/dirs.tsv" > "$idx/dirs.tmp" && mv -f "$idx/dirs.tmp" "$idx/dirs.tsv"
  awk -F'\t' -v L="$list" 'BEGIN{ while ((getline p < L) > 0) if (p != "") P[++n]=p }
    { for (i=1;i<=n;i++) if ($3==P[i] || index($3, P[i] "/")==1) next; print }' "$idx/files.tsv" > "$idx/files.tmp" && mv -f "$idx/files.tmp" "$idx/files.tsv"
  grep -q '^stale=' "$idx/meta.txt" 2>/dev/null || printf 'stale=1\n' >> "$idx/meta.txt"
}

case "$CMD" in
  prune)  do_prune "$1" "$2" ;;
  index)  OUT="$1"; HAVE_SUDO="${2:-0}"; PROG="${3:-}"; do_index "$OUT" ;;
  backup) JOB="$1"; OUT="$2"; HAVE_SUDO="${3:-0}"; PROG="${4:-}"; do_backup "$JOB" "$OUT" ;;
  trash)  JOB="$1"; OUT="$2"; HAVE_SUDO="${3:-0}"; PROG="${4:-}"; do_remove trash "$JOB" "$OUT" ;;
  delete) JOB="$1"; OUT="$2"; HAVE_SUDO="${3:-0}"; PROG="${4:-}"; do_remove delete "$JOB" "$OUT" ;;
  *) printf 'usage: files.sh index <outdir> [sudo] [log] | backup|trash|delete <job.json> <out.json> [sudo] [log]\n'; exit 2 ;;
esac
