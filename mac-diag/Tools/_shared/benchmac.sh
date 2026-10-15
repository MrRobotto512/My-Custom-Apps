#!/bin/bash
# benchmac.sh - shared helpers for the BenchTool "Mac Diagnostics" USB apps.
# =============================================================================
# Sourced by every Run-*.command on this stick. Written for the /bin/bash that
# ships on EVERY macOS (3.2) - no bash-4 features (no associative arrays, no
# mapfile, no ${var,,}), no python (not guaranteed on a customer Mac), no
# `timeout` (does not exist on macOS). JSON work goes through osascript's
# JavaScript (JXA) runtime, which every Mac since 10.10 has.
#
#   bm_init "AppName" "1.0"    -> sets APP_DIR/REPORT_DIR/LOG, starts logging
#   info/good/warn/fail/hdr    -> coloured console lines
#   bm_finding SEV CAT "title" "detail"   -> HIGH | MED | INFO (+ counters)
#   bm_gemini "system prompt" user_file   -> AI text (empty if no key / offline)
#   bm_write_reports meta_file ai_file    -> <App>-Reports/<App>_<host>_<time>.txt/.json
#   bm_sudo_start / bm_sudo_stop          -> one password prompt, kept alive
#   bm_timeout SECS cmd...                -> run with a watchdog
#   bm_pause                              -> "press any key" (skipped with -Quiet)
# Key: gemini.key next to the app script, else ../gemini.key, else $GEMINI_API_KEY.

BM_VERSION="1.0"
BM_SHARED_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BM_PROFILE_DIR="$(cd "$BM_SHARED_DIR/.." && pwd)"
BM_API_BASE="https://generativelanguage.googleapis.com/v1beta"
# Same ladder as the Windows Diagnostics apps: newest flash first, fall back
# down if a model is busy/unavailable for this key.
BM_MODELS="gemini-3.5-flash gemini-2.5-flash gemini-2.5-flash-lite gemini-2.0-flash gemini-2.0-flash-lite"
BM_QUIET=0
BM_NOAI=0
BM_US=$'\x1f'          # unit separator between finding fields
BM_HIGH=0; BM_MED=0; BM_INFO=0
BM_SUDO=0
BM_SUDO_PID=""
BM_AI_MODEL=""
BM_REPORT_TXT=""; BM_REPORT_JSON=""

# ---- colours ----------------------------------------------------------------
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_RED=$'\033[31m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
else
  C_RESET=""; C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_BOLD=""; C_DIM=""
fi
info() { printf '%s[*]%s %s\n' "$C_CYAN"   "$C_RESET" "$*"; }
good() { printf '%s[OK]%s %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
fail() { printf '%s[X]%s %s\n' "$C_RED"    "$C_RESET" "$*"; }
hdr()  { printf '\n%s%s== %s ==%s\n' "$C_BOLD" "$C_CYAN" "$*" "$C_RESET"; }
dim()  { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_RESET"; }

# ---- init -------------------------------------------------------------------
# bm_init "MacDiag" "1.0"  (APP_DIR must already be set by the caller)
bm_init() {
  BM_APP="$1"; BM_APP_VERSION="${2:-1.0}"
  [ -n "$APP_DIR" ] || APP_DIR="$(pwd)"
  REPORT_DIR="$APP_DIR/${BM_APP}-Reports"
  LOG="$APP_DIR/${BM_APP}-last-run.log"
  BM_TMP="$(mktemp -d "${TMPDIR:-/tmp}/benchmac.XXXXXX")"
  BM_FINDINGS="$BM_TMP/findings.tsv"
  BM_SECTIONS="$BM_TMP/sections.txt"
  : > "$BM_FINDINGS"; : > "$BM_SECTIONS"
  mkdir -p "$REPORT_DIR" 2>/dev/null
  # Log everything the tech sees (stick may be read-only -> fall back to tmp).
  if ! ( : >> "$LOG" ) 2>/dev/null; then LOG="$BM_TMP/${BM_APP}-last-run.log"; fi
  exec > >(tee -a "$LOG") 2>&1
  BM_START_TS="$(date +%Y%m%d-%H%M%S)"
  BM_HOST="$(bm_host_short)"
  printf '\n%s%s %s  -  BenchTool Mac Diagnostics  (%s)%s\n' "$C_BOLD" "$BM_APP" "$BM_APP_VERSION" "$(date '+%Y-%m-%d %H:%M:%S')" "$C_RESET"
  dim "log: $LOG"
  trap 'bm_cleanup' EXIT
}

bm_cleanup() {
  bm_sudo_stop
  [ -n "$BM_TMP" ] && [ -d "$BM_TMP" ] && rm -rf "$BM_TMP" 2>/dev/null
  return 0
}

# Common switches every app accepts. Call: bm_parse_common "$@" ; then use
# BM_REST (space-joined leftovers) if the app has its own switches.
bm_parse_common() {
  BM_REST=""
  while [ $# -gt 0 ]; do
    case "$1" in
      -Quiet|-quiet|-q) BM_QUIET=1 ;;
      -NoAI|-noai)      BM_NOAI=1 ;;
      *) BM_REST="$BM_REST $1" ;;
    esac
    shift
  done
}

