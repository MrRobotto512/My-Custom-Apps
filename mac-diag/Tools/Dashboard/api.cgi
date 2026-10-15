#!/bin/bash
# api.cgi - CGI wrapper around api.sh for the httpd server path.
#   /api/<route>?<query>  ->  PATH_INFO=/<route>  QUERY_STRING=<query>
HERE="$(cd "$(dirname "$0")" && pwd)"
# When httpd serves a staged copy from the temp folder, .dash points back at the real Dashboard folder on the stick
DASH="$(cat "$HERE/.dash" 2>/dev/null)"; [ -n "$DASH" ] || DASH="$HERE"
API="$HERE/api.sh"; [ -f "$API" ] || API="$DASH/api.sh"
# tell api.sh where the profile (stick) and the run folder are without it touching the stick to find out
BM_PROFILE="${DASH%/*}"; export BM_PROFILE
BM_RUN="$(cat "$HERE/.rundir" 2>/dev/null || cat "$DASH/.rundir" 2>/dev/null)"; export BM_RUN
route="${PATH_INFO#/}"
# POST bodies (QC checklist save) are spooled to a file api.sh reads via BM_POST
if [ "$REQUEST_METHOD" = "POST" ] && [ -n "$CONTENT_LENGTH" ] && [ "$CONTENT_LENGTH" -gt 0 ] 2>/dev/null; then
  BM_POST="${BM_RUN:-${TMPDIR:-/tmp}}/post.$$"; head -c "$CONTENT_LENGTH" > "$BM_POST"; export BM_POST
fi
case "$route" in
  report|log) printf 'Content-Type: text/plain; charset=utf-8\r\n' ;;
  *)          printf 'Content-Type: application/json; charset=utf-8\r\n' ;;
esac
printf 'Cache-Control: no-store\r\n\r\n'
exec /bin/bash "$API" "$route" "$QUERY_STRING"
