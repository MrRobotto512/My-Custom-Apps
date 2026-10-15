#!/bin/bash
# api.sh - the dashboard's data layer. Prints ONE response body to stdout.
#   api.sh <route> [query-string]
# Routes (all JSON unless noted):
#   stats      live snapshot (collector.sh)         history    graph series (last N ticks)
#   sysinfo    hardware / OS / security (cached)     processes  top 80 processes
#   startup    third-party launchd items             users      local user accounts
#   services   launchctl list (user + system)        disks      physical disks + volumes (+ smartctl health)
#   reports    every report on the stick             report?f=  one report (text/plain)
#   log?app=   tail of an app's last-run log (text)   run?app=   launch an app in Terminal
#   quit       stop the dashboard                    ports      charger + USB + Thunderbolt snapshot (Tests page)
#   qcsave     POST: save a QC checklist (text + JSON) into $RUN/qc -> Dashboard/QC-Reports/
#   job        POST: queue a collector job (cleanup scan / clean / maintenance / tool_run / files_* / stress)   jobstatus?id=  cleanlast
#   files_meta / files_ls?p= / files_find?q=&type=&min= / files_map / files_dests   -> the Files page (reads $RUN/files index)
#   battery    ioreg AppleSmartBattery: health %, cycles, temp, failure flag, serial/date, charger W (no admin needed)
#   intake     Intake / trade-in gate bundle: sysinfo + battery + disk0 (setup mode leads with it)
#   bench      POST: forward the page's JSON (intake + QC) to the bench URL from -Bench ($RUN/bench.url)
# Called by the nc server and by api.cgi (httpd). bash 3.2, no python.
DASH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE="${BM_PROFILE:-$(cd "$DASH/.." && pwd)}"
RUN="${BM_RUN:-$(cat "$DASH/.rundir" 2>/dev/null)}"; [ -d "$RUN" ] || RUN="${TMPDIR:-/tmp}/benchmac-dash"
# Tool logs + reports: read the collector's mirror in the run dir (on the Mac's own disk) when it
# exists - under Apple's httpd this process may not be allowed to read the stick itself.
DATA="$PROFILE"; [ -d "$RUN/mirror/MacDiag" ] && DATA="$RUN/mirror"
ROUTE="${1#/}"; QUERY="$2"
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

js() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
num() { case "$1" in ''|*[!0-9.-]*) printf '0' ;; *) printf '%s' "$1" ;; esac; }
qparam() {   # qparam name  -> URL-decoded value from $QUERY
  local v; v="$(printf '%s' "$QUERY" | tr '&' '\n' | grep "^$1=" | head -n1 | cut -d= -f2- | sed 's/+/ /g')"
  printf '%b' "$(printf '%s' "$v" | sed 's/%\([0-9A-Fa-f][0-9A-Fa-f]\)/\\x\1/g')"
}
plist() { /usr/libexec/PlistBuddy -c "Print :$2" "$1" 2>/dev/null; }
getf() { grep -m1 "$1" "$2" | sed 's/^[^:]*: *//'; }

# ---- routes -----------------------------------------------------------------
r_stats() { [ -s "$RUN/stats.json" ] && cat "$RUN/stats.json" || printf '{"ts":0,"waiting":true}'; }

r_history() {
  local n; n="$(qparam n)"; [ -n "$n" ] || n=180
  printf '{"ts":['; tail -n "$n" "$RUN/history.log" 2>/dev/null | awk '{ printf "%s%s", (NR>1?",":""), $1 }'
  printf '],"cpu":['; tail -n "$n" "$RUN/history.log" 2>/dev/null | awk '{ printf "%s%s", (NR>1?",":""), $2 }'
  printf '],"mem":['; tail -n "$n" "$RUN/history.log" 2>/dev/null | awk '{ printf "%s%s", (NR>1?",":""), $3 }'
  printf '],"gpu":['; tail -n "$n" "$RUN/history.log" 2>/dev/null | awk '{ printf "%s%s", (NR>1?",":""), $4 }'
  printf '],"net_in":['; tail -n "$n" "$RUN/history.log" 2>/dev/null | awk '{ printf "%s%s", (NR>1?",":""), $5 }'
  printf '],"net_out":['; tail -n "$n" "$RUN/history.log" 2>/dev/null | awk '{ printf "%s%s", (NR>1?",":""), $6 }'
  printf '],"disk":['; tail -n "$n" "$RUN/history.log" 2>/dev/null | awk '{ printf "%s%s", (NR>1?",":""), $7 }'
  printf ']}'
}