bm_host_short() {
  local n
  n="$(scutil --get ComputerName 2>/dev/null)"
  [ -n "$n" ] || n="$(hostname -s 2>/dev/null)"
  [ -n "$n" ] || n="mac"
  printf '%s' "$n" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-40
}

bm_pause() {
  [ "$BM_QUIET" = "1" ] && return 0
  printf '\nPress any key to close...'
  read -r -s -n 1 _ 2>/dev/null
  printf '\n'
}

# ---- watchdog (macOS has no `timeout`) --------------------------------------
bm_timeout() {
  local secs="$1"; shift
  "$@" &
  local pid=$!
  # The watchdog must NOT inherit our stdout: when bm_timeout sits in a
  # pipeline, an inherited write end keeps the pipe open (and the reader
  # waiting) until the sleep ends. Kill the sleep itself on the way out too.
  ( sleep "$secs"; kill -0 "$pid" 2>/dev/null && kill "$pid" 2>/dev/null ) >/dev/null 2>&1 </dev/null &
  local wd=$!
  wait "$pid" 2>/dev/null
  local rc=$?
  pkill -P "$wd" 2>/dev/null; kill "$wd" 2>/dev/null; wait "$wd" 2>/dev/null
  return $rc
}

# ---- sudo ------------------------------------------------------------------
# One password prompt up front; a background loop keeps the ticket fresh.
# Returns 1 (and the app keeps going with reduced coverage) if refused.
bm_sudo_start() {
  BM_SUDO=0
  if [ "$(id -u)" = "0" ]; then BM_SUDO=1; return 0; fi
  if sudo -n true 2>/dev/null; then
    BM_SUDO=1
  elif [ "$BM_QUIET" = "1" ] || [ ! -t 0 ]; then
    BM_SUDO=0      # background run (dashboard job) without a ticket: never block on a prompt
  else
    printf '%s[?]%s Administrator password lets this tool read hardware + system-level data.\n' "$C_YELLOW" "$C_RESET"
    printf '    (press Return with no password to skip - the sweep still runs, with less coverage)\n'
    if sudo -p "    admin password: " -v 2>/dev/null; then BM_SUDO=1; else BM_SUDO=0; fi
  fi
  if [ "$BM_SUDO" = "1" ]; then
    ( while true; do sudo -n true 2>/dev/null; sleep 50; kill -0 $$ 2>/dev/null || exit; done ) &
    BM_SUDO_PID=$!
    good "admin access granted"
    return 0
  fi
  warn "no admin access - some checks will be skipped"
  return 1
}
bm_sudo_stop() {
  [ -n "$BM_SUDO_PID" ] && kill "$BM_SUDO_PID" 2>/dev/null
  BM_SUDO_PID=""
  return 0
}
# bm_su cmd...  -> runs with sudo when we have it, plain otherwise
bm_su() { if [ "$BM_SUDO" = "1" ]; then sudo -n "$@"; else "$@"; fi; }

