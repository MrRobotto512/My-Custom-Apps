#!/bin/bash
#  Dashboard  -  live bench dashboard for the Mac in front of you
#  ============================================================================
#  Opens a dashboard in the Mac's own browser (http://127.0.0.1:8765) with
#  pages for Summary / Performance / Processes / System Info / Startup / Users /
#  Services / Disks / Tools / Reports. Live CPU, memory, GPU, network, disk,
#  battery and thermal meters + graphs, the trade-in flags, and one-click launch
#  of the other tools on this stick with their live log and latest report.
#
#  How it works (nothing installed on the Mac, nothing changed):
#    collector.sh   samples the Mac every 2 s -> stats.json + history.log
#    api.sh         turns the Mac's state into JSON for the page
#    server         the Apache httpd every macOS ships (/usr/sbin/httpd) on
#                   127.0.0.1 only, CGI -> api.sh. If httpd is missing, a
#                   tiny bash + nc server takes over (same API, one request at
#                   a time). Nothing listens on the network - localhost only.
#    www/index.html the dashboard (no CDN, works offline)
#
#  Run:  double-click "Start Dashboard.command" at the root of the stick
#        (or this file). Admin password is optional (it
#        unlocks fan RPM / die temperature on Intel, thermal pressure + power on
#        Apple silicon, and the smartctl drive-health block on the Disks page).
#        The Tests page (display / keyboard / trackpad / speakers / camera+mic /
#        ports+charging + QC checklist) needs no password; its checklists are
#        saved to Dashboard/QC-Reports/ on the stick via the collector.
#        Close the Terminal window or press Ctrl-C to stop.
#  Switches:  -Port N   -NoSudo   -NoOpen (don't launch the browser)   -NC (force the nc server)
#             -Setup            setup mode: a Mac still at Setup Assistant (_mbsetupuser, no admin
#                               possible) -> Intake page first, sudo-only pages hidden. Auto-detected.
#             -Bench URL        where the Intake page sends its results (the bench's Keystroke
#                               Flasher page, e.g. http://192.168.86.41:8377/x/automaton_flasher);
#                               the dongle passes it - there is no stick at the setup screen.
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR" || exit 1
. "$APP_DIR/../_shared/benchmac.sh" || { echo "missing _shared/benchmac.sh"; exit 1; }

PORT=8765; NOSUDO=0; NOOPEN=0; FORCE_NC=0; SETUP=0; BENCH=""
bm_parse_common "$@"
set -- $BM_REST
while [ $# -gt 0 ]; do
  case "$1" in
    -Port|-port) shift; PORT="${1:-8765}" ;;
    -NoSudo|-nosudo) NOSUDO=1 ;;
    -NoOpen|-noopen) NOOPEN=1 ;;
    -NC|-nc) FORCE_NC=1 ;;
    -Setup|-setup) SETUP=1 ;;
    -Bench|-bench) shift; BENCH="${1:-}" ;;
  esac
  shift
done
# Setup Assistant runs everything as _mbsetupuser: no password exists, so never ask for one
[ "$(id -un 2>/dev/null)" = "_mbsetupuser" ] && SETUP=1
[ "$SETUP" = "1" ] && NOSUDO=1

bm_init "Dashboard" "1.7"
RUN="$BM_TMP/run"; mkdir -p "$RUN"; : > "$RUN/run"
# the page reads these through /api/status: setup mode + where "Send to bench" posts
printf '%s' "$SETUP" > "$RUN/setup"; printf '%s' "$BENCH" > "$RUN/bench.url"
[ "$SETUP" = "1" ] && info "setup mode: Mac at Setup Assistant (no admin) - Intake + Tests; results go to ${BENCH:-nowhere (no -Bench URL)}"
printf '%s' "$RUN" > "$APP_DIR/.rundir" 2>/dev/null || warn "stick is read-only - the httpd path needs .rundir; using nc"
WWW="$APP_DIR/www"
PROFILE_DIR="$(cd "$APP_DIR/.." && pwd)"   # the Tools/ folder: MacDiag/, MalwareTriage/, ...
# exFAT has no exec bits (everything is executable there); on an HFS+/APFS copy make sure the CGI can run
chmod +x "$APP_DIR/api.cgi" "$APP_DIR/api.sh" "$APP_DIR/collector.sh" 2>/dev/null
[ -f "$WWW/index.html" ] || { fail "www/index.html missing"; bm_pause; exit 1; }

