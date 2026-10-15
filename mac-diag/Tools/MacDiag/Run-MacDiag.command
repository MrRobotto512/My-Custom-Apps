#!/bin/bash
#  MacDiag  -  one-pass Mac health check for the bench  (BenchTool Mac Diagnostics)
#  ============================================================================
#  Answers "what is wrong with this Mac?" in a few minutes, from this USB stick,
#  the way a tech would work through it: hardware ID, memory pressure, storage
#  + SMART, battery health, thermals / throttling, crashes + kernel panics +
#  unexpected shutdowns, startup items, extensions, security posture, network,
#  backups, and the trade-in blockers (Activation Lock, MDM / DEP enrolment,
#  Find My). Every check is READ-ONLY - nothing on the Mac is changed.
#
#  Findings are scored HIGH / MED / INFO, Gemini (as an Apple-certified Mac
#  tech) turns them into a verdict + next bench tests + a customer note, and a
#  report is written to this stick:  MacDiag-Reports/MacDiag_<Mac>_<time>.txt
#  (+ a .json twin BenchTool can read).
#
#  Run:  double-click Run-MacDiag.command  (Terminal opens; it asks for the
#        admin password once - press Return to skip, coverage drops a little).
#  Switches:  -NoAI   skip Gemini      -NoSudo  never ask for the password
#             -Days N crash look-back (default 30)     -Quiet  no prompts
#             -SelfTest  also run a SMART short self-test on internal drives (~2 min)
#
#  Storage health uses the bundled smartctl (smartmontools 7.4, GPL-2, in
#  Tools/_bin) for the real NVMe / ATA counters - Percentage Used, spare
#  blocks, media errors, TB written - not just diskutil's Verified/Failing.
#  Kernel panics are parsed and bucketed (SEP / watchdog / page fault / GPU /
#  SMC / third-party kext) so the report says WHICH kind of panic it is.
#
#  Inspired by watashiwatasha/macos-diagnostic-tool (MIT) - rewritten in plain
#  bash so it runs on any Mac without Python.
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR" || exit 1
. "$APP_DIR/../_shared/benchmac.sh" || { echo "missing _shared/benchmac.sh"; exit 1; }

DAYS=30; NOSUDO=0; SELFTEST=0
bm_parse_common "$@"
set -- $BM_REST
while [ $# -gt 0 ]; do
  case "$1" in
    -NoSudo|-nosudo) NOSUDO=1 ;;
    -Days|-days) shift; DAYS="${1:-30}" ;;
    -SelfTest|-selftest) SELFTEST=1 ;;
  esac
  shift
done

bm_init "MacDiag" "1.1"
META="$BM_TMP/meta.txt"; : > "$META"
meta() { printf '%s=%s\n' "$1" "$2" >> "$META"; }
T="$BM_TMP"

[ "$NOSUDO" = "1" ] || bm_sudo_start
AS=0; bm_is_apple_silicon && AS=1
OSV="$(bm_os_version)"; OSMAJ="$(bm_os_major)"

# =============================================================================
hdr "1. Identity"
# =============================================================================
system_profiler SPHardwareDataType 2>/dev/null > "$T/hw.txt"
getf() { grep -m1 "$1" "$T/hw.txt" | sed 's/^[^:]*: *//'; }
MODEL_NAME="$(getf 'Model Name')"; MODEL_ID="$(getf 'Model Identifier')"
CHIP="$(getf 'Chip')"; [ -n "$CHIP" ] || CHIP="$(getf 'Processor Name')"
SERIAL="$(getf 'Serial Number')"; MEMORY="$(getf '^ *Memory')"
BOARD="$(ioreg -l 2>/dev/null | awk -F'"' '/board-id/ { print $4; exit }')"
ACTLOCK="$(getf 'Activation Lock Status')"
CORES="$(sysctl -n hw.ncpu 2>/dev/null)"
UPTIME="$(uptime 2>/dev/null | sed 's/.*up \(.*\), [0-9]* user.*/\1/')"
BOOTDATE="$(sysctl -n kern.boottime 2>/dev/null | sed 's/.*sec = \([0-9]*\).*/\1/')"
[ -n "$BOOTDATE" ] && BOOTDATE="$(date -r "$BOOTDATE" '+%Y-%m-%d %H:%M' 2>/dev/null)"
info "$MODEL_NAME ($MODEL_ID)  $CHIP  $MEMORY  cores:$CORES"
info "macOS $OSV ($(sw_vers -buildVersion 2>/dev/null))  serial: $SERIAL  board-id: ${BOARD:-n/a}"
info "up $UPTIME (booted $BOOTDATE)"
meta model "$MODEL_NAME ($MODEL_ID)"; meta chip "$CHIP"; meta memory "$MEMORY"; meta serial "$SERIAL"
meta os "macOS $OSV ($(sw_vers -buildVersion 2>/dev/null))"; meta board_id "$BOARD"; meta arch "$(uname -m)"
meta uptime "$UPTIME"
bm_section "Hardware overview" "$T/hw.txt"
# uptime as a finding - "have you tried turning it off and on" is real
UPDAYS="$(uptime 2>/dev/null | grep -o 'up [0-9]* day' | grep -o '[0-9]*')"
[ -n "$UPDAYS" ] && [ "$UPDAYS" -ge 14 ] && bm_finding INFO "System" "Up for $UPDAYS days without a restart" "Long uptimes leak memory in WindowServer/kernel_task - restart before judging performance"

# =============================================================================
hdr "2. Trade-in blockers (Activation Lock / MDM / Find My / iCloud)"
# =============================================================================
if [ -n "$ACTLOCK" ]; then
  case "$(bm_lower "$ACTLOCK")" in
    *enabled*) bm_finding HIGH "Trade-in" "Activation Lock is ENABLED" "The owner must sign out of iCloud / turn off Find My before this Mac can be wiped and resold" ;;
    *) good "Activation Lock: $ACTLOCK" ;;
  esac
  meta activation_lock "$ACTLOCK"
else
  # Intel without T2: Find My token in NVRAM is the tell
  if nvram -p 2>/dev/null | grep -q 'fmm-mobileme-token-FMM'; then
    bm_finding HIGH "Trade-in" "Find My Mac is ON (NVRAM fmm token present)" "Sign out of iCloud before wiping - otherwise the next owner is locked out"
  else
    good "no Find My token in NVRAM"
  fi
fi
ENR="$(bm_su profiles status -type enrollment 2>/dev/null)"
if [ -n "$ENR" ]; then
  printf '%s\n' "$ENR" | sed 's/^/    /'
  if printf '%s' "$ENR" | grep -qi 'Enrolled via DEP: Yes'; then
    bm_finding HIGH "Trade-in" "Enrolled in Apple Business/School Manager (DEP)" "This Mac belongs to an organisation - it will re-enrol in MDM after every wipe until released from ABM/ASM. Do not buy/resell without the release"
  elif printf '%s' "$ENR" | grep -qi 'MDM enrollment: Yes'; then
    bm_finding MED "Trade-in" "MDM profile installed" "Managed by an organisation (not DEP-locked). Remove the management profile before handing back / reselling"
  else
    good "not MDM/DEP enrolled"
  fi
  meta enrollment "$(printf '%s' "$ENR" | tr '\n' ';')"
fi
PROFN="$(bm_su profiles list 2>/dev/null | grep -c 'profileIdentifier')"
[ "${PROFN:-0}" -gt 0 ] && bm_finding INFO "Config" "$PROFN configuration profile(s) installed" "$(bm_su profiles list 2>/dev/null | grep 'profileIdentifier' | sed 's/.*profileIdentifier: //' | tr '\n' ' ' | cut -c1-300)"
ICLOUD="$(defaults read MobileMeAccounts Accounts 2>/dev/null | grep -m1 'AccountID' | sed 's/.*= "\{0,1\}\([^";]*\).*/\1/')"
if [ -n "$ICLOUD" ]; then
  bm_finding INFO "Trade-in" "iCloud signed in: $ICLOUD" "Sign out (System Settings > Apple ID) before erase / trade-in"
  meta icloud "$ICLOUD"
else
  good "no iCloud account signed in for this user"
fi
FV="$(fdesetup status 2>/dev/null)"; meta filevault "$FV"
case "$FV" in *"is On"*) info "FileVault ON - data unreadable without the login password / recovery key";; *) info "FileVault: ${FV:-unknown}";; esac