# ---- findings ---------------------------------------------------------------
# bm_finding HIGH|MED|INFO "Category" "Title" "Detail (one line, any length)"
bm_finding() {
  local sev="$1" cat="$2" title="$3" detail="$4"
  detail="$(printf '%s' "$detail" | tr '\n\r\t' '   ' | tr -d "$BM_US")"
  title="$(printf '%s' "$title" | tr '\n\r\t' '   ' | tr -d "$BM_US")"
  printf '%s%s%s%s%s%s%s\n' "$sev" "$BM_US" "$cat" "$BM_US" "$title" "$BM_US" "$detail" >> "$BM_FINDINGS"
  case "$sev" in
    HIGH) BM_HIGH=$((BM_HIGH+1)); fail "$cat: $title"; [ -n "$detail" ] && dim "$detail" ;;
    MED)  BM_MED=$((BM_MED+1));   warn "$cat: $title"; [ -n "$detail" ] && dim "$detail" ;;
    *)    BM_INFO=$((BM_INFO+1)); info "$cat: $title"; [ -n "$detail" ] && dim "$detail" ;;
  esac
  return 0
}
# bm_section "Title" file-with-raw-text  -> appended verbatim to the txt report
bm_section() {
  local title="$1" file="$2"
  { printf '\n---- %s ----\n' "$title"; cat "$file" 2>/dev/null; } >> "$BM_SECTIONS"
}
bm_section_text() { local f="$BM_TMP/sec.$$"; printf '%s\n' "$2" > "$f"; bm_section "$1" "$f"; rm -f "$f"; }

# ---- JSON via JXA -----------------------------------------------------------
bm_jxa() { osascript -l JavaScript "$@" 2>/dev/null; }
# stdin -> JSON string literal (with quotes)
bm_json_string() {
  osascript -l JavaScript -e 'ObjC.import("Foundation");
    var d=$.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
    var s=ObjC.unwrap($.NSString.alloc.initWithDataEncoding(d,$.NSUTF8StringEncoding));
    if (s===undefined||s===null) s=""; JSON.stringify(String(s))' 2>/dev/null
}

# ---- Gemini -----------------------------------------------------------------
bm_gemini_key() {
  local k f
  for f in "$APP_DIR/gemini.key" "$BM_PROFILE_DIR/gemini.key"; do
    if [ -f "$f" ]; then
      k="$(head -n1 "$f" 2>/dev/null | tr -d '\r\n[:space:]')"
      [ -n "$k" ] && { printf '%s' "$k"; return 0; }
    fi
  done
  [ -n "$GEMINI_API_KEY" ] && { printf '%s' "$GEMINI_API_KEY"; return 0; }
  return 1
}
bm_online() { curl -s -m 6 -o /dev/null "https://generativelanguage.googleapis.com/" 2>/dev/null; }