[ "$NOSUDO" = "1" ] || bm_sudo_start
# the page asks whether the collector holds the admin ticket (tools run through it without a Terminal)
printf '%s' "$BM_SUDO" > "$RUN/have_sudo"

# ---- leftovers from an earlier run ------------------------------------------
# A previous dashboard whose Terminal window was simply closed leaves its httpd
# (SIGHUP = "graceful restart" to Apache), nc listener and collector behind. They
# would answer on our port with a stale config. Stop them, then make sure the
# port is really free (or move to the next one).
pkill -f "Dashboard/collector.sh" 2>/dev/null
pkill -f "httpd -f .*/benchmac\..*/httpd.conf" 2>/dev/null
pkill -f "nc -l 127.0.0.1 $PORT" 2>/dev/null
sleep 0.3
free_port() {
  local tries=0 pids p cmd
  while [ $tries -lt 10 ]; do
    pids="$(lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u)"
    [ -n "$pids" ] || return 0
    local foreign=0
    for p in $pids; do
      cmd="$(ps -o command= -p "$p" 2>/dev/null)"
      case "$cmd" in
        *httpd*httpd.conf*|*"nc -l 127.0.0.1 $PORT"*|*Dashboard*)
          warn "port $PORT still held by a leftover dashboard server (pid $p: $(printf '%s' "$cmd" | cut -c1-60)) - stopping it"
          kill "$p" 2>/dev/null; sleep 0.5; kill -9 "$p" 2>/dev/null ;;
        *) foreign=1 ;;
      esac
    done
    if [ "$foreign" = "1" ]; then
      warn "port $PORT is used by another program - trying $((PORT+1))"; PORT=$((PORT+1))
    fi
    sleep 0.5; tries=$((tries+1))
  done
  fail "could not get a free port near $PORT"; return 1
}
free_port || { bm_pause; exit 1; }

info "starting collector (2 s samples)..."
/bin/bash "$APP_DIR/collector.sh" "$RUN" 2 "$BM_SUDO" "$PROFILE_DIR" >/dev/null 2>&1 &
COLLECTOR_PID=$!
info "priming system info (system_profiler, a few seconds)..."
/bin/bash "$APP_DIR/api.sh" sysinfo >/dev/null 2>&1 &

cleanup_all() {
  rm -f "$RUN/run" 2>/dev/null
  [ -n "$COLLECTOR_PID" ] && kill "$COLLECTOR_PID" 2>/dev/null
  [ -n "$HTTPD_PID" ] && kill "$HTTPD_PID" 2>/dev/null
  [ -n "$NC_PID" ] && kill "$NC_PID" 2>/dev/null
  pkill -f "nc -l 127.0.0.1 $PORT" 2>/dev/null
  [ -n "$RUN" ] && pkill -f "httpd -f $RUN/httpd.conf" 2>/dev/null
  rm -f "$APP_DIR/.rundir" 2>/dev/null
  bm_cleanup
}
# Name the signal in the log: at Setup Assistant the dashboard once vanished ~2 min in
# with no "httpd exited" / "stopping" line - only this trap could have ended it.
on_signal() { warn "received SIG$1 at $(date '+%H:%M:%S') - shutting down (window closed / Ctrl-C / killed)"; cleanup_all; exit 0; }
trap 'on_signal INT' INT; trap 'on_signal TERM' TERM; trap 'on_signal HUP' HUP; trap 'cleanup_all' EXIT