# =============================================================================
hdr "3. Memory"
# =============================================================================
MP="$(memory_pressure 2>/dev/null)"
FREEPCT="$(printf '%s' "$MP" | grep -o 'free percentage: [0-9]*' | grep -o '[0-9]*')"
SWAP="$(sysctl -n vm.swapusage 2>/dev/null)"
SWAPUSED="$(printf '%s' "$SWAP" | sed 's/.*used = \([0-9.]*\)M.*/\1/')"
PHYS_GB="$(sysctl -n hw.memsize 2>/dev/null | awk '{ printf "%d", $1/1073741824 }')"
info "free: ${FREEPCT:-?}%   swap: $SWAP"
ps -Ao rss=,comm= -r 2>/dev/null | head -n 12 | awk '{ printf "  %6.0f MB  %s\n", $1/1024, substr($0, index($0,$2)) }' > "$T/topmem.txt"
bm_section "Top memory users" "$T/topmem.txt"
if [ -n "$FREEPCT" ] && [ "$FREEPCT" -lt 10 ]; then
  bm_finding HIGH "Memory" "Memory pressure critical (${FREEPCT}% free, ${PHYS_GB} GB installed)" "Top: $(head -n3 "$T/topmem.txt" | tr -s ' ' | tr '\n' ';')"
elif [ -n "$FREEPCT" ] && [ "$FREEPCT" -lt 25 ]; then
  bm_finding MED "Memory" "Memory pressure high (${FREEPCT}% free, ${PHYS_GB} GB installed)" "Top: $(head -n3 "$T/topmem.txt" | tr -s ' ' | tr '\n' ';')"
fi
if [ -n "$SWAPUSED" ] && awk -v s="$SWAPUSED" 'BEGIN{ exit !(s+0 > 4096) }'; then
  bm_finding MED "Memory" "Heavy swap use (${SWAPUSED} MB)" "Mac is paging to disk - more RAM (if upgradeable) or fewer apps; an aging SSD makes this feel far worse"
elif [ -n "$SWAPUSED" ] && awk -v s="$SWAPUSED" 'BEGIN{ exit !(s+0 > 1024) }'; then
  bm_finding INFO "Memory" "Swap in use (${SWAPUSED} MB)" "normal under load; note if the customer reports slowness"
fi
[ -n "$PHYS_GB" ] && [ "$PHYS_GB" -le 4 ] && [ "$OSMAJ" -ge 11 ] && bm_finding MED "Memory" "Only ${PHYS_GB} GB RAM on macOS $OSV" "Expect constant swapping; set expectations with the customer"
meta ram_gb "$PHYS_GB"

# =============================================================================
hdr "4. CPU load + thermals"
# =============================================================================
LOAD="$(sysctl -n vm.loadavg 2>/dev/null | tr -d '{}')"
info "load average:$LOAD  (cores: $CORES)"
ps -Ao %cpu=,comm= -r 2>/dev/null | head -n 10 | awk '{ printf "  %5.1f%%  %s\n", $1, substr($0, index($0,$2)) }' > "$T/topcpu.txt"
bm_section "Top CPU users" "$T/topcpu.txt"
KT="$(ps -Ao %cpu=,comm= 2>/dev/null | awk '$2 ~ /kernel_task$/ { print int($1) }' | head -n1)"
[ -n "$KT" ] && [ "$KT" -ge 60 ] && bm_finding HIGH "Thermal" "kernel_task using ${KT}% CPU" "Classic thermal throttling / stuck sensor / bad charger port (kernel_task hogs the CPU to cool the chip). Check fans, heatsink paste, dust, and which side the charger is on"
MDS="$(ps -Ao %cpu=,comm= 2>/dev/null | awk '$2 ~ /(mds_stores|mdworker|mds)$/ { s+=$1 } END { print int(s) }')"
[ -n "$MDS" ] && [ "$MDS" -ge 50 ] && bm_finding INFO "CPU" "Spotlight indexing busy (${MDS}% CPU)" "normal after an OS update/migration; let it finish before judging speed"
if [ "$AS" = "0" ]; then
  THERM="$(pmset -g therm 2>/dev/null)"
  SPEEDLIM="$(printf '%s' "$THERM" | grep -o 'CPU_Speed_Limit *= *[0-9]*' | grep -o '[0-9]*$')"
  SCHEDLIM="$(printf '%s' "$THERM" | grep -o 'CPU_Scheduler_Limit *= *[0-9]*' | grep -o '[0-9]*$')"
  [ -n "$SPEEDLIM" ] && info "CPU speed limit: ${SPEEDLIM}%  scheduler limit: ${SCHEDLIM:-?}%"
  if [ -n "$SPEEDLIM" ] && [ "$SPEEDLIM" -lt 100 ]; then
    if [ "$SPEEDLIM" -lt 70 ]; then
      bm_finding HIGH "Thermal" "CPU is being throttled to ${SPEEDLIM}%" "Sustained throttling = cooling problem (fan, dust, dried paste) or SMC/sensor fault"
    else
      bm_finding MED "Thermal" "CPU speed limited to ${SPEEDLIM}%" "mild throttling right now - watch it under load"
    fi
  fi
  TL="$(sysctl -n machdep.xcpm.cpu_thermal_level 2>/dev/null)"
  [ -n "$TL" ] && [ "$TL" -gt 0 ] && bm_finding MED "Thermal" "CPU thermal level $TL (0 = cool)" "chip reports thermal pressure"
fi
if [ "$BM_SUDO" = "1" ]; then
  info "sampling sensors (powermetrics, ~3 s)..."
  if [ "$AS" = "1" ]; then
    bm_timeout 20 sudo -n powermetrics --samplers thermal -n 1 -i 1000 2>/dev/null > "$T/pm.txt"
    TP="$(grep -m1 -i 'pressure level' "$T/pm.txt" | sed 's/.*: *//')"
    [ -n "$TP" ] && info "thermal pressure: $TP"
    case "$(bm_lower "$TP")" in
      *heavy*|*trapping*|*sleeping*) bm_finding HIGH "Thermal" "Thermal pressure: $TP" "Mac is heavily throttling - cooling problem or bad sensor" ;;
      *moderate*) bm_finding MED "Thermal" "Thermal pressure: $TP" "throttling under the current load" ;;
    esac
  else
    bm_timeout 25 sudo -n powermetrics --samplers smc -n 1 -i 1000 2>/dev/null > "$T/pm.txt"
    DIE="$(grep -m1 'CPU die temperature' "$T/pm.txt" | grep -o '[0-9.]* C' | head -n1)"
    FANS="$(grep -i 'Fan' "$T/pm.txt" | grep -o '[0-9.]* rpm' | tr '\n' ' ')"
    [ -n "$DIE" ] && info "CPU die: $DIE   fans: ${FANS:-n/a}"
    DIEN="${DIE%% *}"
    if [ -n "$DIEN" ]; then
      awk -v d="$DIEN" 'BEGIN{ exit !(d+0 >= 95) }' && bm_finding HIGH "Thermal" "CPU die ${DIE} at idle/light load" "far too hot - fan, heatsink or paste"
      awk -v d="$DIEN" 'BEGIN{ exit !(d+0 >= 80 && d+0 < 95) }' && bm_finding MED "Thermal" "CPU die ${DIE}" "warm for a bench idle; check under load"
    fi
    if [ -n "$FANS" ]; then
      FAN0="$(printf '%s' "$FANS" | grep -o '^[0-9]*')"
      [ -n "$FAN0" ] && [ "$FAN0" -eq 0 ] && [ "$(bm_lower "$MODEL_NAME")" != "macbook" ] && bm_finding MED "Thermal" "Fan reads 0 rpm" "fan not spinning or sensor fault (MacBook 12in has no fan - ignore there)"
      [ -n "$FAN0" ] && [ "$FAN0" -ge 5500 ] && bm_finding MED "Thermal" "Fan at ${FAN0} rpm at idle" "fans maxed out - heat, stuck sensor, or SMC"
    fi
    meta cpu_die "$DIE"; meta fans "$FANS"
  fi
  [ -s "$T/pm.txt" ] && bm_section "powermetrics sample" "$T/pm.txt"
else
  dim "(no admin: fan speed / die temperature skipped)"
fi