# bm_gemini "system prompt text" /path/user_prompt.txt  -> prints model reply
# Returns 1 with nothing printed when AI is off / no key / offline / all busy.
bm_gemini() {
  local system="$1" userfile="$2" key model body resp txt http m attempt
  [ "$BM_NOAI" = "1" ] && { info "AI skipped (-NoAI)"; return 1; }
  key="$(bm_gemini_key)" || { warn "no gemini.key - AI section skipped (set it with Set-GeminiKey.command or BenchTool Settings > Import API Key)"; return 1; }
  bm_online || { warn "no internet - AI section skipped"; return 1; }
  body="$BM_TMP/gemini_body.json"; resp="$BM_TMP/gemini_resp.json"
  printf '%s' "$system" > "$BM_TMP/gemini_sys.txt"
  if ! bm_jxa "$BM_SHARED_DIR/gemini_body.js" "$BM_TMP/gemini_sys.txt" "$userfile" > "$body" || [ ! -s "$body" ]; then
    warn "could not build the AI request (osascript)"; return 1
  fi
  m="${GEMINI_MODEL:-}"
  for model in $m $BM_MODELS; do
    attempt=0
    while [ $attempt -lt 2 ]; do
      attempt=$((attempt+1))
      http="$(curl -s -m 120 -o "$resp" -w '%{http_code}' \
              -H 'Content-Type: application/json' -H "x-goog-api-key: $key" \
              -X POST "$BM_API_BASE/models/$model:generateContent" --data-binary "@$body" 2>/dev/null)"
      case "$http" in
        200)
          txt="$(bm_jxa "$BM_SHARED_DIR/gemini_text.js" "$resp")"
          if [ -n "$txt" ]; then BM_AI_MODEL="$model"; printf '%s\n' "$txt"; return 0; fi
          warn "$model returned an empty answer"; break ;;
        429|503|500|502|504)
          [ $attempt -lt 2 ] && { warn "$model busy (HTTP $http) - retrying..."; sleep 3; continue; }
          warn "$model busy (HTTP $http) - trying the next model"; break ;;
        400|403)
          warn "Gemini rejected the request (HTTP $http): $(head -c 300 "$resp" 2>/dev/null | tr -d '\n')"
          return 1 ;;
        404) break ;;   # model not available for this key -> next
        *)  warn "$model: HTTP ${http:-none}"; break ;;
      esac
    done
  done
  warn "all Gemini models busy/unavailable - AI section skipped"
  return 1
}

# ---- reports ----------------------------------------------------------------
# Findings raised inside `... | while read` pipelines land in the file but not
# in this shell's counters - always recount from the file before judging.
bm_recount() {
  [ -f "$BM_FINDINGS" ] || return 0
  BM_HIGH="$(grep -c "^HIGH$BM_US" "$BM_FINDINGS")"
  BM_MED="$(grep -c "^MED$BM_US" "$BM_FINDINGS")"
  BM_INFO="$(grep -c "^INFO$BM_US" "$BM_FINDINGS")"
  return 0
}
bm_verdict() {
  bm_recount
  if   [ "$BM_HIGH" -gt 0 ]; then printf 'ATTENTION NEEDED'
  elif [ "$BM_MED" -ge 3 ];  then printf 'SEVERAL ISSUES'
  elif [ "$BM_MED" -gt 0 ];  then printf 'MINOR ISSUES'
  else printf 'NO OBVIOUS PROBLEMS'; fi
}

