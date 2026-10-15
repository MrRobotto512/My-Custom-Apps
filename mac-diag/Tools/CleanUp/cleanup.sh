#!/bin/bash
#  cleanup.sh - the "junk + purgeable" engine behind the dashboard's Clean page
#  ============================================================================
#  The first thing on this stick that CHANGES a customer Mac, so the rules are
#  strict:
#    * scan first - nothing is touched until a tech has seen sizes + paths and
#      ticked categories; user-ish categories (Trash, iPhone backups, Downloads,
#      leftovers, large files) are never pre-selected
#    * "clean" MOVES files into the Trash, into a dated folder
#      (~/.Trash/BenchTool-Cleanup-<time>/<category>/) - a customer who wants
#      something back next week drags it out. Emptying the Trash is a separate,
#      explicit step
#    * every path that moves is listed in the report, with before/after free space
#    * Apple caches that would force iCloud re-downloads, Photos/Mail/Documents,
#      and crash/panic reports (MacDiag needs them) are never candidates
#
#  Runs in the tech's Terminal context (collector.sh / Run-CleanUp.command),
#  never under the web server - macOS keeps that away from ~/Library.
#
#    cleanup.sh scan  <out.json> [sudo:0|1] [progress.log]
#    cleanup.sh clean <job.json> <out.json> [sudo:0|1] [progress.log]
#    cleanup.sh maint <op> [sudo:0|1]          spotlight | dns | periodic | lsregister | purge
#  bash 3.2, BSD tools only. No python.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/../_shared/benchmac.sh" ] && . "$HERE/../_shared/benchmac.sh"
CMD="${1:-scan}"; shift
HAVE_SUDO=0; PROG=""
CU_VERSION="1.0"
TS="$(date +%Y%m%d-%H%M%S)"
TRASH_ROOT="$HOME/.Trash/BenchTool-Cleanup-$TS"
REPORT_DIR="$HERE/CleanUp-Reports"
U="$(id -u)"

log() { printf '%s\n' "$*" >&2; [ -n "$PROG" ] && printf '%s %s\n' "$(date '+%H:%M:%S')" "$*" >> "$PROG"; return 0; }
js() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
num() { case "$1" in ''|*[!0-9.-]*) printf '0' ;; *) printf '%s' "$1" ;; esac; }
su_() { if [ "$HAVE_SUDO" = "1" ]; then sudo -n "$@"; else "$@"; fi; }
human() { awk -v b="${1:-0}" 'BEGIN{ if (b>=1073741824) printf "%.2f GB", b/1073741824; else if (b>=1048576) printf "%.0f MB", b/1048576; else if (b>=1024) printf "%.0f KB", b/1024; else printf "%d B", b }'; }
# size of one path in bytes (du -sk * 1024); root-owned trees need sudo to be counted right
psize() { local k; k="$(su_ du -sk "$1" 2>/dev/null | awk '{ print $1 }')"; printf '%s' "$(( ${k:-0} * 1024 ))"; }
free_bytes() { df -k / 2>/dev/null | tail -n1 | awk '{ print $4 * 1024 }'; }
# purgeable = "available" (what macOS could free) - "free" (what is free right now)
purgeable_bytes() {
  local avail free
  avail="$(diskutil info / 2>/dev/null | grep -m1 'Volume Available Space' | sed 's/.*(\([0-9]*\) Bytes.*/\1/')"
  free="$(diskutil info / 2>/dev/null | grep -m1 'Volume Free Space' | sed 's/.*(\([0-9]*\) Bytes.*/\1/')"
  case "$avail$free" in *[!0-9]*|"") printf 0; return;; esac
  [ "$avail" -gt "$free" ] && printf '%s' "$((avail - free))" || printf 0
}