# =============================================================================
hdr "5. Storage"
# =============================================================================
diskutil list 2>/dev/null > "$T/disks.txt"; bm_section "diskutil list" "$T/disks.txt"
DF="$(df -k / 2>/dev/null | tail -n1)"
TOTK="$(printf '%s' "$DF" | awk '{ print $2 }')"; AVAILK="$(printf '%s' "$DF" | awk '{ print $4 }')"
PCT="$(printf '%s' "$DF" | awk '{ print $5 }' | tr -d '%')"
info "startup disk: $(bm_kb_to_human "$AVAILK") free of $(bm_kb_to_human "$TOTK") (${PCT}% used)"
meta disk_free "$(bm_kb_to_human "$AVAILK") of $(bm_kb_to_human "$TOTK")"
FREEPCT_D=$((100 - ${PCT:-0}))
if [ "$FREEPCT_D" -le 5 ]; then
  bm_finding HIGH "Storage" "Startup disk almost full (${FREEPCT_D}% free)" "macOS misbehaves (freezes, failed updates, 'disk full' at login) under ~10% free; clear space before anything else"
elif [ "$FREEPCT_D" -le 15 ]; then
  bm_finding MED "Storage" "Startup disk low on space (${FREEPCT_D}% free)" "aim for 15-20% free on APFS SSDs"
fi
# SMART on every physical disk. diskutil only says Verified/Failing (and ~1 in 4
# dying SSDs never trips it); the bundled smartctl reads the real NVMe / ATA
# health numbers when we have admin rights.
SMARTCTL="$(bm_smartctl)" || SMARTCTL=""
[ "$BM_SUDO" = "1" ] || SMARTCTL=""
[ -n "$SMARTCTL" ] && dim "smartctl $("$SMARTCTL" --version 2>/dev/null | head -n1 | awk '{ print $2 }') bundled - reading drive health counters"
SSD_WEAR=""; SSD_VERDICT=""; SSD_SUMMARY=""
smart_disk() {   # $1 = /dev/diskN  $2 = media name  -> findings + meta from smartctl
  local d="$1" nm="$2" out="$T/smart_${1#/dev/}.txt" kv v pct
  bm_timeout 45 sudo -n "$SMARTCTL" -a "$d" > "$out" 2>&1
  [ -s "$out" ] || return 1
  kv="$(bm_smart_kv "$out")"
  g() { printf '%s\n' "$kv" | awk -F= -v k="$1" '$1==k { print substr($0, length(k)+2); exit }'; }
  local type health model temp hours cycles wear spare thr media unsafe crit tbw realloc pending uncorrect crc failed err
  type="$(g type)"; health="$(g health)"; model="$(g model)"; temp="$(g temp)"; hours="$(g hours)"; cycles="$(g cycles)"
  wear="$(g wear)"; spare="$(g spare)"; thr="$(g spare_thr)"; media="$(g media_errors)"; unsafe="$(g unsafe)"; crit="$(g crit)"
  tbw="$(g tbw)"; realloc="$(g realloc)"; pending="$(g pending)"; uncorrect="$(g uncorrect)"; crc="$(g crc)"; failed="$(g failed)"; err="$(g error)"
  if [ "$type" = "none" ]; then
    dim "  smartctl: ${err:-no SMART data for $d (bridged / unsupported controller)}"
    bm_section "smartctl $d" "$out"
    return 1
  fi
  bm_section "smartctl -a $d  (${model:-$nm})" "$out"
  local yrs=""; [ -n "$hours" ] && yrs="$(awk -v h="$hours" 'BEGIN{ printf "%.1f", h/8760 }')"
  info "  ${model:-$nm}: health ${health}, ${wear:-?}% worn, ${tbw:-?} TB written, ${hours:-?} h on (${yrs:-?} y), ${cycles:-?} power cycles, ${temp:-?}C"
  meta "smart_${d#/dev/}" "type=$type health=$health wear=${wear:-?}% spare=${spare:-?}% tbw=${tbw:-?}TB hours=${hours:-?} cycles=${cycles:-?} media_errors=${media:-?} unsafe=${unsafe:-?} temp=${temp:-?}"
  local verdict="fine"
  case "$(bm_lower "$health")" in *fail*) bm_finding HIGH "Storage" "$d SMART overall health FAILED (${model:-$nm})" "the drive itself says it is failing - image it NOW (ddrescue / Data Recovery), replace before anything else"; verdict="replace" ;; esac
  if [ "$type" = "nvme" ]; then
    case "$crit" in
      ""|0x00|0x0) ;;
      *) bm_finding HIGH "Storage" "$d NVMe critical warning $crit (${model:-$nm})" "bit 0x01 spare below threshold, 0x02 over temperature, 0x04 reliability degraded, 0x08 read-only mode, 0x10 backup memory failed"; verdict="replace" ;;
    esac
    if [ -n "$media" ] && [ "$media" -gt 0 ] 2>/dev/null; then
      bm_finding HIGH "Storage" "$d ${media} media / data integrity error(s) (${model:-$nm})" "uncorrectable read/write errors on the flash - data loss is already happening; image + replace"; verdict="replace"
    fi
    if [ -n "$spare" ] && [ -n "$thr" ] && [ "$spare" -le "$thr" ] 2>/dev/null; then
      bm_finding HIGH "Storage" "$d spare blocks exhausted (${spare}% left, threshold ${thr}%)" "controller has no reserve flash left - replace"; verdict="replace"
    elif [ -n "$spare" ] && [ "$spare" -lt 50 ] 2>/dev/null && [ "${thr:-0}" -lt 50 ] 2>/dev/null; then
      bm_finding MED "Storage" "$d spare blocks at ${spare}% (threshold ${thr:-?}%)" "reserve flash is being used up - plan a replacement"; [ "$verdict" = "fine" ] && verdict="watch"
    fi
    if [ -n "$wear" ]; then
      if [ "$wear" -ge 90 ] 2>/dev/null; then bm_finding HIGH "Storage" "$d ${wear}% of rated write endurance used (${tbw:-?} TB written)" "at/over the vendor's TBW rating - replace before it fails"; verdict="replace"
      elif [ "$wear" -ge 70 ] 2>/dev/null; then bm_finding MED "Storage" "$d ${wear}% of rated write endurance used (${tbw:-?} TB written)" "wearing out - budget for a replacement, keep backups current"; [ "$verdict" = "fine" ] && verdict="watch"
      fi
    fi
    if [ -n "$unsafe" ] && [ -n "$cycles" ] && [ "$unsafe" -ge 20 ] 2>/dev/null; then
      pct="$(awk -v u="$unsafe" -v c="$cycles" 'BEGIN{ if (c>0) printf "%d", u*100/c; else print 0 }')"
      [ "$pct" -ge 30 ] && bm_finding INFO "Storage" "$d ${unsafe} unsafe shutdowns of ${cycles} power cycles (${pct}%)" "power is being cut mid-write: force-power-offs, dead battery, or a flaky power path - correlates with -128 / 0 shutdown causes below"
    fi
  else   # ATA / SATA (2012-2017 Macs, HDDs, Fusion drive halves)
    [ -n "$failed" ] && { bm_finding HIGH "Storage" "$d SMART attribute(s) past threshold: $failed" "drive is failing by its own thresholds - image + replace"; verdict="replace"; }
    if [ -n "$pending" ] && [ "$pending" -gt 0 ] 2>/dev/null; then bm_finding HIGH "Storage" "$d ${pending} pending (unreadable) sector(s)" "sectors that could not be read - data loss in progress; image with ddrescue, replace"; verdict="replace"; fi
    if [ -n "$uncorrect" ] && [ "$uncorrect" -gt 0 ] 2>/dev/null; then bm_finding HIGH "Storage" "$d ${uncorrect} offline-uncorrectable sector(s)" "surface damage - replace"; verdict="replace"; fi
    if [ -n "$realloc" ] && [ "$realloc" -gt 0 ] 2>/dev/null; then
      if [ "$realloc" -ge 50 ]; then bm_finding HIGH "Storage" "$d ${realloc} reallocated sectors" "the drive is running out of spares - replace"; verdict="replace"
      else bm_finding MED "Storage" "$d ${realloc} reallocated sector(s)" "early sign of surface/flash trouble - watch it, keep backups current"; [ "$verdict" = "fine" ] && verdict="watch"; fi
    fi
    if [ -n "$crc" ] && [ "$crc" -gt 0 ] 2>/dev/null; then bm_finding MED "Storage" "$d ${crc} SATA CRC error(s)" "interface errors, not the platters/flash: on a 2012-2015 MacBook Pro/iMac that is the classic HDD/SSD flex cable - replace the cable first"; [ "$verdict" = "fine" ] && verdict="watch"; fi
    if [ -n "$wear" ] && [ "$wear" != "" ]; then
      if [ "$wear" -ge 90 ] 2>/dev/null; then bm_finding HIGH "Storage" "$d SSD life ${wear}% used" "at the end of its rated endurance - replace"; verdict="replace"
      elif [ "$wear" -ge 70 ] 2>/dev/null; then bm_finding MED "Storage" "$d SSD life ${wear}% used" "wearing out - plan a replacement"; [ "$verdict" = "fine" ] && verdict="watch"; fi
    fi
  fi
  [ -n "$temp" ] && [ "$temp" -ge 70 ] 2>/dev/null && bm_finding MED "Storage" "$d running at ${temp}C" "SSD/HDD too hot - airflow, fan, or a heat-soaked chassis"
  [ -n "$hours" ] && [ "$hours" -ge 43800 ] 2>/dev/null && bm_finding INFO "Storage" "$d has ${hours} power-on hours (${yrs} years)" "old drive - fine while the counters above are clean, but say so on the quote"
  # first internal drive drives the JSON summary the dashboard shows
  if [ -z "$SSD_SUMMARY" ]; then
    SSD_WEAR="$wear"; SSD_VERDICT="$verdict"
    SSD_SUMMARY="${model:-$nm}: $verdict (${wear:-?}% worn, ${tbw:-?} TB written, health $health)"
  fi
  return 0
}
for d in $(diskutil list 2>/dev/null | grep -E '^/dev/disk[0-9]+ \((internal|external)' | grep -o '^/dev/disk[0-9]*' | sort -u); do
  DI="$(diskutil info "$d" 2>/dev/null)"
  printf '%s' "$DI" | grep -q 'Virtual: *Yes' && continue
  SM="$(printf '%s' "$DI" | grep -m1 'SMART Status' | sed 's/.*: *//')"
  NM="$(printf '%s' "$DI" | grep -m1 'Device / Media Name' | sed 's/.*: *//')"
  PR="$(printf '%s' "$DI" | grep -m1 'Protocol' | sed 's/.*: *//')"
  SS="$(printf '%s' "$DI" | grep -m1 'Solid State' | sed 's/.*: *//')"
  info "$d: ${NM:-?} [$PR, SSD:${SS:-?}] SMART (diskutil): ${SM:-n/a}"
  case "$(bm_lower "$SM")" in
    *fail*) bm_finding HIGH "Storage" "$d SMART FAILING (${NM})" "Drive is dying - back up NOW, replace before doing anything else" ;;
  esac
  DONE_SMART=0
  if [ -n "$SMARTCTL" ] && [ "$PR" != "USB" ] && [ "$PR" != "Disk Image" ]; then
    smart_disk "$d" "$NM" && DONE_SMART=1
  fi
  if [ "$DONE_SMART" = "0" ]; then
    case "$(bm_lower "$SM")" in
      *fail*|*verified*) ;;
      *) [ "$PR" = "USB" ] || bm_finding INFO "Storage" "$d SMART not reported (${NM}, $PR)" "$([ -n "$SMARTCTL" ] && printf 'smartctl could not read it either' || printf 'run with admin rights for the bundled smartctl health counters')" ;;
    esac
  fi