r_sysinfo() {
  local c="$RUN/sysinfo.json"
  if [ -s "$c" ]; then cat "$c"; return; fi
  local hw="$RUN/hw.txt"; system_profiler SPHardwareDataType 2>/dev/null > "$hw"
  local model mid chip mem serial board osv build act fv sip gk enr icloud gpu disp boot t2
  model="$(getf 'Model Name' "$hw")"; mid="$(getf 'Model Identifier' "$hw")"
  chip="$(getf 'Chip' "$hw")"; [ -n "$chip" ] || chip="$(getf 'Processor Name' "$hw")"
  mem="$(getf '^ *Memory' "$hw")"; serial="$(getf 'Serial Number' "$hw")"; act="$(getf 'Activation Lock' "$hw")"
  board="$(ioreg -l 2>/dev/null | awk -F'"' '/board-id/ { print $4; exit }')"
  osv="$(sw_vers -productVersion 2>/dev/null)"; build="$(sw_vers -buildVersion 2>/dev/null)"
  fv="$(fdesetup status 2>/dev/null)"; sip="$(csrutil status 2>/dev/null | head -n1)"; gk="$(spctl --status 2>/dev/null)"
  enr="$(profiles status -type enrollment 2>/dev/null | tr '\n' ';')"
  icloud="$(defaults read MobileMeAccounts Accounts 2>/dev/null | grep -m1 'AccountID' | sed 's/.*= "\{0,1\}\([^";]*\).*/\1/')"
  gpu="$(system_profiler SPDisplaysDataType 2>/dev/null | grep -m1 'Chipset Model' | sed 's/.*: *//')"
  disp="$(system_profiler SPDisplaysDataType 2>/dev/null | grep -m1 'Resolution' | sed 's/.*: *//')"
  boot="$(sysctl -n kern.boottime 2>/dev/null | sed 's/.*sec = \([0-9]*\).*/\1/')"; [ -n "$boot" ] && boot="$(date -r "$boot" '+%Y-%m-%d %H:%M' 2>/dev/null)"
  t2="$(system_profiler SPiBridgeDataType 2>/dev/null | grep -m1 'Model Name' | sed 's/.*: *//')"
  # The marketing name ("MacBook Pro (16-inch, 2021)"). Apple silicon publishes it
  # in the device tree, which needs no account - the old CPU-Names preference is a
  # logged-in user's file and is always empty at Setup Assistant. Intel Macs have
  # no product-name; the bench maps the identifier instead (tools/mac_models.py).
  local market; market="$(ioreg -rd1 -c IOPlatformExpertDevice 2>/dev/null | awk -F'"' '/"product-name"/{print $4; exit}')"
  [ -n "$market" ] || market="$(defaults read "$HOME/Library/Preferences/com.apple.SystemProfiler" 'CPU Names' 2>/dev/null | grep -o '= "[^"]*"' | head -n1 | sed 's/= "//; s/"$//')"
  local fmm=false; nvram -p 2>/dev/null | grep -q 'fmm-mobileme-token-FMM' && fmm=true
  local admins; admins="$(dscl . -read /Groups/admin GroupMembership 2>/dev/null | sed 's/GroupMembership: //')"
  local xp=""; for pl in /Library/Apple/System/Library/CoreServices/XProtect.bundle/Contents/Info.plist /System/Library/CoreServices/XProtect.bundle/Contents/Info.plist; do [ -f "$pl" ] && { xp="$(plist "$pl" CFBundleShortVersionString)"; break; }; done
  local tm; tm="$(tmutil destinationinfo 2>/dev/null | grep -m1 'Name' | sed 's/.*: *//')"
  local dns; dns="$(scutil --dns 2>/dev/null | grep 'nameserver\[' | awk '{ print $3 }' | sort -u | tr '\n' ' ')"
  local proxy=false; scutil --proxy 2>/dev/null | grep -q 'Enable : 1' && proxy=true
  local cores; cores="$(sysctl -n hw.ncpu 2>/dev/null)"
  local arch; arch="$(uname -m 2>/dev/null)"
  {
    printf '{"model":"%s","market":"%s","model_id":"%s","chip":"%s","cores":%s,"arch":"%s","memory":"%s","serial":"%s","board_id":"%s",' \
      "$(js "$model")" "$(js "$market")" "$(js "$mid")" "$(js "$chip")" "$(num "$cores")" "$(js "$arch")" "$(js "$mem")" "$(js "$serial")" "$(js "$board")"
    printf '"os":"%s","build":"%s","booted":"%s","t2":"%s","gpu":"%s","display":"%s",' "$(js "$osv")" "$(js "$build")" "$(js "$boot")" "$(js "$t2")" "$(js "$gpu")" "$(js "$disp")"
    printf '"activation_lock":"%s","find_my_token":%s,"enrollment":"%s","icloud":"%s","filevault":"%s","sip":"%s","gatekeeper":"%s","xprotect":"%s",' \
      "$(js "$act")" "$fmm" "$(js "$enr")" "$(js "$icloud")" "$(js "$fv")" "$(js "$sip")" "$(js "$gk")" "$(js "$xp")"
    printf '"admins":"%s","time_machine":"%s","dns":"%s","proxy":%s,"user":"%s","host":"%s"}' \
      "$(js "$admins")" "$(js "$tm")" "$(js "$dns")" "$proxy" "$(js "$USER")" "$(js "$(scutil --get ComputerName 2>/dev/null)")"
  } > "$c"
  cat "$c"
}

r_processes() {
  printf '['
  ps -Aro pid=,user=,%cpu=,%mem=,rss=,comm= 2>/dev/null | head -n 80 | awk '{
    pid=$1; u=$2; c=$3; m=$4; r=$5; $1=$2=$3=$4=$5=""; sub(/^ +/,""); p=$0; n=p; sub(/.*\//,"",n);
    gsub(/[^A-Za-z0-9 ._()+\/-]/,"",p); gsub(/[^A-Za-z0-9 ._()+-]/,"",n); gsub(/[^A-Za-z0-9._-]/,"",u);
    printf "%s{\"pid\":%d,\"user\":\"%s\",\"cpu\":%.1f,\"mem\":%.1f,\"rss\":%d,\"name\":\"%s\",\"path\":\"%s\"}", (NR>1?",":""), pid, u, c, m, r*1024, substr(n,1,48), substr(p,1,160) }'
  printf ']'
}

r_startup() {
  printf '['
  local first=1 d f lbl prog where ok li
  for d in /Library/LaunchAgents /Library/LaunchDaemons "$HOME/Library/LaunchAgents"; do
    [ -d "$d" ] || continue
    case "$d" in /Library/LaunchDaemons) where="system daemon";; /Library/LaunchAgents) where="system agent";; *) where="user agent";; esac
    for f in "$d"/*.plist; do
      [ -f "$f" ] || continue
      lbl="$(plist "$f" Label)"; [ -n "$lbl" ] || lbl="$(basename "$f" .plist)"
      case "$lbl" in com.apple.*) continue;; esac
      prog="$(plist "$f" Program)"; [ -n "$prog" ] || prog="$(plist "$f" ProgramArguments:0)"
      ok=true; [ -n "$prog" ] && [ ! -e "$prog" ] && ok=false
      [ "$first" = 1 ] || printf ','; first=0
      printf '{"label":"%s","where":"%s","program":"%s","exists":%s,"file":"%s"}' "$(js "$lbl")" "$where" "$(js "$prog")" "$ok" "$(js "$f")"
    done
  done
  li="$(osascript -e 'tell application "System Events" to get the path of every login item' 2>/dev/null | tr ',' '\n' | sed 's/^ *//')"
  if [ -n "$li" ]; then
    printf '%s\n' "$li" | while read -r p; do [ -n "$p" ] || continue; printf ',{"label":"%s","where":"login item","program":"%s","exists":%s,"file":""}' "$(js "$(basename "$p")")" "$(js "$p")" "$([ -e "$p" ] && printf true || printf false)"; done
  fi
  printf ']'
}