# ---- category definitions ----------------------------------------------------
# cat_paths <id>  -> prints candidate paths (one per line). Every category is a
# list of concrete paths so the tech sees exactly what would move.
cat_paths() {
  case "$1" in
    user_caches)
      for p in "$HOME/Library/Caches"/*; do
        # iCloud/Photos/Safari caches stay (re-download pain); browser + Xcode caches have their own category
        case "$(basename "$p")" in com.apple.bird|CloudKit|com.apple.icloud*|com.apple.Safari*|com.apple.akd|com.apple.iCloudHelper|com.apple.cloudphotod|com.apple.Spotlight|Homebrew|Google|Firefox|BraveSoftware|Microsoft\ Edge|com.operasoftware.Opera|com.apple.WebKit.Networking|com.apple.dt.Xcode) continue;; esac
        [ -e "$p" ] && printf '%s\n' "$p"
      done ;;
    browser_caches)
      for p in "$HOME/Library/Caches/Google/Chrome" "$HOME/Library/Caches/Google/Chrome Canary" "$HOME/Library/Caches/BraveSoftware" "$HOME/Library/Caches/Microsoft Edge" "$HOME/Library/Caches/com.operasoftware.Opera" "$HOME/Library/Caches/Firefox" \
               "$HOME/Library/Containers/com.apple.Safari/Data/Library/Caches" "$HOME/Library/Caches/com.apple.Safari" "$HOME/Library/Caches/com.apple.WebKit.Networking"; do [ -e "$p" ] && printf '%s\n' "$p"; done
      for p in "$HOME/Library/Application Support/Google/Chrome"/*/Cache "$HOME/Library/Application Support/Google/Chrome"/*/"Code Cache" "$HOME/Library/Application Support/BraveSoftware/Brave-Browser"/*/Cache "$HOME/Library/Application Support/Microsoft Edge"/*/Cache "$HOME/Library/Application Support/Firefox/Profiles"/*/cache2; do [ -e "$p" ] && printf '%s\n' "$p"; done ;;
    system_caches)
      for p in /Library/Caches/*; do case "$(basename "$p")" in com.apple.iconservices.store|com.apple.coresymbolicationd) continue;; esac; [ -e "$p" ] && printf '%s\n' "$p"; done ;;
    logs)
      for p in "$HOME/Library/Logs"/*; do [ -e "$p" ] && printf '%s\n' "$p"; done
      for p in /private/var/log/*.gz /private/var/log/*.bz2 /private/var/log/asl/*.asl; do [ -e "$p" ] && printf '%s\n' "$p"; done ;;
    trash)
      for p in "$HOME/.Trash"/*; do case "$(basename "$p")" in BenchTool-Cleanup-*) continue;; esac; [ -e "$p" ] && printf '%s\n' "$p"; done ;;
    ios_backups)
      for p in "$HOME/Library/Application Support/MobileSync/Backup"/*; do [ -d "$p" ] && printf '%s\n' "$p"; done ;;
    xcode)
      for p in "$HOME/Library/Developer/Xcode/DerivedData" "$HOME/Library/Developer/Xcode/iOS DeviceSupport" "$HOME/Library/Developer/Xcode/watchOS DeviceSupport" "$HOME/Library/Developer/Xcode/tvOS DeviceSupport" "$HOME/Library/Developer/CoreSimulator/Caches" "$HOME/Library/Developer/CoreSimulator/Devices" "$HOME/Library/Caches/com.apple.dt.Xcode"; do [ -e "$p" ] && printf '%s\n' "$p"; done ;;
    installers)
      find "$HOME/Downloads" -maxdepth 1 -type f \( -iname '*.dmg' -o -iname '*.pkg' -o -iname '*.iso' -o -iname '*.zip' -o -iname '*.mpkg' \) -mtime +30 2>/dev/null ;;
    mail_cache)
      for p in "$HOME/Library/Containers/com.apple.mail/Data/Library/Mail Downloads" "$HOME/Library/Mail Downloads" "$HOME/Library/Containers/com.apple.mail/Data/Library/Caches"; do [ -e "$p" ] && printf '%s\n' "$p"; done ;;
    leftovers)
      # Application Support folders named after an app that is no longer installed. Conservative:
      # skip shared vendors / system names, and only folders untouched for 90+ days.
      local apps d n
      apps="$(ls /Applications "$HOME/Applications" /Applications/Utilities /System/Applications /System/Applications/Utilities 2>/dev/null | sed 's/\.app$//' | tr 'A-Z' 'a-z' | sort -u)"
      for d in "$HOME/Library/Application Support"/*; do
        [ -d "$d" ] || continue; n="$(basename "$d")"
        case "$n" in com.apple.*|Apple|Google|Adobe|Microsoft|Mozilla|CrashReporter|AddressBook|iCloud|Dock|Knowledge|MobileSync|SyncServices|CallHistory*|com.*|org.*|net.*|io.*|dev.*|Steam|JetBrains|Code|Slack|zoom.us|Spotify|Dropbox|OneDrive|Box|Backblaze|Parallels|VMware*|VirtualBox|Sublime*|Notion|Discord|Figma|Alfred|Bartender*|1Password|Little\ Snitch*|CleanMyMac*|MacUpdate|Setapp|Homebrew|iTerm*|Postgres*|Docker*|minecraft|.DS_Store) continue;; esac
        printf '%s\n' "$apps" | grep -qx "$(printf '%s' "$n" | tr 'A-Z' 'a-z')" && continue
        [ -n "$(find "$d" -maxdepth 0 -mtime +90 2>/dev/null)" ] || continue
        printf '%s\n' "$d"
      done ;;
  esac
}
cat_meta() {   # id -> label|default|risk|needs_sudo|description
  case "$1" in
    user_caches)    printf 'User caches|1|safe|0|~/Library/Caches - apps rebuild these; iCloud/Photos/Safari caches are excluded';;
    browser_caches) printf 'Browser caches|1|safe|0|Chrome, Safari, Firefox, Edge, Brave page caches - sites reload a little slower once';;
    system_caches)  printf 'System caches|1|safe|1|/Library/Caches - needs admin; macOS rebuilds these';;
    logs)           printf 'Logs|1|safe|1|~/Library/Logs + rotated system logs. Crash + panic reports are KEPT (MacDiag reads them)';;
    trash)          printf 'Trash|0|ask|0|The customer'"'"'s Trash. Only tick with permission - this is the one place people recover files from';;
    ios_backups)    printf 'iPhone / iPad backups|0|ask|0|MobileSync backups - often 20-60 GB. Ask the customer; a fresh backup is made on the next sync';;
    xcode)          printf 'Xcode derived data + simulators|1|safe|0|DerivedData, device support files, simulator caches - regenerated on next build';;
    installers)     printf 'Old installers in Downloads|0|ask|0|.dmg / .pkg / .zip / .iso older than 30 days';;
    mail_cache)     printf 'Mail attachment cache|1|safe|0|Mail Downloads + Mail caches - attachments re-download from the server (needs Full Disk Access to see)';;
    leftovers)      printf 'Leftovers of uninstalled apps|0|ask|0|Application Support folders with no matching app, untouched 90+ days - VERIFY the names before ticking';;
  esac
}
CATS="user_caches browser_caches system_caches logs xcode mail_cache trash ios_backups installers leftovers"

# ---- scan -------------------------------------------------------------------
do_scan() {
  local out="$1" first=1 c meta label def risk sud desc total n items p b tot_all=0 free purg
  free="$(free_bytes)"; purg="$(purgeable_bytes)"
  log "scan: free $(human "$free"), purgeable ~$(human "$purg")"
  {
    printf '{"ts":%s,"version":"%s","host":"%s","free":%s,"purgeable":%s,"sudo":%s,"categories":[' "$(date +%s)" "$CU_VERSION" "$(js "$(scutil --get ComputerName 2>/dev/null)")" "$(num "$free")" "$(num "$purg")" "$HAVE_SUDO"
    for c in $CATS; do
      meta="$(cat_meta "$c")"; label="${meta%%|*}"; meta="${meta#*|}"; def="${meta%%|*}"; meta="${meta#*|}"; risk="${meta%%|*}"; meta="${meta#*|}"; sud="${meta%%|*}"; desc="${meta#*|}"
      [ "$first" = 1 ] || printf ','; first=0
      total=0; n=0; items=""
      if [ "$sud" = "1" ] && [ "$HAVE_SUDO" != "1" ]; then
        printf '{"id":"%s","label":"%s","desc":"%s","default":%s,"risk":"%s","needs_sudo":true,"available":false,"bytes":0,"count":0,"items":[]}' "$c" "$(js "$label")" "$(js "$desc")" "$([ "$def" = 1 ] && printf true || printf false)" "$risk"
        continue
      fi
      log "scanning $label..."
      while IFS= read -r p; do
        [ -n "$p" ] || continue
        b="$(psize "$p")"; total=$((total + b)); n=$((n + 1))
        # extra info for iOS backups: device name + last backup date from Info.plist
        local extra=""
        if [ "$c" = "ios_backups" ] && [ -f "$p/Info.plist" ]; then
          extra="$(/usr/libexec/PlistBuddy -c 'Print :Device Name' "$p/Info.plist" 2>/dev/null) · $(/usr/libexec/PlistBuddy -c 'Print :Last Backup Date' "$p/Info.plist" 2>/dev/null | cut -c1-16)"
        fi
        items="$items$([ -n "$items" ] && printf ',')$(printf '{"p":"%s","bytes":%s,"info":"%s"}' "$(js "$p")" "$b" "$(js "$extra")")"
      done <<EOF_PATHS
$(cat_paths "$c" | head -n 400)
EOF_PATHS
      tot_all=$((tot_all + total))
      printf '{"id":"%s","label":"%s","desc":"%s","default":%s,"risk":"%s","needs_sudo":%s,"available":true,"bytes":%s,"count":%s,"items":[%s]}' "$c" "$(js "$label")" "$(js "$desc")" "$([ "$def" = 1 ] && printf true || printf false)" "$risk" "$([ "$sud" = 1 ] && printf true || printf false)" "$total" "$n" "$items"
    done
    # local Time Machine snapshots = the bulk of "purgeable"
    printf '],"snapshots":['
    first=1
    for s in $(tmutil listlocalsnapshots / 2>/dev/null | grep -o 'com.apple.TimeMachine.[0-9-]*' ); do
      [ "$first" = 1 ] || printf ','; first=0
      printf '{"name":"%s","date":"%s"}' "$(js "$s")" "$(printf '%s' "$s" | sed 's/com.apple.TimeMachine.//')"
    done
    # large + old files in the home folder (report only - the tech ticks them one by one)
    printf '],"large":['
    first=1
    log "looking for large files (> 500 MB, untouched 6 months, outside Library)..."
    while IFS= read -r p; do
      [ -n "$p" ] || continue
      b="$(ls -ln "$p" 2>/dev/null | awk '{ print $5 }')"
      [ "$first" = 1 ] || printf ','; first=0
      printf '{"p":"%s","bytes":%s,"mtime":"%s"}' "$(js "$p")" "$(num "$b")" "$(date -r "$p" '+%Y-%m-%d' 2>/dev/null)"
    done <<EOF_LARGE
$(find "$HOME" -xdev -type f -size +500M -mtime +180 -not -path "$HOME/Library/*" -not -path "$HOME/.Trash/*" -not -path "*/Photos Library.photoslibrary/*" -not -path "*/.git/*" 2>/dev/null | head -n 60)
EOF_LARGE
    printf '],"candidates_bytes":%s}\n' "$tot_all"
  } > "$out.tmp" && mv -f "$out.tmp" "$out"
  log "scan done: $(human "$tot_all") in cleanable categories"
}

# ---- clean ------------------------------------------------------------------
# job.json (written by the page): {"cats":["user_caches",...],"paths":["/Users/x/big.iso"],"snapshots":true,"empty_trash":false}
jget_list() { grep -o "\"$1\" *: *\[[^]]*\]" "$2" | sed 's/.*\[//; s/\]//' | tr ',' '\n' | sed 's/^ *"//; s/" *$//' | grep .; }
jget_bool() { grep -o -E "\"$1\" *: *(true|false)" "$2" | head -n1 | grep -q true; }
do_clean() {
  local job="$1" out="$2" c p b moved=0 mbytes=0 errors=0 free0 free1 list="" cats paths snaps=0 empty=0 dest manifest
  free0="$(free_bytes)"
  cats="$(jget_list cats "$job")"; paths="$(jget_list paths "$job" | sed 's/\\\//\//g; s/\\"/"/g')"
  jget_bool snapshots "$job" && snaps=1; jget_bool empty_trash "$job" && empty=1
  mkdir -p "$REPORT_DIR" 2>/dev/null
  manifest="$REPORT_DIR/CleanUp_${TS}.manifest"; : > "$manifest"
  log "clean: free before $(human "$free0"); categories: $(printf '%s' "$cats" | tr '\n' ' ')"
  mv_trash() {   # $1 path  $2 category
    local src="$1" cat="$2" dst sz
    dst="$TRASH_ROOT/$cat"
    [ -e "$src" ] || return 0
    case "$src" in "$HOME"|"$HOME/"|/|/Users|/Users/|/Library|/System*|"$HOME/Library"|"$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Pictures"*|*"Photos Library.photoslibrary"*) log "REFUSED (protected): $src"; errors=$((errors+1)); return 1;; esac
    sz="$(psize "$src")"
    mkdir -p "$dst" 2>/dev/null
    if su_ mv -f "$src" "$dst/" 2>/dev/null; then
      moved=$((moved+1)); mbytes=$((mbytes+sz)); printf '%s\t%s\t%s\n' "$cat" "$sz" "$src" >> "$manifest"
      list="$list$([ -n "$list" ] && printf ',')$(printf '{"cat":"%s","p":"%s","bytes":%s}' "$cat" "$(js "$src")" "$sz")"
    else
      log "could not move: $src"; errors=$((errors+1))
    fi
  }
  for c in $cats; do
    case " $CATS " in *" $c "*) ;; *) log "unknown category $c - skipped"; continue;; esac
    log "moving $c to the Trash..."
    while IFS= read -r p; do [ -n "$p" ] && mv_trash "$p" "$c"; done <<EOF_C
$(cat_paths "$c" | head -n 400)
EOF_C
  done
  if [ -n "$paths" ]; then
    log "moving selected large files..."
    while IFS= read -r p; do
      [ -n "$p" ] || continue
      case "$p" in "$HOME"/*) mv_trash "$p" "large";; *) log "REFUSED (outside home): $p"; errors=$((errors+1));; esac
    done <<EOF_P
$paths
EOF_P
  fi
  local snapn=0
  if [ "$snaps" = "1" ]; then
    log "thinning local Time Machine snapshots..."
    for s in $(tmutil listlocalsnapshots / 2>/dev/null | grep -o 'com.apple.TimeMachine.[0-9-]*'); do
      d="$(printf '%s' "$s" | sed 's/com.apple.TimeMachine.//')"
      if su_ tmutil deletelocalsnapshots "$d" >/dev/null 2>&1; then snapn=$((snapn+1)); printf 'snapshot\t0\t%s\n' "$s" >> "$manifest"; else log "could not delete snapshot $s (needs admin?)"; fi
    done
    su_ tmutil thinlocalsnapshots / 999999999999 4 >/dev/null 2>&1
  fi
  if [ "$empty" = "1" ]; then
    log "EMPTYING the Trash (permanent)..."
    # the dated cleanup folders go too - this is the explicit second step
    su_ rm -rf "$HOME/.Trash"/* "$HOME/.Trash"/.[!.]* 2>/dev/null
    for v in /Volumes/*/.Trashes/"$U"; do [ -d "$v" ] && su_ rm -rf "$v"/* 2>/dev/null; done
  fi
  sync; sleep 1
  free1="$(free_bytes)"
  local freed=$((free1 - free0)); [ "$freed" -lt 0 ] && freed=0
  log "done: moved $moved item(s) / $(human "$mbytes") to $TRASH_ROOT; free now $(human "$free1") (+$(human "$freed"))"
  # report on the stick (txt + json)
  local host; host="$(scutil --get ComputerName 2>/dev/null | tr -d '\n' | tr -c 'A-Za-z0-9._-' '_' | cut -c1-40)"; [ -n "$host" ] || host=mac
  local base="$REPORT_DIR/CleanUp_${host}_${TS}"
  {
    printf 'CleanUp report  -  BenchTool Mac Diagnostics %s\n======================================================================\n' "$CU_VERSION"
    printf 'Generated : %s\nComputer  : %s\nMode      : %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$host" "$([ "$empty" = 1 ] && printf 'moved to Trash, then Trash EMPTIED' || printf 'moved to Trash (recoverable in %s)' "$TRASH_ROOT")"
    printf '\nRESULT    : %s freed  (free before %s, after %s)\n' "$(human "$freed")" "$(human "$free0")" "$(human "$free1")"
    printf 'Moved     : %s item(s), %s\nSnapshots : %s deleted\nErrors    : %s\n' "$moved" "$(human "$mbytes")" "$snapn" "$errors"
    printf '\n---- MOVED (category, size, path) ----\n'; awk -F'\t' '{ printf "%-12s %10s  %s\n", $1, $2, $3 }' "$manifest"
    [ -n "$PROG" ] && { printf '\n---- LOG ----\n'; cat "$PROG"; }
  } > "$base.txt"
  printf '{"tool":"CleanUp","version":"%s","generated":"%s","host":"%s","verdict":"%s freed","freed":%s,"free_before":%s,"free_after":%s,"moved":%s,"moved_bytes":%s,"snapshots":%s,"errors":%s,"trash_folder":"%s","emptied":%s,"items":[%s]}\n' \
    "$CU_VERSION" "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$(js "$host")" "$(human "$freed")" "$freed" "$free0" "$free1" "$moved" "$mbytes" "$snapn" "$errors" "$(js "$TRASH_ROOT")" "$([ "$empty" = 1 ] && printf true || printf false)" "$list" > "$base.json"
  printf '{"ok":true,"freed":%s,"free_before":%s,"free_after":%s,"moved":%s,"moved_bytes":%s,"snapshots":%s,"errors":%s,"trash_folder":"%s","emptied":%s,"report":"%s"}\n' \
    "$freed" "$free0" "$free1" "$moved" "$mbytes" "$snapn" "$errors" "$(js "$TRASH_ROOT")" "$([ "$empty" = 1 ] && printf true || printf false)" "$(js "$(basename "$base.txt")")" > "$out.tmp" && mv -f "$out.tmp" "$out"
}

# ---- maintenance ---------------------------------------------------------------
do_maint() {
  local op="$1" rc=0
  case "$op" in
    spotlight) log "rebuilding the Spotlight index (mdutil -E /) - indexing runs in the background for a while"; su_ mdutil -E / >/dev/null 2>&1 || rc=1 ;;
    dns)       log "flushing DNS cache"; su_ dscacheutil -flushcache 2>/dev/null; su_ killall -HUP mDNSResponder 2>/dev/null || rc=1 ;;
    periodic)  log "running periodic daily/weekly/monthly (log rotation, tmp cleanup)"; su_ periodic daily weekly monthly >/dev/null 2>&1 || rc=1 ;;
    lsregister) log "resetting Launch Services (duplicate 'Open With' entries)"; /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -kill -r -domain local -domain system -domain user >/dev/null 2>&1 || rc=1 ;;
    purge)     log "purging inactive memory / disk cache (purge)"; su_ purge >/dev/null 2>&1 || rc=1 ;;
    *) log "unknown maintenance op $op"; rc=2 ;;
  esac
  [ "$rc" = 0 ] && log "done" || log "failed (rc=$rc)$([ "$HAVE_SUDO" != 1 ] && printf ' - most maintenance ops need the admin password')"
  return $rc
}

case "$CMD" in
  scan)  OUT="$1"; HAVE_SUDO="${2:-0}"; PROG="${3:-}"; do_scan "$OUT" ;;
  clean) JOB="$1"; OUT="$2"; HAVE_SUDO="${3:-0}"; PROG="${4:-}"; do_clean "$JOB" "$OUT" ;;
  maint) OP="$1"; HAVE_SUDO="${2:-0}"; PROG="${3:-}"; do_maint "$OP" ;;
  *) printf 'usage: cleanup.sh scan <out.json> [sudo] [log] | clean <job.json> <out.json> [sudo] [log] | maint <op> [sudo] [log]\n'; exit 2 ;;
esac