done
[ -n "$SSD_SUMMARY" ] && meta ssd "$SSD_SUMMARY"
[ -n "$SSD_WEAR" ] && meta ssd_wear "$SSD_WEAR"
[ -n "$SSD_VERDICT" ] && meta ssd_verdict "$SSD_VERDICT"
# optional short self-test (-SelfTest): ~2 min, drive-side, read-only
if [ "$SELFTEST" = "1" ] && [ -n "$SMARTCTL" ]; then
  for d in $(diskutil list 2>/dev/null | grep -E '^/dev/disk[0-9]+ \(internal' | grep -o '^/dev/disk[0-9]*'); do
    info "starting SMART short self-test on $d (about 2 minutes)..."
    if sudo -n "$SMARTCTL" -t short "$d" >/dev/null 2>&1; then
      n=0
      while [ "$n" -lt 30 ]; do
        sleep 10; n=$((n+1))
        sudo -n "$SMARTCTL" -l selftest "$d" 2>/dev/null > "$T/selftest.txt"
        grep -q -i 'in progress' "$T/selftest.txt" || break
      done
      bm_section "SMART self-test log $d" "$T/selftest.txt"
      if grep -q -i -E 'Completed without error|Self-test .*passed' "$T/selftest.txt"; then good "$d short self-test passed"
      elif grep -q -i -E 'fail|error' "$T/selftest.txt"; then bm_finding HIGH "Storage" "$d SMART short self-test FAILED" "$(grep -i -m1 -E 'fail|error' "$T/selftest.txt" | cut -c1-160)"
      else dim "  self-test result unclear - see report"; fi
    else
      dim "  $d does not support drive self-tests (typical for Apple NVMe)"
    fi
  done
fi
# APFS sanity: snapshots + purgeable
SNAPS="$(tmutil listlocalsnapshots / 2>/dev/null | grep -c 'com.apple')"
[ "${SNAPS:-0}" -gt 5 ] && bm_finding INFO "Storage" "$SNAPS local Time Machine snapshots on the startup disk" "they count as purgeable space; tmutil thinlocalsnapshots / 10000000000 4 frees them"
# Space offenders (fast du on the usual suspects)
{
  for p in "$HOME/Library/Caches" "/Library/Caches" "$HOME/Library/Logs" "/private/var/log" "$HOME/.Trash" "$HOME/Downloads" \
           "$HOME/Library/Application Support/MobileSync/Backup" "$HOME/Library/Developer/Xcode/DerivedData" \
           "$HOME/Library/Mail" "$HOME/Library/Containers/com.apple.mail" "/private/var/vm" "$HOME/Library/Application Support/Steam" \
           "$HOME/Library/Group Containers/group.com.apple.CoreSpeech" "/Library/Application Support/Apple/AssetCache"; do
    [ -d "$p" ] || continue
    k="$(bm_timeout 40 du -sk "$p" 2>/dev/null | awk '{ print $1 }')"
    [ -n "$k" ] && printf '%s\t%s\t%s\n' "$k" "$(bm_kb_to_human "$k")" "$p"
  done | sort -rn | head -n 10 | awk -F'\t' '{ printf "  %9s  %s\n", $2, $3 }'
} > "$T/offenders.txt"
bm_section "Space offenders" "$T/offenders.txt"
BIGK="$(du -sk "$HOME/Library/Application Support/MobileSync/Backup" 2>/dev/null | awk '{ print $1 }')"
[ -n "$BIGK" ] && [ "$BIGK" -gt 20971520 ] && bm_finding INFO "Storage" "iPhone/iPad backups use $(bm_kb_to_human "$BIGK")" "ask before deleting - Finder > device > Manage Backups"
CACHEK="$(du -sk "$HOME/Library/Caches" 2>/dev/null | awk '{ print $1 }')"
[ -n "$CACHEK" ] && [ "$CACHEK" -gt 10485760 ] && bm_finding INFO "Storage" "User caches $(bm_kb_to_human "$CACHEK")" "safe to clear (~/Library/Caches) if space is the complaint"