r_users() {
  printf '['
  local first=1 u uid home hidden admin last
  local admins; admins=" $(dscl . -read /Groups/admin GroupMembership 2>/dev/null | sed 's/GroupMembership: //') "
  dscl . -list /Users UniqueID 2>/dev/null | awk '$2 >= 500 || $1=="root" { print $1, $2 }' | while read -r u uid; do
    home="$(dscl . -read "/Users/$u" NFSHomeDirectory 2>/dev/null | sed 's/NFSHomeDirectory: //')"
    hidden=false; [ "$(dscl . -read "/Users/$u" IsHidden 2>/dev/null | awk '{ print $2 }')" = "1" ] && hidden=true
    admin=false; case "$admins" in *" $u "*) admin=true;; esac
    last="$(last -1 "$u" 2>/dev/null | head -n1 | awk '{ print $4, $5, $6, $7 }')"
    printf '%s{"name":"%s","uid":%s,"home":"%s","admin":%s,"hidden":%s,"last_login":"%s"}' "$([ "$first" = 1 ] || printf ',')" "$(js "$u")" "$(num "$uid")" "$(js "$home")" "$admin" "$hidden" "$(js "$last")"
    first=0
  done
  printf ']'
}

r_services() {
  printf '['
  launchctl list 2>/dev/null | awk 'NR>1 && $3 !~ /^com\.apple\./ {
    gsub(/[^A-Za-z0-9._-]/,"",$3); printf "%s{\"label\":\"%s\",\"pid\":\"%s\",\"status\":\"%s\",\"scope\":\"user\"}", (n++?",":""), $3, $1, $2 }'
  printf ']'
}

# smartctl summary written by collector.sh (key=value) -> JSON object, or null
smart_json() {
  local f="$1" kv
  [ -s "$f" ] || { printf 'null'; return 0; }
  kv="$(cat "$f")"
  g() { printf '%s\n' "$kv" | awk -F= -v k="$1" '$1==k { print substr($0, length(k)+2); exit }'; }
  local type health wear spare thr media unsafe tbw hours cycles temp realloc pending uncorrect crc crit failed err verdict
  type="$(g type)"; health="$(g health)"; wear="$(g wear)"; spare="$(g spare)"; thr="$(g spare_thr)"; media="$(g media_errors)"
  unsafe="$(g unsafe)"; tbw="$(g tbw)"; hours="$(g hours)"; cycles="$(g cycles)"; temp="$(g temp)"; realloc="$(g realloc)"
  pending="$(g pending)"; uncorrect="$(g uncorrect)"; crc="$(g crc)"; crit="$(g crit)"; failed="$(g failed)"; err="$(g error)"
  [ "$type" = "none" ] && { printf '{"type":"none","error":"%s"}' "$(js "$err")"; return 0; }
  verdict="fine"
  case "$(printf '%s' "$health" | tr 'A-Z' 'a-z')" in *fail*) verdict="replace" ;; esac
  [ -n "$failed" ] && verdict="replace"
  case "$crit" in ""|0x00|0x0) ;; *) verdict="replace" ;; esac
  [ -n "$media" ] && [ "$media" -gt 0 ] 2>/dev/null && verdict="replace"
  [ -n "$pending" ] && [ "$pending" -gt 0 ] 2>/dev/null && verdict="replace"
  [ -n "$uncorrect" ] && [ "$uncorrect" -gt 0 ] 2>/dev/null && verdict="replace"
  [ -n "$spare" ] && [ -n "$thr" ] && [ "$spare" -le "$thr" ] 2>/dev/null && verdict="replace"
  [ -n "$realloc" ] && [ "$realloc" -ge 50 ] 2>/dev/null && verdict="replace"
  [ -n "$wear" ] && [ "$wear" -ge 90 ] 2>/dev/null && verdict="replace"
  if [ "$verdict" = "fine" ]; then
    [ -n "$wear" ] && [ "$wear" -ge 70 ] 2>/dev/null && verdict="watch"
    [ -n "$realloc" ] && [ "$realloc" -gt 0 ] 2>/dev/null && verdict="watch"
    [ -n "$crc" ] && [ "$crc" -gt 0 ] 2>/dev/null && verdict="watch"
    [ -n "$spare" ] && [ "$spare" -lt 50 ] 2>/dev/null && [ "${thr:-0}" -lt 50 ] 2>/dev/null && verdict="watch"
  fi
  printf '{"type":"%s","health":"%s","verdict":"%s","wear":%s,"spare":%s,"spare_thr":%s,"media_errors":%s,"unsafe":%s,"tbw":%s,"hours":%s,"cycles":%s,"temp":%s,"realloc":%s,"pending":%s,"crc":%s,"crit":"%s","failed":"%s","ts":%s}' \
    "$(js "$type")" "$(js "$health")" "$verdict" "$(num "${wear:--1}")" "$(num "${spare:--1}")" "$(num "${thr:--1}")" "$(num "${media:--1}")" "$(num "${unsafe:--1}")" \
    "$(num "${tbw:--1}")" "$(num "${hours:--1}")" "$(num "${cycles:--1}")" "$(num "${temp:--1}")" "$(num "${realloc:--1}")" "$(num "${pending:--1}")" "$(num "${crc:--1}")" \
    "$(js "$crit")" "$(js "$failed")" "$(num "$(date -r "$f" +%s 2>/dev/null)")"
}

