#!/bin/bash
# collector.sh - samples the Mac every INTERVAL seconds and writes
#   $RUN/stats.json    the live snapshot the dashboard polls
#   $RUN/history.log   one line per tick (ts cpu mem gpu net_in net_out disk_rw) for the graphs
# Runs in the background under Run-Dashboard.command. bash 3.2, built-ins only.
#   collector.sh <RUN dir> [interval seconds] [sudo:0|1] [profile dir (Tools/)]
RUN="${1:?run dir}"; INTERVAL="${2:-2}"; HAVE_SUDO="${3:-0}"; PROFILE="${4:-}"
# The web server (Apple httpd) is a separate process that macOS may keep away
# from the removable volume, so THIS process (a child of the tech's Terminal,
# which has access) mirrors the tools' logs + reports into the run dir for it.
MIRROR="$RUN/mirror"; mkdir -p "$MIRROR" 2>/dev/null
# Reports + logs are picked up from the tool folders next to this Dashboard
# (Tools/<app>/) AND from the stick root (<app>/ - the pre-Tools layout, or a
# tool copied there by hand), so nothing a tech ran is missed.
STICK_ROOT=""; [ -f "$PROFILE/../Start Dashboard.command" ] && STICK_ROOT="$(cd "$PROFILE/.." && pwd)"
mirror_tools() {
  local app d src
  [ -n "$PROFILE" ] && [ -d "$PROFILE" ] || return 0
  for app in MacDiag MalwareTriage AppleDiagnostics InstallerMaker CleanUp Files; do
    mkdir -p "$MIRROR/$app/$app-Reports" 2>/dev/null
    for d in "$PROFILE/$app" "$STICK_ROOT/$app"; do
      [ -n "$d" ] && [ -d "$d" ] || continue
      src="$d/$app-last-run.log"
      [ -f "$src" ] && [ "$src" -nt "$MIRROR/$app/$app-last-run.log" ] && cp -fp "$src" "$MIRROR/$app/$app-last-run.log" 2>/dev/null
      if [ "$1" = "full" ] && [ -d "$d/$app-Reports" ]; then
        cp -fp "$d/$app-Reports"/*.txt "$d/$app-Reports"/*.json "$MIRROR/$app/$app-Reports/" 2>/dev/null
      fi
    done
  done
  [ "$1" = "full" ] && date +%s > "$MIRROR/.synced" 2>/dev/null
  return 0
}
mirror_tools full
# ---- QC checklists (Tests page): api.sh can only write into $RUN/qc; copy them to the
# stick (Dashboard/QC-Reports/) and, at start, pull older ones back so Reports lists them.
QC_RUN="$RUN/qc"; QC_STICK="$PROFILE/Dashboard/QC-Reports"; mkdir -p "$QC_RUN" 2>/dev/null
mirror_qc() {
  local f d
  [ -n "$PROFILE" ] && [ -d "$PROFILE/Dashboard" ] || return 0
  mkdir -p "$QC_STICK" 2>/dev/null
  for f in "$QC_RUN"/QC_*.txt "$QC_RUN"/QC_*.json; do
    [ -f "$f" ] || continue; d="$QC_STICK/$(basename "$f")"
    { [ ! -f "$d" ] || [ "$f" -nt "$d" ]; } && cp -fp "$f" "$d" 2>/dev/null
  done
  return 0
}
for f in "$QC_STICK"/QC_*.txt "$QC_STICK"/QC_*.json; do [ -f "$f" ] && [ ! -f "$QC_RUN/$(basename "$f")" ] && cp -fp "$f" "$QC_RUN/" 2>/dev/null; done
# ---- job runner: the Clean page queues work here; we have the Terminal's rights + sudo --
# $RUN/jobs/<id>.job -> .running (claimed) -> .result.json + .done ; progress in <id>.log
mkdir -p "$RUN/jobs" "$RUN/clean" 2>/dev/null
run_job() {
  local jf="$1" id type op app args rc; id="$(basename "$jf" .job)"
  local log="$RUN/jobs/$id.log" res="$RUN/jobs/$id.result.json" eng="$PROFILE/CleanUp/cleanup.sh"
  mv -f "$jf" "$jf.running" 2>/dev/null || return 0
  type="$(grep -o '"type" *: *"[a-z_]*"' "$jf.running" | head -n1 | sed 's/.*"\([a-z_]*\)"$/\1/')"
  (
    case "$type" in
      cleanup_scan)  bash "$eng" scan "$res" "$HAVE_SUDO" "$log" >>"$log" 2>&1; [ -s "$res" ] && cp -f "$res" "$RUN/clean/last_scan.json" ;;
      cleanup_clean) bash "$eng" clean "$jf.running" "$res" "$HAVE_SUDO" "$log" >>"$log" 2>&1; rm -f "$RUN/clean/last_scan.json" ;;
      cleanup_maint) op="$(grep -o '"op" *: *"[a-z]*"' "$jf.running" | head -n1 | sed 's/.*"\([a-z]*\)"$/\1/')"
                     if bash "$eng" maint "$op" "$HAVE_SUDO" "$log" >>"$log" 2>&1; then printf '{"ok":true,"op":"%s"}' "$op" > "$res"; else printf '{"ok":false,"op":"%s"}' "$op" > "$res"; fi ;;
      tool_run)      # a stick tool in quiet mode: no prompts, sudo ticket inherited from the dashboard, output -> job log
                     app="$(grep -o '"app" *: *"[A-Za-z]*"' "$jf.running" | head -n1 | sed 's/.*"\([A-Za-z]*\)"$/\1/')"
                     args="$(grep -o '"args" *: *"[^"]*"' "$jf.running" | head -n1 | sed 's/.*: *"//; s/"$//' | tr -c 'A-Za-z0-9 -' ' ')"
                     case "$app" in
                       MacDiag|MalwareTriage|AppleDiagnostics|CleanUp)
                         if [ -f "$PROFILE/$app/Run-$app.command" ]; then
                           ( cd "$PROFILE/$app" && bash "./Run-$app.command" -Quiet $args ) >>"$log" 2>&1 </dev/null; rc=$?
                           printf '{"ok":%s,"app":"%s","rc":%s}' "$([ "$rc" = 0 ] && printf true || printf false)" "$app" "$rc" > "$res"
                         else printf '{"ok":false,"error":"missing"}' > "$res"; fi ;;
                       *) printf '{"ok":false,"error":"tool not allowed"}' > "$res" ;;
                     esac ;;
      files_index)   mkdir -p "$RUN/files" 2>/dev/null; bash "$PROFILE/Files/files.sh" index "$RUN/files" "$HAVE_SUDO" "$log" >>"$log" 2>&1
                     [ -s "$RUN/files/meta.txt" ] && printf '{"ok":true}' > "$res" || printf '{"ok":false,"error":"index failed"}' > "$res" ;;
      files_backup)  bash "$PROFILE/Files/files.sh" backup "$jf.running" "$res" "$HAVE_SUDO" "$log" >>"$log" 2>&1 ;;
      files_trash)   bash "$PROFILE/Files/files.sh" trash "$jf.running" "$res" "$HAVE_SUDO" "$log" >>"$log" 2>&1; bash "$PROFILE/Files/files.sh" prune "$RUN/files" "$res.removed"; rm -f "$res.removed" ;;
      files_delete)  bash "$PROFILE/Files/files.sh" delete "$jf.running" "$res" "$HAVE_SUDO" "$log" >>"$log" 2>&1; bash "$PROFILE/Files/files.sh" prune "$RUN/files" "$res.removed"; rm -f "$res.removed" ;;
      stress)        # fan / thermal load test (no admin needed): every core busy for N s, thermal state before/after
                     secs="$(grep -o '"secs" *: *[0-9]*' "$jf.running" | head -n1 | sed 's/.*: *//')"; [ "$secs" -ge 10 ] 2>/dev/null || secs=60; [ "$secs" -le 180 ] 2>/dev/null || secs=180
                     ncpu="$(sysctl -n hw.ncpu 2>/dev/null)"; [ "$ncpu" -ge 1 ] 2>/dev/null || ncpu=4
                     therm_now() { local l t; l="$(pmset -g therm 2>/dev/null | grep -m1 'CPU_Speed_Limit' | sed 's/.*= *//' | tr -d ' ')"; t="$(ioreg -rn AppleSmartBattery -w0 2>/dev/null | grep -m1 '"Temperature" = ' | sed 's/.*= //')"; printf '%s %s' "${l:-100}" "${t:-0}"; }
                     set -- $(therm_now); lim0="$1"; bt0="$2"
                     printf 'load test: %s cores busy for %s s (CPU speed limit %s%%, battery %s.%s C)\n' "$ncpu" "$secs" "$lim0" "$((bt0/100))" "$(printf '%02d' $((bt0%100)))" >>"$log"
                     pids=""; n=0; while [ "$n" -lt "$ncpu" ]; do yes >/dev/null 2>&1 & pids="$pids $!"; n=$((n+1)); done
                     el=0; while [ "$el" -lt "$secs" ] && [ -f "$RUN/run" ]; do sleep 5; el=$((el+5)); set -- $(therm_now); printf '  %3ss  speed limit %s%%  battery %s.%s C\n' "$el" "$1" "$(($2/100))" "$(printf '%02d' $(($2%100)))" >>"$log"; done
                     kill $pids 2>/dev/null; wait $pids 2>/dev/null
                     set -- $(therm_now); lim1="$1"; bt1="$2"
                     thr=false; [ "$lim1" -lt 100 ] 2>/dev/null && thr=true
                     printf '{"ok":true,"secs":%s,"ncpu":%s,"cpu_limit_before":%s,"cpu_limit_after":%s,"batt_temp_before":%s,"batt_temp_after":%s,"throttled":%s}' "$secs" "$ncpu" "${lim0:-100}" "${lim1:-100}" "$(awk -v t="$bt0" 'BEGIN{printf "%.1f", t/100}')" "$(awk -v t="$bt1" 'BEGIN{printf "%.1f", t/100}')" "$thr" > "$res"
                     printf 'done: speed limit %s%% -> %s%%, battery %s -> %s C%s\n' "$lim0" "$lim1" "$(awk -v t="$bt0" 'BEGIN{printf "%.1f", t/100}')" "$(awk -v t="$bt1" 'BEGIN{printf "%.1f", t/100}')" "$([ "$thr" = true ] && printf ' - THROTTLED under load' )" >>"$log" ;;
      *) printf '{"ok":false,"error":"unknown job type"}' > "$res" ;;
    esac
    [ -s "$res" ] || printf '{"ok":false,"error":"no result"}' > "$res"
    mv -f "$jf.running" "$jf.done" 2>/dev/null
  ) </dev/null >/dev/null 2>&1 &
  return 0
}
poll_jobs() {
  local jf
  ls "$RUN/jobs"/*.running >/dev/null 2>&1 && return 0     # one at a time
  for jf in "$RUN/jobs"/*.job; do [ -f "$jf" ] && { run_job "$jf"; break; }; done
  return 0
}
# ---- drive health via the bundled smartctl (admin only; refreshed every ~5 min) --
# Apple's diskutil only says Verified/Failing; smartctl (Tools/_bin, GPL-2) gives
# Percentage Used / spare blocks / media errors / TB written. The parsed
# key=value summary lands in $RUN/smart/<disk>.kv for api.sh's disks route.
SMART_DIR="$RUN/smart"; mkdir -p "$SMART_DIR" 2>/dev/null; SMARTCTL=""
if [ "$HAVE_SUDO" = "1" ] && [ -n "$PROFILE" ] && [ -f "$PROFILE/_shared/benchmac.sh" ]; then
  . "$PROFILE/_shared/benchmac.sh" 2>/dev/null
  SMARTCTL="$(bm_smartctl "$RUN/bin" 2>/dev/null)" || SMARTCTL=""
fi
smart_scan() {
  [ -n "$SMARTCTL" ] || return 0
  local d out
  for d in $(diskutil list 2>/dev/null | grep -E '^/dev/disk[0-9]+ \((internal|external)' | grep -o '^/dev/disk[0-9]*' | sort -u); do
    out="$SMART_DIR/${d#/dev/}"
    sudo -n "$SMARTCTL" -a "$d" > "$out.txt" 2>&1
    bm_smart_kv "$out.txt" > "$out.kv.tmp" 2>/dev/null && mv -f "$out.kv.tmp" "$out.kv"
  done
  return 0
}
smart_scan
STATS="$RUN/stats.json"; HIST="$RUN/history.log"; TMPF="$RUN/stats.tmp"
NCPU="$(sysctl -n hw.ncpu 2>/dev/null)"; [ -n "$NCPU" ] || NCPU=1
MEMSIZE="$(sysctl -n hw.memsize 2>/dev/null)"; [ -n "$MEMSIZE" ] || MEMSIZE=0
PAGESZ="$(sysctl -n hw.pagesize 2>/dev/null)"; [ -n "$PAGESZ" ] || PAGESZ=4096
AS=0; [ "$(uname -m 2>/dev/null)" = "arm64" ] && AS=1
# JSON string sanitiser: strip control chars, escape backslash + quote
js() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
num() { case "$1" in ''|*[!0-9.-]*) printf '0' ;; *) printf '%s' "$1" ;; esac; }

prev_in=0; prev_out=0; prev_ts=0; prev_dr=0; prev_dw=0
tick=0; pm_temp=""; pm_fan=""; pm_pressure=""; pm_power=""
while [ -f "$RUN/run" ]; do
  ts="$(date +%s)"
  # ---- CPU ------------------------------------------------------------------
  cpu="$(ps -A -o %cpu= 2>/dev/null | awk -v n="$NCPU" '{ s+=$1 } END { v=s/n; if (v>100) v=100; printf "%.1f", v }')"
  load="$(sysctl -n vm.loadavg 2>/dev/null | tr -d '{}' | awk '{ printf "%s %s %s", $1, $2, $3 }')"
  # ---- memory ---------------------------------------------------------------
  vm="$(vm_stat 2>/dev/null)"
  vmv() { printf '%s' "$vm" | awk -F: -v k="$1" '$1==k { gsub(/[^0-9]/,"",$2); print $2; exit }'; }
  pfree="$(vmv 'Pages free')"; pact="$(vmv 'Pages active')"; pinact="$(vmv 'Pages inactive')"
  pwired="$(vmv 'Pages wired down')"; pcomp="$(vmv 'Pages occupied by compressor')"; pspec="$(vmv 'Pages speculative')"
  mem_used="$(awk -v a="${pact:-0}" -v w="${pwired:-0}" -v c="${pcomp:-0}" -v p="$PAGESZ" 'BEGIN{ printf "%.0f", (a+w+c)*p }')"
  mem_cached="$(awk -v i="${pinact:-0}" -v s="${pspec:-0}" -v p="$PAGESZ" 'BEGIN{ printf "%.0f", (i+s)*p }')"
  mem_pct="$(awk -v u="$mem_used" -v t="$MEMSIZE" 'BEGIN{ if (t>0) printf "%.1f", u*100/t; else print 0 }')"
  mp="$(memory_pressure 2>/dev/null | grep -o 'free percentage: [0-9]*' | grep -o '[0-9]*')"
  swap="$(sysctl -n vm.swapusage 2>/dev/null | sed 's/.*used = \([0-9.]*\)M.*/\1/')"
  # ---- GPU (ioreg reports utilisation on Apple silicon + most Intel) ---------
  gpu="$(ioreg -r -d 1 -c IOAccelerator 2>/dev/null | grep -o '"Device Utilization %"=[0-9]*' | head -n1 | grep -o '[0-9]*$')"
  [ -n "$gpu" ] || gpu=0
  # ---- network (all non-loopback interfaces) --------------------------------
  nb="$(netstat -ib 2>/dev/null | awk '$1!="lo0" && $1!="Name" && $3 ~ /Link/ { i+=$7; o+=$10 } END { printf "%d %d", i, o }')"
  cur_in="${nb%% *}"; cur_out="${nb##* }"
  if [ "$prev_ts" -gt 0 ]; then
    dt=$((ts-prev_ts)); [ "$dt" -lt 1 ] && dt=1
    net_in="$(awk -v a="$cur_in" -v b="$prev_in" -v d="$dt" 'BEGIN{ v=(a-b)/d; if (v<0) v=0; printf "%.0f", v }')"
    net_out="$(awk -v a="$cur_out" -v b="$prev_out" -v d="$dt" 'BEGIN{ v=(a-b)/d; if (v<0) v=0; printf "%.0f", v }')"
  else net_in=0; net_out=0; fi
  prev_in="$cur_in"; prev_out="$cur_out"
  # ---- disk throughput (iostat, MB/s for the first disk) ----------------------
  dio="$(iostat -d -c 1 2>/dev/null | tail -n1 | awk '{ printf "%.2f", $3 }')"; [ -n "$dio" ] || dio=0
  df_root="$(df -k / 2>/dev/null | tail -n1)"
  disk_tot="$(printf '%s' "$df_root" | awk '{ print $2*1024 }')"; disk_used="$(printf '%s' "$df_root" | awk '{ print $3*1024 }')"
  disk_pct="$(printf '%s' "$df_root" | awk '{ print $5 }' | tr -d '%')"
  # ---- battery / power --------------------------------------------------------
  batt="$(pmset -g batt 2>/dev/null)"
  batt_pct="$(printf '%s' "$batt" | grep -o '[0-9]*%' | head -n1 | tr -d '%')"
  batt_state="$(printf '%s' "$batt" | grep -o '%; [a-z ]*' | head -n1 | sed 's/%; //')"
  batt_src="$(printf '%s' "$batt" | head -n1 | grep -o "'[^']*'" | tr -d "'")"
  batt_left="$(printf '%s' "$batt" | grep -o '[0-9]*:[0-9]* remaining' | head -n1 | sed 's/ remaining//')"
  # ---- thermals (Intel: pmset therm is free; sudo: powermetrics every 5th tick) -
  speed_lim=""; if [ "$AS" = "0" ]; then speed_lim="$(pmset -g therm 2>/dev/null | grep -o 'CPU_Speed_Limit *= *[0-9]*' | grep -o '[0-9]*$')"; fi
  if [ "$HAVE_SUDO" = "1" ] && [ $((tick % 5)) -eq 0 ]; then
    if [ "$AS" = "1" ]; then
      pmo="$(sudo -n powermetrics --samplers thermal,cpu_power -n 1 -i 500 2>/dev/null)"
      pm_pressure="$(printf '%s' "$pmo" | grep -m1 -i 'pressure level' | sed 's/.*: *//')"
      pm_power="$(printf '%s' "$pmo" | grep -m1 'Combined Power' | grep -o '[0-9]* mW' | grep -o '[0-9]*' | awk '{ printf "%.1f", $1/1000 }')"
    else
      pmo="$(sudo -n powermetrics --samplers smc -n 1 -i 500 2>/dev/null)"
      pm_temp="$(printf '%s' "$pmo" | grep -m1 'CPU die temperature' | grep -o '[0-9.]*' | head -n1)"
      pm_fan="$(printf '%s' "$pmo" | grep -m1 -i 'Fan' | grep -o '[0-9.]* rpm' | grep -o '^[0-9]*')"
    fi
  fi
  # ---- top processes ----------------------------------------------------------
  procs="$(ps -Aro pid=,%cpu=,%mem=,rss=,comm= 2>/dev/null | head -n 14 | awk '{
      pid=$1; c=$2; m=$3; r=$4; $1=$2=$3=$4=""; sub(/^ +/,""); n=$0; sub(/.*\//,"",n);
      gsub(/[^A-Za-z0-9 ._()+-]/,"",n);
      printf "%s{\"pid\":%d,\"cpu\":%.1f,\"mem\":%.1f,\"rss\":%d,\"name\":\"%s\"}", (NR>1?",":""), pid, c, m, r*1024, substr(n,1,40) }')"
  nproc="$(ps -A 2>/dev/null | wc -l | tr -d ' ')"
  up="$(uptime 2>/dev/null | sed 's/.*up \(.*\), [0-9]* users*.*/\1/' | sed 's/,  *load.*//')"
  # ---- write ------------------------------------------------------------------
  {
    printf '{"ts":%s,"interval":%s,"ncpu":%s,"as":%s,' "$ts" "$INTERVAL" "$NCPU" "$AS"
    printf '"cpu":%s,"load":"%s",' "$(num "$cpu")" "$(js "$load")"
    printf '"mem":{"total":%s,"used":%s,"cached":%s,"pct":%s,"free_pct":%s,"swap_mb":%s},' "$MEMSIZE" "$mem_used" "$mem_cached" "$(num "$mem_pct")" "$(num "${mp:-0}")" "$(num "${swap:-0}")"
    printf '"gpu":%s,' "$(num "$gpu")"
    printf '"net":{"in":%s,"out":%s},' "$(num "$net_in")" "$(num "$net_out")"
    printf '"disk":{"total":%s,"used":%s,"pct":%s,"mbps":%s},' "$(num "$disk_tot")" "$(num "$disk_used")" "$(num "${disk_pct:-0}")" "$(num "$dio")"
    printf '"battery":{"pct":%s,"state":"%s","source":"%s","left":"%s","present":%s},' "$(num "${batt_pct:-0}")" "$(js "$batt_state")" "$(js "$batt_src")" "$(js "$batt_left")" "$([ -n "$batt_pct" ] && printf true || printf false)"
    printf '"thermal":{"speed_limit":%s,"die_c":%s,"fan_rpm":%s,"pressure":"%s","power_w":%s},' "$(num "${speed_lim:-100}")" "$(num "${pm_temp:-0}")" "$(num "${pm_fan:-0}")" "$(js "$pm_pressure")" "$(num "${pm_power:-0}")"
    printf '"procs":[%s],"nproc":%s,"uptime":"%s"}\n' "$procs" "$(num "$nproc")" "$(js "$up")"
  } > "$TMPF" && mv -f "$TMPF" "$STATS"
  printf '%s %s %s %s %s %s %s\n' "$ts" "$(num "$cpu")" "$(num "$mem_pct")" "$(num "$gpu")" "$(num "$net_in")" "$(num "$net_out")" "$(num "$dio")" >> "$HIST"
  # keep the history file bounded (last 600 ticks)
  if [ $((tick % 100)) -eq 99 ]; then tail -n 600 "$HIST" > "$HIST.tmp" 2>/dev/null && mv -f "$HIST.tmp" "$HIST"; fi
  # logs every tick (they change while a tool runs), reports every 5th tick
  if [ $((tick % 5)) -eq 4 ]; then mirror_tools full; mirror_qc; else mirror_tools; fi
  poll_jobs
  # drive health counters every 150 ticks (~5 min at the default 2 s)
  if [ $((tick % 150)) -eq 149 ]; then smart_scan; fi
  prev_ts="$ts"; tick=$((tick+1))
  sleep "$INTERVAL"
done