# =============================================================================
hdr "6. Battery + power"
# =============================================================================
system_profiler SPPowerDataType 2>/dev/null > "$T/power.txt"
if grep -q 'Cycle Count' "$T/power.txt"; then
  CYC="$(grep -m1 'Cycle Count' "$T/power.txt" | sed 's/.*: *//')"
  COND="$(grep -m1 'Condition' "$T/power.txt" | sed 's/.*: *//')"
  MAXCAP="$(grep -m1 'Maximum Capacity' "$T/power.txt" | sed 's/.*: *//' | tr -d '%')"
  FCC="$(grep -m1 'Full Charge Capacity' "$T/power.txt" | sed 's/.*: *//')"
  RAW="$(ioreg -rn AppleSmartBattery 2>/dev/null)"
  [ -n "$MAXCAP" ] || MAXCAP="$(printf '%s' "$RAW" | awk -F'= ' '/"AppleRawMaxCapacity"/ { m=$2 } /"DesignCapacity"/ { d=$2 } END { if (d>0 && m>0) printf "%d", m*100/d }')"
  PF="$(printf '%s' "$RAW" | grep -m1 '"PermanentFailureStatus"' | sed 's/.*= //')"
  TEMP="$(printf '%s' "$RAW" | grep -m1 '"Temperature"' | sed 's/.*= //' | awk '{ printf "%.0f", $1/100 }')"
  CHG="$(pmset -g batt 2>/dev/null | grep -o '[0-9]*%; [a-z ]*' | head -n1)"
  info "battery: ${CYC} cycles, condition '${COND}', max capacity ${MAXCAP:-?}%, ${CHG}, ${TEMP:-?}C"
  meta battery "cycles=$CYC condition=$COND max_capacity=${MAXCAP:-?}% fcc=$FCC"
  case "$(bm_lower "$COND")" in
    *"service"*|*"replace"*) bm_finding HIGH "Battery" "Battery condition: $COND (${CYC} cycles, ${MAXCAP:-?}%)" "macOS is asking for a battery replacement" ;;
  esac
  [ -n "$PF" ] && [ "$PF" != "0" ] && bm_finding HIGH "Battery" "PermanentFailureStatus = $PF" "battery gauge reports a permanent fault - replace"
  if [ -n "$MAXCAP" ]; then
    [ "$MAXCAP" -lt 70 ] && bm_finding HIGH "Battery" "Max capacity ${MAXCAP}% (${CYC} cycles)" "well past service threshold (Apple: 80%)"
    [ "$MAXCAP" -ge 70 ] && [ "$MAXCAP" -lt 80 ] && bm_finding MED "Battery" "Max capacity ${MAXCAP}% (${CYC} cycles)" "below Apple's 80% service threshold"
  fi
  [ -n "$CYC" ] && [ "$CYC" -ge 1000 ] && bm_finding MED "Battery" "${CYC} charge cycles" "rated life is 1000 cycles on all Macs since 2010"
  [ -n "$TEMP" ] && [ "$TEMP" -ge 45 ] && bm_finding MED "Battery" "Battery ${TEMP}C" "hot battery - swollen? check trackpad/bottom case for bulge"
  # charger
  CHGW="$(grep -m1 'Wattage' "$T/power.txt" | sed 's/.*: *//')"
  CHGN="$(grep -m1 -A12 'AC Charger Information' "$T/power.txt" | grep -m1 'Connected' | sed 's/.*: *//')"
  [ "$CHGN" = "Yes" ] && info "charger connected: ${CHGW:-?}W"
  if [ "$CHGN" = "Yes" ] && [ -n "$CHGW" ] && [ "$CHGW" -lt 30 ] && printf '%s' "$MODEL_NAME" | grep -qi 'pro'; then
    bm_finding INFO "Power" "Low-wattage charger (${CHGW}W) on a MacBook Pro" "charges slowly / drains under load - not a fault"
  fi
  grep -q 'Charging: *No' "$T/power.txt" && [ "$CHGN" = "Yes" ] && [ "${CHG%%%*}" -lt 90 ] 2>/dev/null && bm_finding INFO "Power" "Plugged in but not charging" "optimised charging hold, hot battery, or a charging-circuit fault - watch it"
else
  info "no battery (desktop Mac)"
fi
bm_section "Power / battery" "$T/power.txt"

# =============================================================================
hdr "7. Crashes, panics + unexpected shutdowns (last $DAYS days)"
# =============================================================================
# ---- kernel panics: collect, then CLASSIFY (which subsystem, which kext) -----
# Sources: /Library/Logs/DiagnosticReports (*.panic, Kernel-*.ips, panic-full-*.ips),
# /var/db/PanicReporter (Apple silicon, root-only), and the T2's retired
# bridgeOS panics. Each file is bucketed by its panic string so the report says
# "3 GPU panics" instead of "3 panics", and any non-Apple kext in a backtrace
# is called out - that is the single most actionable panic finding there is.
mkdir -p "$T/panics"; : > "$T/panlist.txt"
find /Library/Logs/DiagnosticReports -maxdepth 1 \( -name '*.panic' -o -name 'Kernel*.ips' -o -name 'panic*.ips' \) -mtime -"${DAYS}"d 2>/dev/null >> "$T/panlist.txt"
if [ "$BM_SUDO" = "1" ]; then
  sudo -n find /var/db/PanicReporter /Library/Logs/DiagnosticReports/ProxiedDevice-Bridge/Retired -maxdepth 1 \( -name '*.panic' -o -name '*.ips' \) -mtime -"${DAYS}"d 2>/dev/null >> "$T/panlist.txt"
