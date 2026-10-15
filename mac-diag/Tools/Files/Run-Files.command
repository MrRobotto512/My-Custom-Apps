#!/bin/bash
#  Files  -  disk map / find / back up / remove  (BenchTool Mac Diagnostics)
#  ============================================================================
#  Terminal front end for files.sh - the engine behind the dashboard's FILES
#  page. This version indexes the startup disk and prints a "what fills it"
#  report: the biggest top-level folders, the customer-data folders (Desktop,
#  Documents, Photos, Mail, iPhone backups...) and the 40 largest files.
#  Browsing, selecting, backing up and removing are done on the dashboard's
#  Files page (Run-Dashboard.command -> Files), which uses the same engine.
#
#  Run:  double-click Run-Files.command   (asks for the admin password once so
#        other users' folders and system-owned files are counted too)
#  Switches:  -NoSudo   -Quiet
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR" || exit 1
. "$APP_DIR/../_shared/benchmac.sh" || { echo "missing _shared/benchmac.sh"; exit 1; }
NOSUDO=0
bm_parse_common "$@"
set -- $BM_REST
while [ $# -gt 0 ]; do case "$1" in -NoSudo|-nosudo) NOSUDO=1;; esac; shift; done
bm_init "Files" "1.0"
T="$BM_TMP"
[ "$NOSUDO" = "1" ] || bm_sudo_start

hdr "1. Index the startup disk"
info "one pass with du + find - 1 to 5 minutes depending on the disk"
mkdir -p "$T/idx"
bash "$APP_DIR/files.sh" index "$T/idx" "$BM_SUDO" "$T/progress.log" 2>&1 | sed 's/^/  /'
[ -s "$T/idx/meta.txt" ] || { fail "index failed - see above"; bm_write_reports /dev/null /dev/null "INDEX FAILED"; bm_pause; exit 1; }
kv() { awk -F= -v k="$1" '$1==k { print substr($0, length(k)+2); exit }' "$T/idx/meta.txt"; }
HOMEDIR="$(kv home)"; TOTAL="$(kv total)"; USED="$(kv used)"
good "$(kv dirs) folders, $(kv files) files >= 1 MB  -  $(bm_bytes_to_human "$USED") of $(bm_bytes_to_human "$TOTAL") used"
printf 'volume=%s\nused=%s\ntotal=%s\nfolders=%s\nfiles_1mb=%s\nadmin=%s\n' "$(kv volume)" "$(bm_bytes_to_human "$USED")" "$(bm_bytes_to_human "$TOTAL")" "$(kv dirs)" "$(kv files)" "$([ "$BM_SUDO" = 1 ] && printf yes || printf no)" > "$T/meta.txt"

# top-level folders by size (kB<TAB>path -> human, path)
top_under() {   # $1 parent  $2 count
  local pre="$1/"; [ "$1" = "/" ] && pre="/"
  awk -F'\t' -v pre="$pre" 'index($2, pre)==1 { r=substr($2, length(pre)+1); if (r != "" && index(r, "/")==0) printf "%s\t%s\n", $1, $2 }' "$T/idx/dirs.tsv" \
    | sort -t"$(printf '\t')" -k1,1rn | head -n "$2" | awk -F'\t' '{ b=$1*1024; h=(b>=1073741824)?sprintf("%.2f GB",b/1073741824):(b>=1048576)?sprintf("%.0f MB",b/1048576):sprintf("%.0f KB",b/1024); printf "  %10s  %s\n", h, $2 }'
}
hdr "2. What fills the disk"
printf '  top-level:\n'; top_under / 12 | tee "$T/top.txt"
printf '\n  in %s:\n' "$HOMEDIR"; top_under "$HOMEDIR" 12 | tee -a "$T/top.txt"
printf '\n  in %s/Library:\n' "$HOMEDIR"; top_under "$HOMEDIR/Library" 8 | tee -a "$T/top.txt"
bm_section "Largest folders" "$T/top.txt"

hdr "3. Customer data (what a backup / migration is made of)"
: > "$T/map.txt"
for rel in Desktop Documents Downloads Pictures "Pictures/Photos Library.photoslibrary" Movies Music Library/Mail "Library/Application Support/MobileSync/Backup" Library/Messages "Library/Mobile Documents"; do
  k="$(awk -F'\t' -v p="$HOMEDIR/$rel" '$2==p { print $1; exit }' "$T/idx/dirs.tsv")"
  [ -n "$k" ] || continue
  printf '  %10s  ~/%s\n' "$(bm_kb_to_human "$k")" "$rel" | tee -a "$T/map.txt"
  case "$rel" in
    Library/Application*) [ "$k" -gt 10485760 ] && bm_finding INFO "Space" "iPhone / iPad backups take $(bm_kb_to_human "$k")" "Ask the customer whether they still need them (Finder > device > Manage Backups) - the Clean page can remove them" ;;
    Pictures/Photos*)     [ "$k" -gt 104857600 ] && bm_finding INFO "Space" "Photos library is $(bm_kb_to_human "$k")" "Back it up as ONE folder (the .photoslibrary bundle) - never copy files out of it" ;;
    Downloads)            [ "$k" -gt 20971520 ] && bm_finding INFO "Space" "Downloads holds $(bm_kb_to_human "$k")" "Usually installers and duplicates - review with the customer" ;;
  esac
done
[ -s "$T/map.txt" ] || printf '  (no user folders found in the index)\n' | tee "$T/map.txt"
bm_section "Customer data" "$T/map.txt"

hdr "4. Largest files"
sort -t"$(printf '\t')" -k1,1rn "$T/idx/files.tsv" | head -n 40 | awk -F'\t' '{ b=$1; h=(b>=1073741824)?sprintf("%.2f GB",b/1073741824):sprintf("%.0f MB",b/1048576); printf "  %10s  %s\n", h, $3 }' | tee "$T/large.txt"
bm_section "Largest 40 files" "$T/large.txt"
PCT=$(( TOTAL > 0 ? USED * 100 / TOTAL : 0 ))
[ "$PCT" -ge 90 ] && bm_finding MED "Space" "Startup disk is ${PCT}% full" "macOS gets slow and unstable above ~90% - free space before judging performance (Clean page, Files page)"
[ "$PCT" -ge 95 ] && bm_finding HIGH "Space" "Startup disk is ${PCT}% full - critically low" "Updates, Photos and Mail will start failing; free space is the first repair"

bm_write_reports "$T/meta.txt" /dev/null
good "report written to $BM_REPORT_TXT"
info "to browse, back up or remove: Run-Dashboard.command -> Files page"
bm_open_report 2>/dev/null
bm_pause
exit 0