# bm_write_reports meta_file ai_file [verdict-string]
#   meta_file: key=value lines (model, serial, os, ...) -> report header + JSON
bm_write_reports() {
  local meta="$1" ai="$2" verdict="${3:-$(bm_verdict)}"
  bm_recount
  local base="$REPORT_DIR/${BM_APP}_${BM_HOST}_${BM_START_TS}"
  local txt="$base.txt" json="$base.json"
  BM_REPORT_TXT="$txt"; BM_REPORT_JSON="$json"
  {
    printf '%s report  -  BenchTool Mac Diagnostics %s\n' "$BM_APP" "$BM_APP_VERSION"
    printf '%s\n' "======================================================================"
    printf 'Generated : %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
    printf 'Computer  : %s\n' "$BM_HOST"
    [ -f "$meta" ] && awk -F= '{ k=$1; v=substr($0, length(k)+2); printf "%-10s: %s\n", k, v }' "$meta"
    printf '\nVERDICT   : %s   (HIGH %d / MED %d / INFO %d)\n' "$verdict" "$BM_HIGH" "$BM_MED" "$BM_INFO"
    printf '\n---- FINDINGS ----\n'
    if [ -s "$BM_FINDINGS" ]; then
      awk -F"$BM_US" '{ printf "[%-4s] %s: %s\n        %s\n", $1, $2, $3, $4 }' "$BM_FINDINGS"
    else
      printf '(none)\n'
    fi
    if [ -s "$ai" ]; then
      printf '\n---- AI TRIAGE (%s) ----\n' "${BM_AI_MODEL:-gemini}"
      cat "$ai"
      printf '\n'
    fi
    cat "$BM_SECTIONS" 2>/dev/null
  } > "$txt" 2>/dev/null
  printf 'verdict=%s\nhigh=%s\nmed=%s\ninfo=%s\ntool=%s\nversion=%s\nhost=%s\ngenerated=%s\nai_model=%s\n' \
    "$verdict" "$BM_HIGH" "$BM_MED" "$BM_INFO" "$BM_APP" "$BM_APP_VERSION" "$BM_HOST" \
    "$(date '+%Y-%m-%dT%H:%M:%S%z')" "${BM_AI_MODEL:-}" > "$BM_TMP/meta2.txt"
  [ -f "$meta" ] && cat "$meta" >> "$BM_TMP/meta2.txt"
  bm_jxa "$BM_SHARED_DIR/report_json.js" "$BM_TMP/meta2.txt" "$BM_FINDINGS" "$ai" "$json" >/dev/null 2>&1 \
    || warn "JSON twin not written (osascript)"
  good "report: $txt"
  [ -f "$json" ] && dim "json:   $json"
}

bm_open_report() {
  [ "$BM_QUIET" = "1" ] && return 0
  [ -n "$BM_REPORT_TXT" ] && [ -f "$BM_REPORT_TXT" ] || return 0
  printf 'Open the report now? [y/N] '
  read -r -n 1 a 2>/dev/null; printf '\n'
  case "$a" in y|Y) open -t "$BM_REPORT_TXT" 2>/dev/null || open "$BM_REPORT_TXT" ;; esac
}

