#!/bin/bash
#  Set Gemini Key  -  one place to set the Google Gemini API key for every
#  Mac Diagnostics app on this drive that needs one.
#  ============================================================================
#  Double-click it once. It asks for your Gemini API key, then writes it as
#  gemini.key into every app folder here whose script reads "gemini.key"
#  (MacDiag, MalwareTriage, and any AI app you add later - nothing to edit).
#
#  Normally you never need this: BenchTool -> Settings -> Import API Key writes
#  the key into every app folder on the Pi, and USB Drive Writer copies it onto
#  the stick. Use this on the road, when the key changed, or on a stick that
#  was written before the key was imported.
#
#  Get a key at  https://aistudio.google.com  ->  "Get API key".
#  Usage:  Set-GeminiKey.command [KEY]       (no KEY = it asks)
DIR="$(cd "$(dirname "$0")" && pwd)"
KEY="$1"
if [ -z "$KEY" ]; then
  printf 'Paste the Gemini API key (starts with AIza...): '
  read -r KEY
fi
KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
case "$KEY" in
  AIza*) ;;
  "") echo "no key given"; exit 1 ;;
  *) echo "that does not look like a Gemini key (they start with AIza) - saving anyway" ;;
esac
n=0
for d in "$DIR"/*/; do
  d="${d%/}"
  case "$(basename "$d")" in _*) continue ;; esac
  if [ -f "$d/gemini.key" ] || grep -lq 'gemini.key' "$d"/*.command "$d"/*.sh 2>/dev/null; then
    printf '%s\n' "$KEY" > "$d/gemini.key" && chmod 600 "$d/gemini.key" 2>/dev/null && { echo "[OK] $(basename "$d")/gemini.key"; n=$((n+1)); }
  fi
done
# shared apps read the profile-root key as a fallback too
printf '%s\n' "$KEY" > "$DIR/gemini.key" && chmod 600 "$DIR/gemini.key" 2>/dev/null && echo "[OK] gemini.key (profile root)"
# quick live check
if command -v curl >/dev/null 2>&1; then
  code="$(curl -s -m 15 -o /dev/null -w '%{http_code}' -H "x-goog-api-key: $KEY" "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1" 2>/dev/null)"
  case "$code" in 200) echo "[OK] key accepted by Google ($n app folder(s) updated)";; 000) echo "[!] offline - key saved, not verified";; *) echo "[!] Google answered HTTP $code - check the key";; esac
fi
printf '\nPress any key to close...'; read -r -s -n 1 _; printf '\n'