fi
# newest first (date -r works on every macOS since 10.13 and on GNU; unreadable = 0 = last)
sort -u "$T/panlist.txt" | while IFS= read -r f; do [ -n "$f" ] && printf '%s %s\n' "$(date -r "$f" +%s 2>/dev/null || printf 0)" "$f"; done | sort -rn | cut -d' ' -f2- | head -n 40 > "$T/panlist2.txt"
PANICS="$(grep -c . "$T/panlist2.txt")"
# bucket name for a panic file's text -> prints "bucket|hint"
panic_bucket() {
  local f="$1"
  if grep -q -i -E 'SEP Panic|sep panic|AppleSEP' "$f"; then printf 'SEP (Secure Enclave)|Touch ID / secure element / firmware - if repeated and SEP-only, it is the logic board or the bridge, not software'
  elif grep -q -i -E 'Userspace watchdog|no successful checkins from com\.apple\.WindowServer' "$f"; then printf 'WindowServer watchdog|the display server stopped responding - GPU / display driver first, then a misbehaving kext or an app spinning the GPU'
  elif grep -q -i -E 'watchdog|WDT timeout|wdt timeout' "$f"; then printf 'watchdog|the machine hung and the watchdog reset it - firmware / thermal / power-rail hangs, also bad RAM'
  elif grep -q -i -E 'Sleep Wake failure|sleep transition timed out|wake transition timed out|Wake transition' "$f"; then printf 'sleep/wake|failed sleep or wake - drivers (USB/Thunderbolt hubs, third-party kexts), then SMC/PMU'
  elif grep -q -i -E 'AGX|IOGPU|IOAccelerator|AppleParavirtGPU|GPU|AMDRadeon|IntelGraphics|NVDA' "$f"; then printf 'GPU|graphics stack - on Intel dGPU Macs (2011/2015/2016-17 15") this is the classic failing-GPU signature; on Apple silicon it is usually a driver/kext or the SoC'
  elif grep -q -i -E 'AppleSMC|IOPlatformPlugin|SMC|PMU|AppleARMPMU|power management' "$f"; then printf 'SMC / power|power-management - liquid damage, failing charging circuit, or a battery/PMU fault; check the -74/-78/-79 shutdown codes below'
  elif grep -q -i -E 'ANS2|AppleANS|IONVMe|NVMe|AppleAPFS|apfs_|APFS' "$f"; then printf 'storage / NVMe|the SSD controller or APFS - correlate with the smartctl counters above; ANS2 panics on Apple silicon usually mean the flash or the board'
  elif grep -q -i -E 'zone map exhausted|zalloc|out of memory|kalloc' "$f"; then printf 'kernel memory exhausted|a kernel extension leaking memory - find the third-party kext, then suspect VPN/antivirus/virtualisation drivers'
  elif grep -q -i -E 'Kernel data abort|page fault|instruction fetch abort|Invalid kernel stack|Kernel trap|Double fault|general protection' "$f"; then printf 'memory access fault|corrupt memory or a buggy driver - if no third-party kext is named, test the RAM (Apple Diagnostics / Repair Assistant) and the SSD'
  elif grep -q -i -E 'AppleUSB|IOUSB|xhci|USB' "$f"; then printf 'USB stack|a USB device or hub driver - unplug everything and retest; if it persists with nothing attached, the port/controller'
  elif grep -q -i -E 'Thunderbolt' "$f"; then printf 'Thunderbolt|a Thunderbolt/USB-C dock, display or the controller'
  elif grep -q -i -E 'AppleBCMWLAN|IO80211|wifi|Bluetooth|BTCore|AppleBluetooth' "$f"; then printf 'Wi-Fi / Bluetooth|the wireless card or its driver'
  elif grep -q -i -E 'Machine-check|MCA' "$f"; then printf 'machine check (CPU)|the CPU itself reported a hardware error - board-level'
  elif grep -q -i -E 'bridgeOS|ProxiedDevice|T2' "$f"; then printf 'T2 / bridgeOS|the T2 security chip panicked - firmware revive (DFU) first, then the board'
  else printf 'other|see the panic text in the report'; fi
}
: > "$T/panics.txt"; : > "$T/pankexts.txt"; : > "$T/panloaded.txt"; : > "$T/panbuckets.txt"; LASTP=""
if [ "$PANICS" -gt 0 ]; then
  i=0
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    i=$((i+1)); pf="$T/panics/$i.txt"
    bm_su head -c 400000 "$f" 2>/dev/null > "$pf"
    [ -s "$pf" ] || continue
    [ -n "$LASTP" ] || LASTP="$f"
    when="$(date -r "$f" '+%Y-%m-%d %H:%M' 2>/dev/null || bm_su stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$f" 2>/dev/null)"
    why="$(grep -m1 -o '"panicString" *: *"[^"]\{0,240\}' "$pf" 2>/dev/null | sed 's/"panicString" *: *"//')"
    [ -n "$why" ] || why="$(grep -m1 -i 'panic(' "$pf" 2>/dev/null | cut -c1-240)"
    b="$(panic_bucket "$pf")"; bucket="${b%%|*}"
    # third-party kexts: anything in the backtrace / loaded list that is not com.apple.*
    # (JSON panics keep the list inside the panicString with \n separators, hence the tr)
    kexts="$(awk '{ gsub(/\\n/, "\n"); gsub(/\\t/, " "); print }' "$pf" | grep -o -E '(^|[[:space:]])[a-zA-Z0-9_]+\.[a-zA-Z0-9_.-]+\([0-9][0-9.]*\)' | sed 's/^[[:space:]]*//; s/(.*//' | grep -v -E '^com\.apple\.' | sort -u | head -n 8 | tr '\n' ' ')"
    # third-party kexts merely LOADED at panic time (the "loaded kexts:" list: name<tab>version) - weaker evidence
    loaded="$(awk '{ gsub(/\\n/, "\n"); gsub(/\\t/, " "); print }' "$pf" | awk 'tolower($0) ~ /^loaded kexts:/ { on=1; next } on && /^[A-Za-z0-9_]+\.[A-Za-z0-9_.-]+[ \t]+[0-9]/ { print $1 } on && /^[[:space:]]*$/ { on=0 }' | grep -v -E '^com\.apple\.' | sort -u | head -n 8 | tr '\n' ' ')"
    onwake=""; grep -q -i -E 'wake|sleep' "$pf" && onwake=" [sleep/wake context]"
    printf '%s\n' "$bucket" >> "$T/panbuckets.txt"
    [ -n "$kexts" ] && printf '%s\n' "$kexts" | tr ' ' '\n' | grep . >> "$T/pankexts.txt"
    [ -n "$loaded" ] && printf '%s\n' "$loaded" | tr ' ' '\n' | grep . >> "$T/panloaded.txt"
    printf '%s  %-22s %s%s\n      %s\n      %s%s\n' "$when" "$bucket" "$(basename "$f")" "$onwake" "${why:-(no panic string found)}" "${kexts:+3rd-party kexts in backtrace: $kexts}" "${loaded:+ | loaded: $loaded}" >> "$T/panics.txt"
  done < "$T/panlist2.txt"
  info "$PANICS kernel panic file(s) in $DAYS days:"; sed 's/^/  /' "$T/panics.txt" | cut -c1-200
  bm_section "Kernel panics - classified ($DAYS days)" "$T/panics.txt"
  [ -n "$LASTP" ] && { head -c 6000 "$T/panics/1.txt" > "$T/lastpanic.txt" 2>/dev/null; bm_section "Latest kernel panic ($(basename "$LASTP"))" "$T/lastpanic.txt"; }
  BUCKETS="$(sort "$T/panbuckets.txt" | uniq -c | sort -rn | awk '{ c=$1; $1=""; sub(/^ /,""); printf "%s%s x%d", (NR>1?"; ":""), $0, c }')"
  TOPB="$(sort "$T/panbuckets.txt" | uniq -c | sort -rn | head -n1 | awk '{ $1=""; sub(/^ /,""); print }')"
  TOPHINT=""; for pf in "$T"/panics/*.txt; do b="$(panic_bucket "$pf")"; [ "${b%%|*}" = "$TOPB" ] && { TOPHINT="${b#*|}"; break; }; done
  KEXTS="$(sort "$T/pankexts.txt" | uniq -c | sort -rn | awk '{ printf "%s%s(x%d)", (NR>1?" ":""), $2, $1 }')"
  meta panics "count=$PANICS; $BUCKETS${KEXTS:+; kexts: $KEXTS}${LOADED:+; loaded: $LOADED}"
  LOADED="$(sort "$T/panloaded.txt" | uniq -c | sort -rn | awk '{ printf "%s%s(x%d)", (NR>1?" ":""), $2, $1 }')"
  if [ -n "$KEXTS" ]; then
    bm_finding HIGH "Stability" "Third-party kernel extension(s) in panic backtraces: $KEXTS" "remove/update that driver FIRST (VPN, antivirus, audio, virtualisation, dock/USB drivers are the usual suspects) - a hardware verdict is not safe until the panics happen without it"
  elif [ -n "$LOADED" ]; then
    bm_finding MED "Stability" "Third-party kernel extension(s) were loaded when it panicked: $LOADED" "not in the backtrace, so weaker evidence - still unload/uninstall them and retest before blaming hardware"
  fi
  if [ "$PANICS" -ge 2 ]; then
    bm_finding HIGH "Stability" "$PANICS kernel panics in $DAYS days ($BUCKETS)" "$TOPB: ${TOPHINT:-repeated panics are hardware more often than software}"
  else
    bm_finding MED "Stability" "1 kernel panic in $DAYS days ($TOPB)" "${TOPHINT}. One panic can be a fluke - retest after updates, treat two as a pattern"
  fi
  case "$BUCKETS" in *"sleep/wake"*|*"SMC / power"*) bm_finding INFO "Stability" "Panics in the sleep/wake or SMC/power buckets" "with keys double-typing, fans high at idle or charge asymmetry between ports, suspect liquid damage - check the LCI stickers inside the USB-C ports" ;; esac
elif [ "$BM_SUDO" != "1" ]; then
  dim "(no panic files in the user-readable log folder; /var/db/PanicReporter needs admin rights)"
fi
# app crashes (user + system)
find "$HOME/Library/Logs/DiagnosticReports" /Library/Logs/DiagnosticReports -maxdepth 1 \( -name '*.crash' -o -name '*.ips' \) -mtime -"${DAYS}"d 2>/dev/null \
  | grep -E -v -i 'panic|Kernel' | sed 's#.*/##; s/[-_][0-9][0-9][0-9][0-9]-[0-9].*//; s/\.crash$//; s/\.ips$//' | sort | uniq -c | sort -rn | head -n 15 > "$T/crashes.txt"
NCR="$(awk '{ s+=$1 } END { print s+0 }' "$T/crashes.txt")"
if [ "$NCR" -gt 0 ]; then
  info "$NCR app crash reports in $DAYS days:"; sed 's/^/  /' "$T/crashes.txt"
  bm_section "App crashes (count, process)" "$T/crashes.txt"
  TOPCR="$(head -n1 "$T/crashes.txt" | awk '{ print $2 " (" $1 "x)" }')"
  TOPN="$(head -n1 "$T/crashes.txt" | awk '{ print $1 }')"
  [ "$TOPN" -ge 5 ] && bm_finding MED "Stability" "Repeated crashes: $TOPCR" "same process crashing over and over - update/reinstall it, check its plug-ins; if it's WindowServer/kernel-adjacent think GPU/RAM"
  [ "$NCR" -ge 30 ] && bm_finding MED "Stability" "$NCR crash reports in $DAYS days" "$(head -n3 "$T/crashes.txt" | awk '{ printf "%s x%s; ", $2, $1 }')"
fi
# shutdown causes
info "reading shutdown causes from the unified log (may take ~20 s)..."
bm_timeout 90 log show --last "${DAYS}d" --style compact --predicate 'eventMessage CONTAINS "Previous shutdown cause"' 2>/dev/null | grep -o 'Previous shutdown cause: *-\{0,1\}[0-9]*' | sed 's/.*: *//' | sort | uniq -c | sort -rn > "$T/shutdown.txt"
if [ -s "$T/shutdown.txt" ]; then
  sc_name() { case "$1" in
    5) printf 'normal shutdown';; 0) printf 'power lost / battery disconnected';; 3) printf 'hard shutdown (power button held / hang)';;
    -128) printf 'unexpected - could be anything (often a panic or PMU)';; -60) printf 'bad master directory block (disk)';; -61|-62) printf 'watchdog timeout (hang)';;
    -71) printf 'SO-DIMM memory temperature';; -74) printf 'battery temperature too high';; -75) printf 'AC adapter communication problem';;
    -78) printf 'incorrect input current (charger)';; -79) printf 'incorrect current from battery';; -86) printf 'proximity temperature (heatpipe)';;
    -95) printf 'CPU temperature';; -100) printf 'power supply temperature';; -101) printf 'display temperature';; -103) printf 'battery voltage too low';;
    -104) printf 'unknown battery';; -108) printf 'memory/thermal';; -112) printf 'heatsink temperature';; -127) printf 'PMU/SMC forced shutdown';;
    -20) printf 'BridgeOS (T2) panic';; -14) printf 'electrical / thermal reset';; *) printf 'code %s' "$1";; esac; }
  while read -r cnt code; do
    [ -n "$code" ] || continue
    printf '  %3sx  %-5s %s\n' "$cnt" "$code" "$(sc_name "$code")"
    case "$code" in
      5) ;;
      3) [ "$cnt" -ge 3 ] && bm_finding MED "Stability" "${cnt} hard shutdowns (power button held)" "customer is force-powering off - hangs / black screens?" ;;
      0) [ "$cnt" -ge 2 ] && bm_finding MED "Power" "${cnt} shutdowns from power loss / battery disconnect" "flaky battery connector, dead battery on a laptop, or a desktop on a bad outlet/strip" ;;
      -128) bm_finding MED "Stability" "${cnt} unexpected shutdown(s) (-128)" "not a clean shutdown - correlate with panics above" ;;
      -74|-95|-100|-101|-112|-86|-71|-108) bm_finding HIGH "Thermal" "${cnt} THERMAL shutdown(s): $(sc_name "$code") ($code)" "the SMC/PMU cut power for heat - fan, sensor, paste, or the part it names" ;;
      -75|-78|-79|-103|-104) bm_finding HIGH "Power" "${cnt} power-circuit shutdown(s): $(sc_name "$code") ($code)" "charger / battery / charging circuit fault" ;;
      -127|-20|-14|-60|-61|-62) bm_finding MED "Stability" "${cnt} shutdown(s): $(sc_name "$code") ($code)" "" ;;
    esac
  done < "$T/shutdown.txt"
  bm_section "Shutdown causes ($DAYS days)" "$T/shutdown.txt"