# ---- small helpers ----------------------------------------------------------
bm_kb_to_human() {   # KB -> "1.2 GB"
  awk -v k="$1" 'BEGIN{ if (k>=1048576) printf "%.1f GB", k/1048576; else if (k>=1024) printf "%.0f MB", k/1024; else printf "%d KB", k }'
}
bm_bytes_to_human() {
  awk -v b="$1" 'BEGIN{ if (b>=1099511627776) printf "%.2f TB", b/1099511627776; else if (b>=1073741824) printf "%.1f GB", b/1073741824; else if (b>=1048576) printf "%.0f MB", b/1048576; else printf "%d KB", b/1024 }'
}
bm_trim() { printf '%s' "$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'; }
bm_lower() { printf '%s' "$1" | tr 'A-Z' 'a-z'; }
bm_is_apple_silicon() { [ "$(uname -m 2>/dev/null)" = "arm64" ] || [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; }
bm_os_version() { sw_vers -productVersion 2>/dev/null; }
bm_os_major() { bm_os_version | cut -d. -f1; }
# version compare: bm_ver_ge 10.15 10.13 -> true
bm_ver_ge() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | head -n1)" = "$2" ]; }
# codesign summary for a path: apple | appstore | devid:<who> | adhoc | signed:<auth> | unsigned | invalid | missing
bm_sign_status() {
  local p="$1" out
  [ -e "$p" ] || { printf 'missing'; return; }
  out="$(codesign -dv --verify=4 "$p" 2>&1)"
  case "$out" in
    *"not signed at all"*) printf 'unsigned' ;;
    *"invalid signature"*|*"modified"*|*"does not seal"*|*"resource envelope"*|*"invalid Info.plist"*) printf 'invalid' ;;
    *) if printf '%s' "$out" | grep -q 'Authority=Apple Root CA' && ! printf '%s' "$out" | grep -q 'Developer ID'; then
         if printf '%s' "$out" | grep -q 'Authority=Software Signing'; then printf 'apple'; else printf 'appstore'; fi
       elif printf '%s' "$out" | grep -q 'Developer ID Application'; then
         printf 'devid:%s' "$(printf '%s' "$out" | grep -o 'Developer ID Application: [^)]*)' | head -n1 | sed 's/Developer ID Application: //')"
       elif printf '%s' "$out" | grep -q 'Signature=adhoc'; then printf 'adhoc'
       elif printf '%s' "$out" | grep -q 'Authority='; then printf 'signed:%s' "$(printf '%s' "$out" | grep 'Authority=' | head -n1 | cut -c11-70)"
       else printf 'unsigned'; fi ;;
  esac
}
# Is a path somewhere malware/adware likes to hide (user-writable, temp, hidden)?
bm_path_is_suspicious() {
  case "$1" in
    /tmp/*|/private/tmp/*|/var/folders/*|/private/var/folders/*|/private/var/tmp/*|/var/tmp/*|/Users/Shared/*) return 0 ;;
    */.[!.]*/*) return 0 ;;     # any hidden folder in the path
    /Users/*/Library/*) return 0 ;;
    /Library/Application\ Support/*) return 2 ;;   # common for adware AND legit vendors - caller decides
    *) return 1 ;;
  esac
}
# PlistBuddy read helper: bm_plist file key  (empty on missing)
bm_plist() { /usr/libexec/PlistBuddy -c "Print :$2" "$1" 2>/dev/null; }
# Program path a launchd plist runs (Program, else ProgramArguments[0])
bm_launchd_program() {
  local f="$1" p
  p="$(bm_plist "$f" Program)"
  [ -n "$p" ] || p="$(bm_plist "$f" ProgramArguments:0)"
  printf '%s' "$p"
}
bm_launchd_args() { /usr/libexec/PlistBuddy -c "Print :ProgramArguments" "$1" 2>/dev/null | sed -e '1d' -e '$d' | sed 's/^[[:space:]]*//' | tr '\n' ' '; }
# Full Disk Access probe for Terminal: 0 = yes
bm_has_fda() { ls "$HOME/Library/Safari" >/dev/null 2>&1 || ls "$HOME/Library/Mail" >/dev/null 2>&1; }

# ---- bundled smartctl (smartmontools 7.4, GPL-2, Tools/_bin/) -----------------
# diskutil only says Verified/Failing; the numbers that actually predict an SSD
# death (Percentage Used, Available Spare, Media errors) need smartctl. The
# stick is exFAT (no exec bit), so the universal binary is copied into a tmp
# dir, made executable, cleared of xattrs and - should macOS still refuse it -
# ad-hoc signed. Prints the runnable path; returns 1 when unavailable.
#   bm_smartctl [dest-dir]     (default $BM_TMP/bin)
BM_SMARTCTL=""
bm_smartctl() {
  if [ -n "$BM_SMARTCTL" ] && [ -x "$BM_SMARTCTL" ]; then printf '%s' "$BM_SMARTCTL"; return 0; fi
  local src="$BM_PROFILE_DIR/_bin/smartctl" dir="${1:-$BM_TMP/bin}" dst
  [ -f "$src" ] || return 1
  [ -n "$dir" ] || return 1
  dst="$dir/smartctl"
  mkdir -p "$dir" 2>/dev/null || return 1
  if [ ! -x "$dst" ] || [ "$src" -nt "$dst" ]; then
    cp -f "$src" "$dst" 2>/dev/null || return 1
    chmod 755 "$dst" 2>/dev/null
    xattr -c "$dst" 2>/dev/null
  fi
  if ! "$dst" --version >/dev/null 2>&1; then
    codesign -s - -f "$dst" >/dev/null 2>&1
    "$dst" --version >/dev/null 2>&1 || return 1
  fi
  BM_SMARTCTL="$dst"; printf '%s' "$dst"; return 0
}
# bm_smart_kv <smartctl -a output file>  -> key=value lines (NVMe + ATA/SATA)
#   type=nvme|ata|none  health=PASSED|FAILED|?  model= serial= fw= temp= hours=
#   cycles= wear=<% used> spare= spare_thr= media_errors= unsafe= crit=<hex>
#   tbw=<TB written> realloc= pending= uncorrect= crc= failed=<attr names>
bm_smart_kv() {
  awk '
    function num(s) { gsub(/[^0-9.]/, "", s); return s + 0 }
    function val(line) { sub(/^[^:]*:[ \t]*/, "", line); return line }
    BEGIN { type = "none"; health = "?"; failed = "" }
    /NVMe Log 0x02/ || /^Percentage Used:/ { type = "nvme" }
    /^ID# ATTRIBUTE_NAME/ { type = "ata"; inattr = 1; next }
    /^Model Number:/ || /^Device Model:/ { model = val($0) }
    /^Serial Number:/ { serial = val($0) }
    /^Firmware Version:/ { fw = val($0) }
    /^SMART overall-health self-assessment test result:/ { health = val($0) }
    /^SMART Health Status:/ { health = val($0) }
    /^Critical Warning:/ { crit = val($0) }
    /^Temperature:/ { temp = num(val($0)) }
    /^Available Spare:/ { spare = num(val($0)) }
    /^Available Spare Threshold:/ { spare_thr = num(val($0)) }
    /^Percentage Used:/ { wear = num(val($0)) }
    /^Data Units Written:/ { v = val($0); gsub(/,/, "", v); split(v, a, " "); tbw = a[1] * 512000 / 1e12 }
    /^Power Cycles:/ { v = val($0); gsub(/,/, "", v); cycles = num(v) }
    /^Power On Hours:/ { v = val($0); gsub(/,/, "", v); hours = num(v) }
    /^Unsafe Shutdowns:/ { v = val($0); gsub(/,/, "", v); unsafe = num(v) }
    /^Media and Data Integrity Errors:/ { v = val($0); gsub(/,/, "", v); media = num(v) }
    /^Error Information Log Entries:/ { v = val($0); gsub(/,/, "", v); errlog = num(v) }
    /Smartctl open device/ || /Permission denied/ || /Unable to detect device type/ { err = $0 }
    inattr && /^[ ]*[0-9]+ / {
      id = $1 + 0; name = $2; value = $4 + 0; raw = $10 + 0; when = $9
      if (when != "-" && when != "") failed = failed (failed == "" ? "" : ",") name
      if (id == 5) realloc = raw; else if (id == 9) hours = raw; else if (id == 12) cycles = raw
      else if (id == 194) temp = raw; else if (id == 197) pending = raw; else if (id == 198) uncorrect = raw
      else if (id == 199) crc = raw
      else if (id == 241 && raw > 0) tbw = raw * 512 / 1e12
      else if (id == 231 || id == 233 || id == 177 || id == 173) { if (life == "" || id == 231 || id == 233) life = value }
      else if (id == 174 || id == 192) unsafe = raw
    }
    inattr && /^[ \t]*$/ { inattr = 0 }
    END {
      if (type == "ata" && life != "" && wear == "") { wear = 100 - life; if (wear < 0) wear = 0 }
      printf "type=%s\nhealth=%s\nmodel=%s\nserial=%s\nfw=%s\ntemp=%s\nhours=%s\ncycles=%s\nwear=%s\nspare=%s\nspare_thr=%s\nmedia_errors=%s\nunsafe=%s\ncrit=%s\ntbw=%s\nrealloc=%s\npending=%s\nuncorrect=%s\ncrc=%s\nerrlog=%s\nfailed=%s\nerror=%s\n",
        type, health, model, serial, fw, temp, hours, cycles, wear, spare, spare_thr, media, unsafe, crit, (tbw == "" ? "" : sprintf("%.2f", tbw)), realloc, pending, uncorrect, crc, errlog, failed, err
    }' "$1" 2>/dev/null
}