# ---- server 1: Apple's httpd (concurrent, keep-alive, proper CGI) -------------
# httpd serves from a copy in the temp run folder on the Mac's own disk, never
# from the stick: Apple's httpd is a separate process and macOS may refuse it
# access to a removable volume (TCC) even though this Terminal has it -> 403.
# api.cgi finds the real Dashboard folder through the .dash file next to it.
start_httpd() {
  local httpd="${BM_HTTPD:-/usr/sbin/httpd}" moddir="${BM_MODDIR:-/usr/libexec/apache2}" mimet=/private/etc/apache2/mime.types
  [ -f "$mimet" ] || mimet=/etc/mime.types
  [ -x "$httpd" ] || { dim "no $httpd on this Mac"; return 1; }
  [ -d "$moddir" ] || { dim "no $moddir on this Mac"; return 1; }
  [ -f "$APP_DIR/.rundir" ] || return 1
  local site="$RUN/site"; mkdir -p "$site" || return 1
  cp "$WWW/index.html" "$site/index.html" && cp "$APP_DIR/api.cgi" "$site/api.cgi" && cp "$APP_DIR/api.sh" "$site/api.sh" || { warn "could not stage the page into $site"; return 1; }
  printf '%s' "$APP_DIR" > "$site/.dash"; printf '%s' "$RUN" > "$site/.rundir"; chmod 755 "$site/api.cgi" "$site/api.sh"
  local conf="$RUN/httpd.conf" m loaded=""
  {
    printf 'ServerName benchtool.local\nServerRoot "/usr"\nListen 127.0.0.1:%s\n' "$PORT"
    for m in mpm_prefork unixd authz_core authz_host dir mime alias cgi log_config headers; do
      [ -f "$moddir/mod_$m.so" ] && { printf 'LoadModule %s_module %s/mod_%s.so\n' "$m" "$moddir" "$m"; loaded="$loaded $m"; }
    done
    printf 'PidFile "%s/httpd.pid"\nErrorLog "%s/httpd.err"\nLogLevel info\nTypesConfig "%s"\n' "$RUN" "$RUN" "$mimet"
    printf 'DocumentRoot "%s"\nDirectoryIndex index.html\n' "$site"
    printf '<Directory />\n  AllowOverride None\n  Require all granted\n</Directory>\n'
    printf '<Directory "%s">\n  Options +FollowSymLinks +ExecCGI\n  AllowOverride None\n  Require all granted\n</Directory>\n' "$site"
    printf 'ScriptAlias /api "%s/api.cgi"\n' "$site"
    printf 'RedirectMatch ^/$ /index.html\n'
    printf '<IfModule headers_module>\n  Header set Cache-Control "no-store"\n</IfModule>\n'
    printf 'StartServers 1\nMinSpareServers 1\nMaxSpareServers 2\nMaxRequestWorkers 8\n'
  } > "$conf"
  dim "httpd modules:$loaded"
  "$httpd" -f "$conf" -DFOREGROUND >"$RUN/httpd.out" 2>&1 &
  HTTPD_PID=$!
  local i=0 code_page="" code_api=""
  while [ $i -lt 30 ]; do
    code_page="$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/index.html" 2>/dev/null)"
    [ -n "$code_page" ] && [ "$code_page" != "000" ] && break
    kill -0 "$HTTPD_PID" 2>/dev/null || break
    sleep 0.2; i=$((i+1))
  done
  code_api="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/stats" 2>/dev/null)"
  if [ "$code_page" = "200" ] && [ "$code_api" = "200" ]; then return 0; fi
  warn "httpd answered page=$code_page api=$code_api - not usable, switching to the nc server"
  sleep 1
  { printf -- '---- httpd.err ----\n'; tail -n 12 "$RUN/httpd.err" 2>/dev/null; printf -- '---- httpd.out ----\n'; tail -n 5 "$RUN/httpd.out" 2>/dev/null; } | tee -a "$LOG" | sed 's/^/    /'
  kill "$HTTPD_PID" 2>/dev/null; wait "$HTTPD_PID" 2>/dev/null; HTTPD_PID=""
  return 1
}