else
  dim "(no shutdown-cause entries found in the log window)"
fi
last reboot shutdown 2>/dev/null | head -n 12 > "$T/reboots.txt"; bm_section "Recent reboots/shutdowns" "$T/reboots.txt"

# =============================================================================
hdr "8. Startup items, agents + extensions"
# =============================================================================
: > "$T/agents.txt"
ORPH=0; NAGENT=0; THIRD=0
for dir in /Library/LaunchAgents /Library/LaunchDaemons "$HOME/Library/LaunchAgents"; do
  [ -d "$dir" ] || continue
  for f in "$dir"/*.plist; do
    [ -f "$f" ] || continue
    NAGENT=$((NAGENT+1))
    prog="$(bm_launchd_program "$f")"
    lbl="$(bm_plist "$f" Label)"
    case "$lbl" in com.apple.*) continue ;; esac
    THIRD=$((THIRD+1))
    st="ok"
    if [ -n "$prog" ] && [ ! -e "$prog" ]; then st="MISSING"; ORPH=$((ORPH+1)); fi
    printf '%-8s %-45s %s\n' "$st" "$lbl" "$prog" >> "$T/agents.txt"
  done
done
info "$NAGENT launchd items, $THIRD third-party, $ORPH orphaned (program file missing)"
bm_section "Third-party launch agents/daemons" "$T/agents.txt"
[ "$ORPH" -ge 1 ] && bm_finding INFO "Startup" "$ORPH launch item(s) point at a missing program" "leftovers of uninstalled apps - $(grep '^MISSING' "$T/agents.txt" | awk '{ print $2 }' | head -n5 | tr '\n' ' ')"
[ "$THIRD" -ge 25 ] && bm_finding MED "Startup" "$THIRD third-party background items" "a lot for a home Mac - slow logins, battery drain; review with the customer"
LI="$(osascript -e 'tell application "System Events" to get the name of every login item' 2>/dev/null)"
[ -n "$LI" ] && { info "login items: $LI"; meta login_items "$LI"; }
if [ "$AS" = "0" ]; then
  kextstat 2>/dev/null | grep -v 'com.apple' | awk 'NR>1 { print $6 }' > "$T/kexts.txt"
  NK="$(grep -c . "$T/kexts.txt")"
  [ "$NK" -gt 0 ] && { info "third-party kernel extensions: $(tr '\n' ' ' < "$T/kexts.txt")"; bm_finding INFO "Extensions" "$NK third-party kernel extension(s) loaded" "$(tr '\n' ' ' < "$T/kexts.txt" | cut -c1-250) - suspects for panics"; }
fi
systemextensionsctl list 2>/dev/null | grep -E -v '^---|^enabled|^[0-9]* extension' | grep -E '\*|activated' > "$T/sysext.txt"
NSE="$(grep -c . "$T/sysext.txt")"
[ "$NSE" -gt 0 ] && { info "$NSE system extension(s) active"; bm_section "System extensions" "$T/sysext.txt"; }

# =============================================================================
hdr "9. Security posture + updates"
# =============================================================================
SIP="$(csrutil status 2>/dev/null | head -n1)"; GK="$(spctl --status 2>/dev/null)"
FWS="$(/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate 2>/dev/null)"
info "$SIP | Gatekeeper: ${GK:-?} | Firewall: ${FWS:-?}"
meta sip "$SIP"; meta gatekeeper "$GK"
printf '%s' "$SIP" | grep -qi 'disabled' && bm_finding MED "Security" "System Integrity Protection is DISABLED" "someone turned SIP off (Recovery > csrutil). Fine on a dev box, a red flag on a customer Mac - re-enable unless they know why"
printf '%s' "$GK" | grep -qi 'disabled' && bm_finding MED "Security" "Gatekeeper is disabled" "unsigned apps run without a warning - re-enable: sudo spctl --master-enable"
XPV=""
for pl in /Library/Apple/System/Library/CoreServices/XProtect.bundle/Contents/Info.plist /System/Library/CoreServices/XProtect.bundle/Contents/Info.plist; do
  [ -f "$pl" ] && { XPV="$(bm_plist "$pl" CFBundleShortVersionString)"; break; }
done
[ -n "$XPV" ] && { info "XProtect (built-in malware definitions): $XPV"; meta xprotect "$XPV"; }
AUC="$(defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled 2>/dev/null)"
[ "$AUC" = "0" ] && bm_finding INFO "Updates" "Automatic update checks are OFF" ""
info "checking for macOS updates (softwareupdate, ~30 s)..."
bm_timeout 75 softwareupdate -l 2>&1 | grep -E -i 'Title:|\* Label|No new software' | head -n 8 > "$T/updates.txt"
if grep -E -qi 'Label|Title' "$T/updates.txt"; then
  UPD="$(grep -E -io 'Label: [^,]*|Title: [^,]*' "$T/updates.txt" | sed 's/^[A-Za-z]*: //' | tr '\n' ';' | cut -c1-200)"
  bm_finding INFO "Updates" "Updates available" "$UPD"
elif grep -qi 'No new software' "$T/updates.txt"; then good "macOS is up to date"; fi
# admin accounts
ADMINS="$(dscl . -read /Groups/admin GroupMembership 2>/dev/null | sed 's/GroupMembership: //')"
info "admin users: $ADMINS"; meta admins "$ADMINS"
NADM="$(printf '%s' "$ADMINS" | wc -w | tr -d ' ')"
[ "$NADM" -ge 4 ] && bm_finding INFO "Security" "$NADM admin accounts" "$ADMINS"
ROOTEN="$(dscl . -read /Users/root AuthenticationAuthority 2>/dev/null | grep -c 'ShadowHash')"
[ "$ROOTEN" -gt 0 ] && bm_finding MED "Security" "The root account is ENABLED" "unusual on a personal Mac"
RL="$(bm_su systemsetup -getremotelogin 2>/dev/null)"
printf '%s' "$RL" | grep -qi ': On' && bm_finding INFO "Security" "Remote Login (SSH) is on" ""
if launchctl print system 2>/dev/null | grep -E -q 'com.apple.screensharing.agent|screensharing"'; then info "Screen Sharing / Remote Management service loaded"; fi

# =============================================================================
hdr "10. Network"
# =============================================================================
DNS="$(scutil --dns 2>/dev/null | grep 'nameserver\[' | awk '{ print $3 }' | sort -u | tr '\n' ' ')"
info "DNS: ${DNS:-none}"; meta dns "$DNS"
PROXY="$(scutil --proxy 2>/dev/null | grep -i 'Enable : 1' | grep -v 'ExceptionsList' )"
if [ -n "$PROXY" ]; then
  PX="$(scutil --proxy 2>/dev/null | grep -E -i 'Proxy :|ProxyAutoConfigURLString|Port :' | tr -s ' ' | tr '\n' ' ' | cut -c1-200)"
  bm_finding MED "Network" "A proxy / PAC is configured" "$PX - adware and 'VPN' junk hijack traffic this way; also breaks internet when the proxy app is gone"
fi
HOSTS="$(grep -E -v '^[[:space:]]*#|^[[:space:]]*$|localhost|broadcasthost|^::1' /etc/hosts 2>/dev/null | head -n 5 | tr '\n' ';')"
[ -n "$HOSTS" ] && bm_finding INFO "Network" "Custom /etc/hosts entries" "$HOSTS"
if ping -c 1 -t 3 1.1.1.1 >/dev/null 2>&1; then
  good "internet reachable"
  if ! dscacheutil -q host -a name apple.com 2>/dev/null | grep -q 'ip_address'; then bm_finding HIGH "Network" "DNS is not resolving (apple.com)" "IP works, names don't - DNS servers above are wrong/dead or a filtering profile is installed"; fi
else
  bm_finding INFO "Network" "No internet right now" "Wi-Fi off / not joined on the bench? AI section will be skipped"
fi
if [ "$OSMAJ" -ge 11 ] && [ "$BM_SUDO" = "1" ]; then
  bm_timeout 20 sudo -n wdutil info 2>/dev/null | grep -E -i 'SSID|RSSI|Channel|Tx Rate|Security' | grep -v 'BSSID' | head -n 6 > "$T/wifi.txt"
else
  system_profiler SPAirPortDataType 2>/dev/null | grep -A14 'Current Network Information' | grep -E 'PHY Mode|Channel|Signal|Transmit Rate|Security' | head -n 6 > "$T/wifi.txt"
fi
[ -s "$T/wifi.txt" ] && { sed 's/^/  /' "$T/wifi.txt"; bm_section "Wi-Fi" "$T/wifi.txt"; }
RSSI="$(grep -E -io 'RSSI: *-[0-9]*|Signal / Noise: *-[0-9]*' "$T/wifi.txt" 2>/dev/null | grep -o '\-[0-9]*' | head -n1)"
[ -n "$RSSI" ] && [ "$RSSI" -lt -75 ] && bm_finding INFO "Network" "Weak Wi-Fi signal (${RSSI} dBm) on the bench" "judge Wi-Fi complaints near the customer's router, not here"
bm_timeout 20 lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1 { print $1, $3, $9 }' | sort -u > "$T/listen.txt"
NL="$(grep -vc '^$' "$T/listen.txt")"; [ "$NL" -gt 0 ] && bm_section "Listening TCP ports" "$T/listen.txt"
grep -E -v '^(launchd|rapportd|ControlCe|AirPlayXP|sharingd|mDNSRespo|cupsd|remoted|identitys|screensha|ARDAgent) ' "$T/listen.txt" | grep -E -v ' 127.0.0.1:| \[::1\]:' | head -n 5 > "$T/listen_odd.txt"
[ -s "$T/listen_odd.txt" ] && bm_finding INFO "Network" "Non-Apple programs listening on the network" "$(awk '{ printf "%s@%s; ", $1, $3 }' "$T/listen_odd.txt")"

# =============================================================================
hdr "11. Backups + display"
# =============================================================================
TMD="$(tmutil destinationinfo 2>/dev/null | grep -m1 'Name' | sed 's/.*: *//')"
TML="$(tmutil latestbackup 2>/dev/null | sed 's#.*/##; s/\.backup$//; s/\.previous$//')"
if [ -n "$TMD" ]; then
  info "Time Machine -> $TMD, latest: ${TML:-none yet}"; meta time_machine "$TMD latest=$TML"
  if [ -n "$TML" ]; then
    TMAGE="$(( ( $(date +%s) - $(date -j -f '%Y-%m-%d-%H%M%S' "$TML" +%s 2>/dev/null || date +%s) ) / 86400 ))"
    [ "$TMAGE" -ge 30 ] && bm_finding MED "Backup" "Last Time Machine backup is $TMAGE days old" "back up before any repair that touches the SSD"
  fi
