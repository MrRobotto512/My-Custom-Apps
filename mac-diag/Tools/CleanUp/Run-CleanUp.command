#!/bin/bash
#  CleanUp  -  junk + purgeable-space cleaner  (BenchTool Mac Diagnostics)
#  ============================================================================
#  Terminal front end for cleanup.sh - the same engine the dashboard's Clean
#  page uses. Scans first (sizes per category), you pick categories by number,
#  everything moves into ~/.Trash/BenchTool-Cleanup-<time>/ (recoverable), and a
#  report lands in CleanUp-Reports/. Emptying the Trash is a separate answer.
#
#  Run:  double-click Run-CleanUp.command   (asks for the admin password once -
#        system caches, logs and Time Machine snapshots need it)
#  Switches:  -ScanOnly   -NoSudo   -Quiet
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR" || exit 1
. "$APP_DIR/../_shared/benchmac.sh" || { echo "missing _shared/benchmac.sh"; exit 1; }
SCANONLY=0; NOSUDO=0
bm_parse_common "$@"
set -- $BM_REST
while [ $# -gt 0 ]; do case "$1" in -ScanOnly|-scanonly) SCANONLY=1;; -NoSudo|-nosudo) NOSUDO=1;; esac; shift; done
bm_init "CleanUp" "1.0"
T="$BM_TMP"
[ "$NOSUDO" = "1" ] || bm_sudo_start
hdr "1. Scan"
bash "$APP_DIR/cleanup.sh" scan "$T/scan.json" "$BM_SUDO" "$T/progress.log" 2>&1 | sed 's/^/  /'
# human table from the JSON (osascript JXA keeps this bash-3.2 friendly)
bm_jxa -e 'ObjC.import("Foundation"); function run(a){ const j=JSON.parse(ObjC.unwrap($.NSString.stringWithContentsOfFileEncodingError($(a[0]),4,null))); const h=b=>b>=1073741824?(b/1073741824).toFixed(2)+" GB":b>=1048576?Math.round(b/1048576)+" MB":Math.round(b/1024)+" KB"; let o=""; j.categories.forEach((c,i)=>{ o+=(" "+(i+1)).slice(-2)+"  "+(c.available?h(c.bytes).padStart(10):"needs admin")+"  "+(c.default?"[x]":"[ ]")+" "+c.label+(c.risk==="ask"?"  (ask the customer)":"")+"\n"; }); o+="\nsnapshots: "+j.snapshots.length+"   large old files: "+j.large.length+"   purgeable ~"+h(j.purgeable)+"   free "+h(j.free)+"\n"; return o; }' "$T/scan.json" 2>/dev/null > "$T/table.txt"
[ -s "$T/table.txt" ] && cat "$T/table.txt" || { warn "could not render the table - raw scan:"; head -c 2000 "$T/scan.json"; echo; }
bm_section "Scan" "$T/table.txt"
if [ "$SCANONLY" = "1" ] || [ "$BM_QUIET" = "1" ]; then bm_write_reports /dev/null /dev/null "SCAN ONLY"; bm_pause; exit 0; fi
hdr "2. Choose"
printf 'Category numbers to clean (space separated), "d" for the [x] defaults, Return to cancel: '
read -r pick
[ -n "$pick" ] || { info "cancelled - nothing changed"; bm_pause; exit 0; }
IDS="user_caches browser_caches system_caches logs xcode mail_cache trash ios_backups installers leftovers"
CATS=""
if [ "$pick" = "d" ]; then CATS="user_caches browser_caches system_caches logs xcode mail_cache"; else
  for n in $pick; do i=0; for id in $IDS; do i=$((i+1)); [ "$i" = "$n" ] && CATS="$CATS $id"; done; done; fi
printf 'Also delete local Time Machine snapshots (purgeable space)? [Y/n] '; read -r a; SNAP=true; case "$a" in n|N) SNAP=false;; esac
printf 'Empty the Trash afterwards? PERMANENT - the moved files cannot be recovered. [y/N] '; read -r a; EMPTY=false; case "$a" in y|Y) EMPTY=true;; esac
printf '{"cats":[%s],"paths":[],"snapshots":%s,"empty_trash":%s}\n' "$(for c in $CATS; do printf '"%s",' "$c"; done | sed 's/,$//')" "$SNAP" "$EMPTY" > "$T/job.json"
hdr "3. Clean"
bash "$APP_DIR/cleanup.sh" clean "$T/job.json" "$T/result.json" "$BM_SUDO" "$T/progress.log" 2>&1 | sed 's/^/  /'
good "report written to $APP_DIR/CleanUp-Reports/ (cleanup.sh wrote it directly)"
bm_pause
exit 0