# ---- server 2: bash + nc (one request at a time, but zero dependencies) -------
nc_handle() {   # stdin = request from nc, stdout -> nc's stdin (the socket)
  local line method path query file body ct len clen=0
  read -r -t 5 line || return 0
  line="$(printf '%s' "$line" | tr -d '\r')"
  method="${line%% *}"; path="${line#* }"; path="${path%% *}"
  # drain headers (bounded); remember Content-Length for POST bodies
  local h n=0; while [ $n -lt 40 ] && read -r -t 2 h; do h="$(printf '%s' "$h" | tr -d '\r')"; [ -z "$h" ] && break; case "$h" in [Cc]ontent-[Ll]ength:*) clen="$(printf '%s' "${h#*:}" | tr -d ' ')";; esac; n=$((n+1)); done
  query="${path#*\?}"; [ "$query" = "$path" ] && query=""; path="${path%%\?*}"
  case "$path" in
    /api/*)
      local route="${path#/api/}"
      case "$route" in report|log) ct="text/plain; charset=utf-8";; *) ct="application/json; charset=utf-8";; esac
      if [ "$method" = "POST" ] && [ "${clen:-0}" -gt 0 ] 2>/dev/null; then
        BM_POST="$RUN/post.$$"; head -c "$clen" > "$BM_POST"; export BM_POST
        body="$(/bin/bash "$APP_DIR/api.sh" "$route" "$query" 2>/dev/null)"; rm -f "$BM_POST"; unset BM_POST
      else
        body="$(/bin/bash "$APP_DIR/api.sh" "$route" "$query" 2>/dev/null)"
      fi
      len="$(printf '%s' "$body" | wc -c | tr -d ' ')"
      printf 'HTTP/1.1 200 OK\r\nContent-Type: %s\r\nContent-Length: %s\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n' "$ct" "$len"
      printf '%s' "$body" ;;
    /|/index.html)
      len="$(wc -c < "$WWW/index.html" | tr -d ' ')"
      printf 'HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %s\r\nConnection: close\r\n\r\n' "$len"
      cat "$WWW/index.html" ;;
    *)
      file="$WWW/$(basename "$path")"
      if [ -f "$file" ]; then
        case "$file" in *.css) ct="text/css";; *.js) ct="application/javascript";; *.png) ct="image/png";; *.svg) ct="image/svg+xml";; *) ct="application/octet-stream";; esac
        len="$(wc -c < "$file" | tr -d ' ')"
        printf 'HTTP/1.1 200 OK\r\nContent-Type: %s\r\nContent-Length: %s\r\nConnection: close\r\n\r\n' "$ct" "$len"; cat "$file"
      else
        printf 'HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\nConnection: close\r\n\r\nnot found'
      fi ;;
  esac
}
nc_loop() {
  local fifo="$RUN/fifo"; rm -f "$fifo"; mkfifo "$fifo" || return 1
  while [ -f "$RUN/run" ]; do
    # nc's stdout (the request) -> handler; handler's stdout -> fifo -> nc's stdin (the socket).
    # A watchdog kills a connection that never sends a request (browser pre-connects).
    nc -l 127.0.0.1 "$PORT" < "$fifo" | nc_handle > "$fifo" &
    local job=$!
    ( sleep 8; kill -0 "$job" 2>/dev/null && pkill -f "nc -l 127.0.0.1 $PORT" 2>/dev/null ) >/dev/null 2>&1 </dev/null &
    local wd=$!
    wait "$job" 2>/dev/null
    pkill -P "$wd" 2>/dev/null; kill "$wd" 2>/dev/null; wait "$wd" 2>/dev/null
  done
}

SERVER=""
if [ "$FORCE_NC" != "1" ] && start_httpd; then
  SERVER="httpd"; good "server: Apple httpd on http://127.0.0.1:$PORT (pid $HTTPD_PID)"
else
  command -v nc >/dev/null 2>&1 || { fail "neither httpd nor nc is available on this Mac"; bm_pause; exit 1; }
  SERVER="nc"; good "server: bash + nc on http://127.0.0.1:$PORT (one request at a time)"
  nc_loop &
  NC_PID=$!
fi
sleep 1
URL="http://127.0.0.1:$PORT/index.html"
[ "$NOOPEN" = "1" ] || open "$URL" 2>/dev/null
printf '\n%s  Dashboard running at %s  -  press Ctrl-C (or close this window) to stop.%s\n\n' "$C_BOLD" "$URL" "$C_RESET"
# keep the session alive; exit when the page's Quit button removes the run flag.
# If Apple's httpd dies underneath us (seen once at Setup Assistant: the page kept
# working in Safari while port 8765 went dark within a couple of minutes), do NOT
# quit - log why, bring httpd back up, and after three deaths fall back to the
# built-in bash+nc server so the page never loses its API.
HTTPD_DEATHS=0
while [ -f "$RUN/run" ]; do
  sleep 1
  if [ "$SERVER" = "httpd" ] && ! kill -0 "$HTTPD_PID" 2>/dev/null; then
    HTTPD_DEATHS=$((HTTPD_DEATHS+1))
    warn "httpd exited (death $HTTPD_DEATHS at $(date '+%H:%M:%S'))"
    { printf -- '---- httpd.err (tail) ----\n'; tail -n 15 "$RUN/httpd.err" 2>/dev/null; printf -- '---- httpd.out (tail) ----\n'; tail -n 5 "$RUN/httpd.out" 2>/dev/null; } | tee -a "$LOG" | sed 's/^/    /'
    HTTPD_PID=""
    if [ "$HTTPD_DEATHS" -lt 3 ] && start_httpd; then
      good "httpd restarted on http://127.0.0.1:$PORT (pid $HTTPD_PID)"
    else
      command -v nc >/dev/null 2>&1 || { fail "httpd keeps dying and nc is not available"; break; }
      SERVER="nc"; warn "switching to the built-in bash + nc server on http://127.0.0.1:$PORT"
      nc_loop &
      NC_PID=$!
    fi
  fi
done
info "stopping"
exit 0