r_disks() {
  printf '['
  local first=1 d di nm sz proto ssd smart dl synmap=""
  # APFS volumes mount from a synthesized disk (disk3s1...) whose "Physical Store" is on the
  # real disk (disk0s2) - map synthesized -> physical so volumes show under the drive they live on.
  dl="$(diskutil list 2>/dev/null)"
  synmap="$(printf '%s\n' "$dl" | awk '/^\/dev\/disk[0-9]+ \(synthesized\)/ { s=$1; sub("/dev/","",s) } /Physical Store disk/ { p=$NF; sub(/s[0-9]+$/,"",p); if (s) printf "%s:%s ", s, p }')"
  for d in $(printf '%s\n' "$dl" | grep -E '^/dev/disk[0-9]+ \((internal|external)' | grep -o '^/dev/disk[0-9]*' | sort -u); do
    di="$(diskutil info "$d" 2>/dev/null)"
    nm="$(printf '%s' "$di" | grep -m1 'Device / Media Name' | sed 's/.*: *//')"
    sz="$(printf '%s' "$di" | grep -m1 'Disk Size' | sed 's/.*(\([0-9]*\) Bytes.*/\1/')"
    proto="$(printf '%s' "$di" | grep -m1 'Protocol' | sed 's/.*: *//')"
    ssd="$(printf '%s' "$di" | grep -m1 'Solid State' | sed 's/.*: *//')"
    smart="$(printf '%s' "$di" | grep -m1 'SMART Status' | sed 's/.*: *//')"
    local internal; internal="$(printf '%s' "$di" | grep -m1 'Device Location' | sed 's/.*: *//')"
    [ "$first" = 1 ] || printf ','; first=0
    printf '{"dev":"%s","name":"%s","size":%s,"protocol":"%s","ssd":"%s","smart":"%s","location":"%s",' "$(js "${d#/dev/}")" "$(js "$nm")" "$(num "$sz")" "$(js "$proto")" "$(js "$ssd")" "$(js "$smart")" "$(js "$internal")"
    printf '"health":%s,"volumes":[' "$(smart_json "$RUN/smart/${d#/dev/}.kv")"
    local pats="^$d(s[0-9]+)*$" m
    for m in $synmap; do [ "${m#*:}" = "${d#/dev/}" ] && pats="$pats|^/dev/${m%%:*}(s[0-9]+)*$"; done
    df -k 2>/dev/null | awk -v re="$pats" '$1 ~ re { mp=$9; for (i=10;i<=NF;i++) mp=mp" "$i; printf "%s{\"dev\":\"%s\",\"total\":%d,\"used\":%d,\"mount\":\"%s\"}", (n++?",":""), $1, $2*1024, $3*1024, mp }'
    printf ']}'
  done
  printf ']'
}

# ---- hardware tests: ports / charger snapshot ---------------------------------
# One call = the charger the Mac negotiated right now (+ what the battery is
# really drawing), every USB device and every Thunderbolt/USB4 port. The Tests
# page polls this while the tech moves the charger / a stick from port to port.
r_ports() {
  local pw bat conn watt name chg pct amp volt aw desc fam serial usb tb
  pw="$(system_profiler SPPowerDataType 2>/dev/null)"
  bat="$(ioreg -rn AppleSmartBattery 2>/dev/null)"
  conn="$(printf '%s\n' "$pw" | grep -A14 'AC Charger Information' | grep -m1 'Connected' | sed 's/.*: *//')"
  watt="$(printf '%s\n' "$pw" | grep -A14 'AC Charger Information' | grep -m1 'Wattage' | sed 's/.*: *//')"
  name="$(printf '%s\n' "$pw" | grep -A14 'AC Charger Information' | grep -m1 -E '^ *Name:' | sed 's/.*: *//')"
  fam="$(printf '%s\n' "$pw" | grep -A14 'AC Charger Information' | grep -m1 -E '^ *Family:' | sed 's/.*: *//')"
  serial="$(printf '%s\n' "$pw" | grep -A14 'AC Charger Information' | grep -m1 -E '^ *Serial Number:' | sed 's/.*: *//')"
  chg="$(printf '%s\n' "$pw" | grep -m1 -E '^ *Charging:' | sed 's/.*: *//')"
  pct="$(printf '%s\n' "$pw" | grep -m1 'State of Charge' | sed 's/.*: *//')"
  [ -n "$pct" ] || pct="$(pmset -g batt 2>/dev/null | grep -o '[0-9]*%' | head -n1 | tr -d '%')"
  amp="$(printf '%s\n' "$bat" | grep -m1 '"InstantAmperage"' | sed 's/.*= *//' | tr -d ' ')"
  volt="$(printf '%s\n' "$bat" | grep -m1 -E '^ *"Voltage"' | sed 's/.*= *//' | tr -d ' ')"
  aw="$(printf '%s\n' "$bat" | grep -m1 -o '"Watts"=[0-9]*' | cut -d= -f2)"
  desc="$(printf '%s\n' "$bat" | grep -m1 -o '"Description"="[^"]*"' | cut -d= -f2 | tr -d '"')"
  [ -n "$watt" ] || watt="$aw"
  # InstantAmperage is a 64-bit two's complement in ioreg: huge unsigned = negative (discharging)
  local bw; bw="$(awk -v a="${amp:-0}" -v v="${volt:-0}" 'BEGIN{ if (a > 4611686018427387904) a = a - 18446744073709551616; printf "%.1f", a*v/1000000 }')"
  usb="$(system_profiler SPUSBDataType 2>/dev/null | awk '
    function flush() { if (name != "" && pid != "") { printf "%s{\"name\":\"%s\",\"speed\":\"%s\",\"vendor\":\"%s\",\"loc\":\"%s\",\"ma\":%d,\"bus\":\"%s\"}", (n++?",":""), name, speed, vendor, loc, ma+0, bus }; name = ""; pid = ""; speed = ""; vendor = ""; loc = ""; ma = 0 }
    function js(s) { gsub(/\\/, "\\\\", s); gsub(/"/, "\\\"", s); gsub(/[\001-\037]/, "", s); return s }
    /^ {4}[^ ].*:$/ { flush(); bus = $0; sub(/^ */, "", bus); sub(/:$/, "", bus); cand = ""; next }
    /^ +[^:]+:$/ { c = $0; sub(/^ */, "", c); sub(/:$/, "", c); if (pid != "") flush(); cand = js(c); next }
    /^ +Product ID:/ { name = cand; pid = $NF; next }
    /^ +Speed:/ { s = $0; sub(/^[^:]*: */, "", s); speed = js(s); next }
    /^ +Manufacturer:/ { s = $0; sub(/^[^:]*: */, "", s); vendor = js(s); next }
    /^ +Location ID:/ { s = $0; sub(/^[^:]*: */, "", s); loc = js(s); next }
    /^ +Current Required \(mA\):/ { ma = $NF; next }
    END { flush() }')"
  tb="$(system_profiler SPThunderboltDataType 2>/dev/null | awk '
    function js(s) { gsub(/\\/, "\\\\", s); gsub(/"/, "\\\"", s); gsub(/[\001-\037]/, "", s); return s }
    function flushport() { if (inport) { printf "%s{\"bus\":\"%s\",\"receptacle\":\"%s\",\"status\":\"%s\",\"speed\":\"%s\",\"device\":\"%s\"}", (n++?",":""), bus, rec, st, spd, dev }; inport = 0; rec = ""; st = ""; spd = ""; dev = "" }
    /^ {4}[^ ].*Bus.*:$/ { flushport(); bus = $0; sub(/^ */, "", bus); sub(/:$/, "", bus); next }
    /^ +Port( \(Upstream\))?:$/ { flushport(); inport = 1; next }
    /^ +Status:/ && inport { s = $0; sub(/^[^:]*: */, "", s); st = js(s); next }
    /^ +Receptacle:/ && inport { rec = $NF; next }
    /^ +Speed:/ && inport { s = $0; sub(/^[^:]*: */, "", s); spd = js(s); next }
    /^ +Device Name:/ { s = $0; sub(/^[^:]*: */, "", s); if (inport) dev = js(s); else lastdev = js(s); next }
    END { flushport() }')"
  printf '{"ts":%s,"charger":{"connected":%s,"watts":%s,"name":"%s","family":"%s","serial":"%s","desc":"%s","charging":"%s","pct":%s,"battery_w":%s},"usb":[%s],"thunderbolt":[%s]}' \
    "$(date +%s)" "$([ "$conn" = "Yes" ] && printf true || printf false)" "$(num "${watt:-0}")" "$(js "$name")" "$(js "$fam")" "$(js "$serial")" "$(js "$desc")" "$(js "$chg")" "$(num "${pct:-0}")" "$(num "$bw")" "$usb" "$tb"
}

# ---- QC checklist save (POST body = text, a separator line, then JSON) ------
# Written into $RUN/qc (this process may not be allowed to touch the stick);
# collector.sh copies it to Dashboard/QC-Reports/ on the stick.
r_qcsave() {
  local body="${BM_POST:-}" host ts base txt json sep='=====BENCHMAC-JSON====='
  [ -n "$body" ] && [ -s "$body" ] || { printf '{"ok":false,"error":"empty body"}'; return; }
  host="$(scutil --get ComputerName 2>/dev/null)"; [ -n "$host" ] || host="$(hostname -s 2>/dev/null)"; [ -n "$host" ] || host="mac"
  host="$(printf '%s' "$host" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-40)"
  ts="$(date +%Y%m%d-%H%M%S)"; mkdir -p "$RUN/qc" 2>/dev/null
  base="$RUN/qc/QC_${host}_${ts}"; txt="$base.txt"; json="$base.json"
  awk -v sep="$sep" '$0 == sep { exit } { print }' "$body" > "$txt"
  awk -v sep="$sep" 'f { print } $0 == sep { f = 1 }' "$body" > "$json"
  rm -f "$body" 2>/dev/null
  if [ -s "$txt" ]; then printf '{"ok":true,"file":"%s"}' "$(js "$(basename "$txt")")"; else rm -f "$txt" "$json"; printf '{"ok":false,"error":"no text"}'; fi
}

# ---- job queue (Clean page) ---------------------------------------------------
# The web server may not touch ~/Library, so the page QUEUES work and collector.sh
# (a child of the tech's Terminal, with the sudo ticket) executes it:
#   POST /api/job      body {"type":"cleanup_scan"|"cleanup_clean"|"cleanup_maint", ...}  -> {"ok":true,"id":..}
#   GET  /api/jobstatus?id=..   -> {"state":"queued|running|done","log":"tail","result":{...}|null}
#   GET  /api/cleanlast         -> the last scan JSON (so a reload keeps the table)
r_job() {
  local body="${BM_POST:-}" type id
  [ -n "$body" ] && [ -s "$body" ] || { printf '{"ok":false,"error":"empty body"}'; return; }
  type="$(grep -o '"type" *: *"[a-z_]*"' "$body" | head -n1 | sed 's/.*"\([a-z_]*\)"$/\1/')"
  case "$type" in cleanup_scan|cleanup_clean|cleanup_maint|tool_run|files_index|files_backup|files_trash|files_delete|stress) ;; *) rm -f "$body"; printf '{"ok":false,"error":"unknown job type"}'; return;; esac
  if [ "$type" = "tool_run" ]; then
    local app; app="$(grep -o '"app" *: *"[A-Za-z]*"' "$body" | head -n1 | sed 's/.*"\([A-Za-z]*\)"$/\1/')"
    case "$app" in MacDiag|MalwareTriage|AppleDiagnostics|CleanUp) ;; *) rm -f "$body"; printf '{"ok":false,"error":"tool not allowed here (needs a Terminal)"}'; return;; esac
  fi
  mkdir -p "$RUN/jobs" 2>/dev/null
  id="$(date +%Y%m%d-%H%M%S)-$$"
  mv -f "$body" "$RUN/jobs/$id.job" 2>/dev/null || { printf '{"ok":false,"error":"queue write failed"}'; return; }
  printf '{"ok":true,"id":"%s","type":"%s"}' "$id" "$type"
}
r_jobstatus() {
  local id state="unknown" res="null" log=""; id="$(qparam id)"
  case "$id" in *..*|*/*|"") printf '{"state":"bad"}'; return;; esac
  local j="$RUN/jobs/$id"
  if   [ -f "$j.result.json" ]; then state="done"; res="$(cat "$j.result.json")"
  elif [ -f "$j.running" ];     then state="running"
  elif [ -f "$j.job" ];         then state="queued"
  elif [ -f "$j.done" ];        then state="done"; fi
  [ -f "$j.log" ] && log="$(tail -n 40 "$j.log" | tr -d '\r')"
  [ -n "$res" ] && [ "$res" != "null" ] && printf '%s' "$res" | grep -q '^{' || res="null"
  printf '{"state":"%s","log":"%s","result":%s}' "$state" "$(js "$log")" "$res"
}
r_cleanlast() { [ -s "$RUN/clean/last_scan.json" ] && cat "$RUN/clean/last_scan.json" || printf '{"ts":0}'; }

# ---- Files page: reads the index files.sh built (never the disk itself) --------
FIDX="$RUN/files"
fmeta() { awk -F= -v k="$1" '$1==k { print substr($0, length(k)+2); exit }' "$FIDX/meta.txt" 2>/dev/null; }
r_files_meta() {
  [ -s "$FIDX/meta.txt" ] || { printf '{"indexed":false}'; return; }
  printf '{"indexed":true,"stale":%s,"ts":%s,"home":"%s","host":"%s","total":%s,"used":%s,"sudo":%s,"dirs":%s,"files":%s,"volume":"%s","root":"%s"}' \
    "$([ "$(fmeta stale)" = 1 ] && printf true || printf false)" "$(num "$(fmeta ts)")" "$(js "$(fmeta home)")" "$(js "$(fmeta host)")" "$(num "$(fmeta total)")" "$(num "$(fmeta used)")" "$([ "$(fmeta sudo)" = 1 ] && printf true || printf false)" "$(num "$(fmeta dirs)")" "$(num "$(fmeta files)")" "$(js "$(fmeta volume)")" "$(js "$(fmeta root)")"
}
# children of a folder from the index: sub-folders (with sizes) + files >= 1 MB directly inside it
r_files_ls() {
  local p own; p="$(qparam p)"; [ -n "$p" ] || p="/"; p="${p%/}"; [ -n "$p" ] || p="/"
  case "$p" in *..*) printf '{"error":"bad path"}'; return;; esac
  [ -s "$FIDX/dirs.tsv" ] || { printf '{"error":"not indexed"}'; return; }
  own="$(awk -F'\t' -v p="$p" '$2==p { print $1 * 1024; exit }' "$FIDX/dirs.tsv")"
  printf '{"path":"%s","bytes":%s,"dirs":[' "$(js "$p")" "$(num "${own:-0}")"
  local pre="$p/"; [ "$p" = "/" ] && pre="/"
  awk -F'\t' -v pre="$pre" 'index($2, pre)==1 { r=substr($2, length(pre)+1); if (r != "" && index(r, "/")==0) printf "%s\t%s\n", $1*1024, r }' "$FIDX/dirs.tsv" \
    | sort -t"$(printf '\t')" -k1,1rn | head -n 400 | awk -F'\t' 'function js(s){ gsub(/\\/,"\\\\",s); gsub(/"/,"\\\"",s); return s } { printf "%s{\"n\":\"%s\",\"bytes\":%s}", (NR>1?",":""), js($2), $1 }'
  printf '],"files":['
  awk -F'\t' -v pre="$pre" 'index($3, pre)==1 { r=substr($3, length(pre)+1); if (r != "" && index(r, "/")==0) printf "%s\t%s\t%s\n", $1, $2, r }' "$FIDX/files.tsv" 2>/dev/null \
    | sort -t"$(printf '\t')" -k1,1rn | head -n 300 | awk -F'\t' 'function js(s){ gsub(/\\/,"\\\\",s); gsub(/"/,"\\\"",s); return s } { printf "%s{\"n\":\"%s\",\"bytes\":%s,\"mtime\":%s}", (NR>1?",":""), js($3), $1, $2 }'
  printf ']}'
}
# search the large-file index: q = name substring (case-insensitive), type = photo|video|audio|doc|archive|installer|image|all, min = MB
r_files_find() {
  local q t min re; q="$(qparam q)"; t="$(qparam type)"; min="$(qparam min)"; [ -n "$min" ] || min=0
  [ -s "$FIDX/files.tsv" ] || { printf '{"error":"not indexed","items":[]}'; return; }
  case "$t" in
    photo) re='\.(jpe?g|heic|heif|png|gif|tiff?|raw|cr2|cr3|nef|arw|dng|bmp|webp|psd)$' ;;
    video) re='\.(mov|mp4|m4v|mkv|avi|mts|m2ts|wmv|flv|webm|3gp|mpg|mpeg)$' ;;
    audio) re='\.(mp3|m4a|aac|wav|aiff?|flac|ogg|wma|m4b|alac)$' ;;
    doc)   re='\.(pdf|docx?|xlsx?|pptx?|pages|numbers|key|txt|rtf|csv|md|epub|odt)$' ;;
    archive) re='\.(zip|rar|7z|tar|gz|tgz|bz2|xz|sit|sitx)$' ;;
    installer) re='\.(dmg|pkg|mpkg|iso|app|xip)$' ;;
    image) re='\.(dmg|iso|img|sparseimage|sparsebundle|vmdk|vdi|qcow2|ova)$' ;;
    *) re='' ;;
  esac
  printf '{"items":['
  awk -F'\t' -v q="$(printf '%s' "$q" | tr 'A-Z' 'a-z')" -v re="$re" -v minb="$(num "$min")" '
    { n=$3; sub(/.*\//, "", n); ln=tolower(n)
      if (q != "" && index(ln, q)==0) next
      if (re != "" && ln !~ re) next
      if ($1 < minb*1048576) next
      print }' "$FIDX/files.tsv" | sort -t"$(printf '\t')" -k1,1rn | head -n 300 \
    | awk -F'\t' 'function js(s){ gsub(/\\/,"\\\\",s); gsub(/"/,"\\\"",s); return s } { printf "%s{\"p\":\"%s\",\"bytes\":%s,\"mtime\":%s}", (NR>1?",":""), js($3), $1, $2 }'
  printf ']}'
}
# the customer-data map: the folders a backup quote is made of, with sizes from the index
r_files_map() {
  local home rel p b first=1
  home="$(fmeta home)"; [ -n "$home" ] || { printf '[]'; return; }
  printf '['
  for rel in Desktop Documents Downloads Pictures "Pictures/Photos Library.photoslibrary" Movies Music "Music/Music" Library/Mail "Library/Application Support/MobileSync/Backup" "Library/Messages" "Library/Mobile Documents" Public; do
    p="$home/$rel"; b="$(awk -F'\t' -v p="$p" '$2==p { print $1 * 1024; exit }' "$FIDX/dirs.tsv" 2>/dev/null)"
    [ -n "$b" ] || continue
    [ "$first" = 1 ] || printf ','; first=0
    printf '{"label":"%s","p":"%s","bytes":%s}' "$(js "$(case "$rel" in Pictures/Photos*) printf 'Photos library';; Library/Mail) printf 'Mail';; Library/Application*) printf 'iPhone / iPad backups';; Library/Messages) printf 'Messages';; Library/Mobile*) printf 'iCloud Drive (local copy)';; Music/Music) printf 'Music library';; *) printf '%s' "$rel";; esac)")" "$(js "$p")" "$b"
  done
  printf ']'
}
# mounted volumes a backup can go to (df works without touching the volumes)
r_files_dests() {
  printf '['
  df -k 2>/dev/null | awk 'function js(s){ gsub(/\\/,"\\\\",s); gsub(/"/,"\\\"",s); return s } NR==1 { mi=9; for (i=1;i<=NF;i++) if ($i=="Mounted") mi=i; next } { mp=$mi; for (i=mi+1;i<=NF;i++) mp=mp" "$i; if (mp ~ /^\/Volumes\// && mp !~ /^\/Volumes\/(Recovery|Preboot|VM|Update|Hardware|xarts|iSCPreboot|Data)$/) { n=mp; sub(/^\/Volumes\//, "", n); printf "%s{\"p\":\"%s\",\"name\":\"%s\",\"total\":%d,\"free\":%d,\"dev\":\"%s\"}", (c++?",":""), js(mp), js(n), $2*1024, $4*1024, js($1) } }'
  printf ']'
}

r_reports() {
  printf '['
  local first=1 app f base ts verdict json
  for app in MacDiag MalwareTriage AppleDiagnostics CleanUp Files QC; do
    for f in "$DATA/$app/$app-Reports"/*.txt "$RUN/qc"/QC_*.txt; do
      [ -f "$f" ] || continue
      case "$f" in "$RUN/qc/"*) [ "$app" = "QC" ] || continue ;; *) [ "$app" = "QC" ] && continue ;; esac
      base="$(basename "$f")"; json="${f%.txt}.json"; verdict=""
      [ -f "$json" ] && verdict="$(grep -m1 '"verdict"' "$json" | sed 's/.*"verdict": *"\([^"]*\)".*/\1/')"
      [ -n "$verdict" ] || verdict="$(grep -m1 -E '^(VERDICT|RESULT)' "$f" | sed 's/^[A-Z]* *: *//; s/ *(HIGH.*//')"
      ts="$(date -r "$f" +%s 2>/dev/null)"
      [ "$first" = 1 ] || printf ','; first=0
      printf '{"app":"%s","file":"%s","ts":%s,"verdict":"%s","size":%s}' "$app" "$(js "$base")" "$(num "$ts")" "$(js "$verdict")" "$(num "$(wc -c < "$f" | tr -d ' ')")"
    done
  done
  printf ']'
}