else
  bm_finding MED "Backup" "No Time Machine destination configured" "no local backup - confirm iCloud/other backup before working on the storage; offer a backup"
fi
system_profiler SPDisplaysDataType 2>/dev/null > "$T/disp.txt"; bm_section "Graphics / displays" "$T/disp.txt"
GPU="$(grep -m1 'Chipset Model' "$T/disp.txt" | sed 's/.*: *//')"; meta gpu "$GPU"
grep -E -q 'Display Type|Resolution' "$T/disp.txt" || bm_finding INFO "Display" "No display reported by the GPU" "clamshell / headless right now, or a display fault"

# =============================================================================
hdr "12. Verdict"
# =============================================================================
bm_recount
VERDICT="$(bm_verdict)"
printf '\n%s%s  VERDICT: %s   (HIGH %d / MED %d / INFO %d)%s\n\n' "$C_BOLD" "$C_CYAN" "$VERDICT" "$BM_HIGH" "$BM_MED" "$BM_INFO" "$C_RESET"

AI="$T/ai.txt"; : > "$AI"
{
  printf 'MAC UNDER TEST\n'; cat "$META"
  printf '\nFINDINGS (severity | category | title | detail)\n'
  awk -F"$BM_US" '{ printf "%s | %s | %s | %s\n", $1, $2, $3, $4 }' "$BM_FINDINGS"
  printf '\nTOP CPU\n'; cat "$T/topcpu.txt"
  printf '\nTOP MEMORY\n'; cat "$T/topmem.txt"
  [ -s "$T/shutdown.txt" ] && { printf '\nSHUTDOWN CAUSES (count code)\n'; cat "$T/shutdown.txt"; }
  [ -s "$T/crashes.txt" ] && { printf '\nCRASH COUNTS\n'; cat "$T/crashes.txt"; }
  [ -s "$T/panics.txt" ] && { printf '\nKERNEL PANICS - CLASSIFIED (date, bucket, file, panic string, 3rd-party kexts)\n'; head -c 4000 "$T/panics.txt"; }
  [ -s "$T/lastpanic.txt" ] && { printf '\nLATEST PANIC (head)\n'; head -c 2500 "$T/lastpanic.txt"; }
  for sf in "$T"/smart_disk*.txt; do [ -s "$sf" ] && { printf '\nSMARTCTL %s\n' "$(basename "$sf" .txt | sed 's/smart_//')"; grep -E -i 'Model|health|Critical|Temperature:|Spare|Percentage Used|Data Units Written|Power On Hours|Power Cycles|Unsafe|Integrity|Reallocated|Pending|Uncorrect|CRC|Wear|Life|Total_LBAs' "$sf" | head -n 24; }; done
} > "$T/ai_user.txt"
SYS='You are a senior Apple-certified Mac technician (ACMT) at an independent repair shop bench. You are given the structured output of a read-only health check run on a customer Mac. Write for the technician, in plain English, no marketing.
Respond with exactly these sections:
1. VERDICT - one line: what is most likely wrong (or that the Mac looks healthy) and how confident you are.
2. WHAT THE EVIDENCE SAYS - ranked list of the real problems, each tied to the specific findings/codes that prove it. Ignore noise. Say when two findings share one root cause (e.g. thermal shutdowns + kernel_task + throttling = cooling).
3. NEXT BENCH TESTS - concrete ordered steps to confirm (Repair Assistant on Apple silicon + macOS 26 / Apple Diagnostics on older Macs, ASD/AHT, stress test under load with fan RPM, SMC/NVRAM reset on Intel, DFU revive/restore, disk First Aid, safe mode, new user account, removing a named third-party kext, etc.) and what result would confirm/exclude each cause.
4. LIKELY PARTS / WORK - the repair(s) that would fix it (battery, fan, thermal paste, SSD, logic-board level, or software-only). Never quote prices.
5. TRADE-IN / HANDOVER NOTES - only if Activation Lock, MDM/DEP, iCloud or backup findings exist; otherwise write "none".
6. CUSTOMER NOTE - 3-4 sentences a front-desk person can read to the customer: what we found, what it means for them, what we recommend. No jargon, no prices.'
if bm_gemini "$SYS" "$T/ai_user.txt" > "$AI"; then
  printf '\n%s---- AI TRIAGE (%s) ----%s\n' "$C_BOLD" "$BM_AI_MODEL" "$C_RESET"; cat "$AI"; printf '\n'
fi

bm_write_reports "$META" "$AI" "$VERDICT"
bm_open_report
bm_pause
exit 0