r_report() {   # text
  local f; f="$(qparam f)"; f="$(basename "$f")"
  local app="${f%%_*}"
  local p="$DATA/$app/$app-Reports/$f"
  [ "$app" = "QC" ] && p="$RUN/qc/$f"
  case "$f" in *..*|*/*|"") printf 'bad request'; return;; esac
  [ -f "$p" ] && cat "$p" || printf 'report not found'
}

r_log() {   # text
  local app; app="$(qparam app)"
  case "$app" in MacDiag|MalwareTriage|AppleDiagnostics|InstallerMaker|CleanUp|Files) ;; *) printf 'unknown app'; return;; esac
  local l="$DATA/$app/$app-last-run.log"
  if [ -f "$l" ]; then tail -n 80 "$l" | sed 's/\x1b\[[0-9;]*m//g' | tr -d '\r'; [ -s "$l" ] || printf '(log is empty)'; else printf '(no run yet)'; fi
}

r_run() {
  local app; app="$(qparam app)"
  case "$app" in MacDiag|MalwareTriage|AppleDiagnostics|InstallerMaker|CleanUp) ;; *) printf '{"ok":false,"error":"unknown app"}'; return;; esac
  local cmd="$PROFILE/$app/Run-$app.command"
  if [ -f "$cmd" ]; then
    # A new Terminal window runs the tool (it needs a real tty for sudo + prompts)
    if open -a Terminal "$cmd" >/dev/null 2>&1; then printf '{"ok":true,"app":"%s"}' "$app"; else printf '{"ok":false,"error":"open failed"}'; fi
  else printf '{"ok":false,"error":"missing"}'; fi
}

r_quit() { rm -f "$RUN/run" 2>/dev/null; printf '{"ok":true}'; }
r_status() {   # what the page is talking to - shown in the sidebar footer
  local n synced
  n="$(find "$DATA" -maxdepth 3 -name '*.txt' -path '*-Reports*' 2>/dev/null | wc -l | tr -d ' ')"
  synced="$(cat "$RUN/mirror/.synced" 2>/dev/null)"
  local busy=false; ls "$RUN/jobs"/*.running >/dev/null 2>&1 && busy=true
  local setup=false; [ "$(cat "$RUN/setup" 2>/dev/null)" = "1" ] && setup=true
  printf '{"version":"1.7","data":"%s","mirror":%s,"reports":%s,"synced":%s,"profile":"%s","sudo":%s,"busy":%s,"setup":%s,"bench":"%s","user":"%s"}' "$(js "$DATA")" "$([ "$DATA" = "$RUN/mirror" ] && printf true || printf false)" "$(num "$n")" "$(num "${synced:-0}")" "$(js "$PROFILE")" "$([ "$(cat "$RUN/have_sudo" 2>/dev/null)" = "1" ] && printf true || printf false)" "$busy" "$setup" "$(js "$(cat "$RUN/bench.url" 2>/dev/null)")" "$(js "$(id -un 2>/dev/null)")"
}

# ---- battery / intake / bench (setup mode: a Mac at Setup Assistant, no admin) ----
# ioreg's battery object also carries an INLINE "BatteryData" dict that repeats the same
# keys with no spaces ("DesignCapacity"=4382) - anchoring on '"Key" = ' skips it (same trick
# as the dongle's Battery Health preset). All unprivileged; works as _mbsetupuser.
r_battery() {
  local io; io="$(LC_ALL=C ioreg -rn AppleSmartBattery -w0 2>/dev/null)"
  bk() { printf '%s\n' "$io" | grep -m1 "\"$1\" = " | sed 's/.*" = //; s/^"//; s/"$//; s/[[:space:]]*$//'; }
  local design max cyc temp pfs bser mfg ext chg watts adesc pct cur volt amp
  design="$(num "$(bk DesignCapacity)")"; max="$(num "$(bk AppleRawMaxCapacity)")"; [ "$max" = "0" ] && max="$(num "$(bk NominalChargeCapacity)")"
  cyc="$(bk CycleCount)"; [ -n "$cyc" ] || cyc=-1; cyc="$(num "$cyc")"
  temp="$(num "$(bk Temperature)")"; pfs="$(num "$(bk PermanentFailureStatus)")"
  bser="$(bk BatterySerialNumber)"; [ -n "$bser" ] || bser="$(bk Serial)"
  mfg="$(num "$(bk ManufactureDate)")"
  ext="$(bk ExternalConnected)"; chg="$(bk IsCharging)"
  cur="$(num "$(bk AppleRawCurrentCapacity)")"; volt="$(num "$(bk Voltage)")"; amp="$(num "$(bk InstantAmperage)")"
  watts="$(printf '%s' "$io" | grep -o '"Watts"=[0-9]*' | head -n1 | sed 's/.*=//')"; watts="$(num "$watts")"
  adesc="$(printf '%s' "$io" | grep -o '"Description"="[^"]*"' | head -n1 | sed 's/.*="//; s/"$//')"
  pct=0; [ "$design" -gt 0 ] 2>/dev/null && [ "$max" -gt 0 ] 2>/dev/null && pct=$((100 * max / design))
  # ManufactureDate packs day (5 bits) / month (4) / year-1980 (7)
  local mdate=""; if [ "$mfg" -gt 0 ] 2>/dev/null; then mdate="$(( (mfg >> 9) + 1980 ))-$(printf '%02d' $(( (mfg >> 5) & 15 )))-$(printf '%02d' $(( mfg & 31 )))"; fi
  # temperature is hundredths of a degree C
  local tc=""; [ "$temp" -gt 0 ] 2>/dev/null && tc="$(awk -v t="$temp" 'BEGIN { printf "%.1f", t / 100 }')"
  local pm; pm="$(pmset -g batt 2>/dev/null | grep -m1 -o '[0-9]*%.*' | sed 's/;.*$//')"
  printf '{"present":%s,"design":%s,"max":%s,"pct":%s,"cycles":%s,"temp_c":%s,"pfs":%s,"serial":"%s","mfg":"%s","external":%s,"charging":%s,"adapter_w":%s,"adapter":"%s","current":%s,"voltage_mv":%s,"amperage_ma":%s,"pmset":"%s"}' \
    "$([ "$design" -gt 0 ] 2>/dev/null && printf true || printf false)" "$design" "$max" "$pct" "$cyc" "${tc:-0}" "$pfs" "$(js "$bser")" "$(js "$mdate")" \
    "$([ "$ext" = "Yes" ] && printf true || printf false)" "$([ "$chg" = "Yes" ] && printf true || printf false)" "$watts" "$(js "$adesc")" "$cur" "$volt" "$amp" "$(js "$pm")"
}

r_intake() {
  local di size medium smart name
  di="$(diskutil info disk0 2>/dev/null)"
  size="$(printf '%s' "$di" | grep -m1 'Disk Size' | sed 's/.*: *//; s/ (.*//')"
  medium="$(printf '%s' "$di" | grep -m1 'Solid State' | sed 's/.*: *//')"
  smart="$(printf '%s' "$di" | grep -m1 'SMART Status' | sed 's/.*: *//')"
  name="$(printf '%s' "$di" | grep -m1 'Media Name' | sed 's/.*: *//')"
  printf '{"sysinfo":'; r_sysinfo
  printf ',"battery":'; r_battery
  printf ',"disk":{"size":"%s","ssd":%s,"smart":"%s","name":"%s"}' "$(js "$size")" "$([ "$medium" = "Yes" ] && printf true || printf false)" "$(js "$smart")" "$(js "$name")"
  # Touch ID: the sensor's driver node exists whether or not a finger is enrolled (nothing is, at Setup Assistant)
  local tid=false bio; ioreg -rc AppleBiometricSensor 2>/dev/null | grep -q 'AppleBiometricSensor' && tid=true
  [ "$tid" = true ] || { ioreg -rc AppleBiometricServices 2>/dev/null | grep -q 'AppleBiometric' && tid=true; }
  bio="$(bioutil -r 2>/dev/null | grep -m1 -i 'functionality' | sed 's/^ *//')"
  printf ',"touchid":{"sensor":%s,"bioutil":"%s"}' "$tid" "$(js "$bio")"
  printf ',"setup":%s,"bench":"%s","ts":%s}' "$([ "$(cat "$RUN/setup" 2>/dev/null)" = "1" ] && printf true || printf false)" "$(js "$(cat "$RUN/bench.url" 2>/dev/null)")" "$(date +%s)"
}

r_bench() {   # POST body (JSON from the Intake page) -> <bench>/report ; the reply is passed back
  local url; url="$(cat "$RUN/bench.url" 2>/dev/null)"
  [ -n "$url" ] || { printf '{"ok":false,"error":"no bench URL - start the dashboard with -Bench http://<bench>:8377/x/<app>"}'; return; }
  [ -n "$BM_POST" ] && [ -s "$BM_POST" ] || { printf '{"ok":false,"error":"empty report"}'; return; }
  local out code; out="$RUN/bench.reply"
  code="$(curl -sS -m 20 -o "$out" -w '%{http_code}' -X POST -H 'Content-Type: application/json' --data-binary @"$BM_POST" "${url%/}/report" 2>"$RUN/bench.err")"
  cp "$BM_POST" "$RUN/bench.last.json" 2>/dev/null
  case "$code" in
    2*) if head -c1 "$out" 2>/dev/null | grep -q '{'; then printf '{"ok":true,"status":%s,"reply":' "$code"; cat "$out"; printf '}'; else printf '{"ok":true,"status":%s,"reply":"%s"}' "$code" "$(js "$(head -c 300 "$out")")"; fi ;;
    *)  printf '{"ok":false,"status":"%s","error":"%s"}' "$(js "$code")" "$(js "$(head -c 300 "$out" 2>/dev/null; head -c 200 "$RUN/bench.err" 2>/dev/null)")" ;;
  esac
}

case "$ROUTE" in
  stats)     r_stats ;;     history)   r_history ;;  sysinfo) r_sysinfo ;;
  processes) r_processes ;; startup)   r_startup ;;  users)   r_users ;;
  services)  r_services ;;  disks)     r_disks ;;    reports) r_reports ;;
  report)    r_report ;;    log)       r_log ;;      run)     r_run ;;
  quit)      r_quit ;;      status)    r_status ;;   ports)   r_ports ;;
  qcsave)    r_qcsave ;;    job)       r_job ;;      jobstatus) r_jobstatus ;;
  cleanlast) r_cleanlast ;; files_meta) r_files_meta ;; files_ls) r_files_ls ;;
  files_find) r_files_find ;; files_map) r_files_map ;; files_dests) r_files_dests ;;
  battery)   r_battery ;;   intake)    r_intake ;;    bench)   r_bench ;;
  *) printf '{"error":"no such route"}' ;;
esac
