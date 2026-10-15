#!/bin/bash
#  Apple Diagnostics / AHT Finder  (BenchTool Mac Diagnostics)
#  ============================================================================
#  Gets the RIGHT Apple hardware test onto the bench for the Mac in front of you:
#
#   * Macs from mid-2013 on (and every Apple silicon Mac) have Apple Diagnostics
#     built into firmware - this tool prints the exact key sequence for THIS
#     Mac (Intel: D / Option-D over the internet, Apple silicon: startup
#     options + Command-D) and decodes the reference codes it returns
#     (PPT004, VDH002, PFM00x ...) into plain language + the likely part.
#
#   * Apple silicon + macOS 26 (Tahoe) and later: Command-D opens REPAIR
#     ASSISTANT instead - Mac Resource Inspector (the old diagnostics), Display
#     Anomalies, Keyboard / Trackpad / Touch ID and Audio tests, plus the
#     post-repair calibration (battery, Touch ID, display, logic board, lid
#     angle sensor, top case) an independent shop used to have no access to.
#     The tool also explains the Parts & Service pane (Genuine / Used /
#     Unknown / Unverified) that a trade-in check must read.
#
#   * Older Macs (2006 - early 2013) used Apple Hardware Test (AHT), a
#     per-model disk image Apple still serves. This tool reads the Mac's model
#     identifier + board-id, looks up the download code, downloads the image
#     (Apple first, GitHub mirror if Apple is down), caches it on this stick
#     (AHT-Images/) and can build a bootable AHT USB from a second stick
#     (erase -> copy System folder -> bless).  Hold Option at boot, pick "AHT".
#
#  Run:  double-click Run-AppleDiagnostics.command
#  Switches:  -Code XXX000 [YYY000...]   just decode reference codes and exit
#             -Model MacBookPro8,2 -Board Mac-94245A3940C91C80   look up a Mac
#                                   that is NOT the one you're sitting at
#             -Quiet
#
#  AHT table + mirror: AHTFinder by Maxim Coppieters (MPL-2.0), github.com/
#  MaxTechnics/AHTFinder - the table was reused, the script rewritten.
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR" || exit 1
. "$APP_DIR/../_shared/benchmac.sh" || { echo "missing _shared/benchmac.sh"; exit 1; }

AHT_TSV="$APP_DIR/aht_codes.tsv"; ADP_TSV="$APP_DIR/adp_codes.tsv"
IMG_DIR="$APP_DIR/AHT-Images"
APPLE_URL="https://download.info.apple.com/Apple_Hardware_Test"
MIRROR_URL="https://raw.githubusercontent.com/MaxTechnics/AHTFinder/master/Download%20Code%20Backups"

CODES=""; OVR_MODEL=""; OVR_BOARD=""
bm_parse_common "$@"
set -- $BM_REST
while [ $# -gt 0 ]; do
  case "$1" in
    -Code|-code) shift; while [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; do CODES="$CODES $1"; shift; done; continue ;;
    -Model|-model) shift; OVR_MODEL="$1" ;;
    -Board|-board) shift; OVR_BOARD="$1" ;;
  esac
  shift
done

bm_init "AppleDiagnostics" "1.1"
META="$BM_TMP/meta.txt"; : > "$META"
meta() { printf '%s=%s\n' "$1" "$2" >> "$META"; }
T="$BM_TMP"

# ---- reference-code decoder -------------------------------------------------
decode_code() {
  local c; c="$(printf '%s' "$1" | tr 'a-z' 'A-Z' | tr -cd 'A-Z0-9')"
  local row; row="$(grep -v '^#' "$ADP_TSV" | awk -F'\t' -v c="$c" '$1==c { print; exit }')"
  if [ -n "$row" ]; then
    local meaning note; meaning="$(printf '%s' "$row" | cut -f2)"; note="$(printf '%s' "$row" | cut -f3)"
    case "$c" in ADP000) good "$c - $meaning"; bm_finding INFO "Diagnostics" "$c: $meaning" "$note";;
      PPT002|PPT003|CNW007|CNW008|PPP003|NNN001) bm_finding MED "Diagnostics" "$c: $meaning" "$note";;
      *) bm_finding HIGH "Diagnostics" "$c: $meaning" "$note";; esac
  else
    local fam="${c%???}"
    local hint=""
    case "$fam" in
      CNW) hint="Wi-Fi family";; NDC) hint="camera family";; NDD) hint="USB family";; NDK) hint="keyboard family";; NDL) hint="Bluetooth family";;
      NDR) hint="trackpad family";; NDT) hint="Thunderbolt family";; PFM) hint="SMC family";; PFR) hint="firmware family";; PPF) hint="fan family";;
      PPM) hint="memory family";; PPN) hint="power-management family";; PPP) hint="power-adapter family";; PPR) hint="processor family";;
      PPT) hint="battery family";; VDC) hint="SD-reader family";; VDH) hint="storage family";; VFD) hint="display/GPU family";; VFF) hint="audio family";;
      ADP) hint="pass/diagnostic status";;
    esac
    bm_finding MED "Diagnostics" "$c: not in the local table${hint:+ ($hint)}" "check support.apple.com > 'Apple Diagnostics reference codes'"
  fi
}

if [ -n "$CODES" ]; then
  hdr "Reference codes"
  for c in $CODES; do decode_code "$c"; done
  bm_write_reports "$META" /dev/null "CODES DECODED"; bm_pause; exit 0
fi

# ---- identify the Mac -------------------------------------------------------
hdr "1. This Mac"
system_profiler SPHardwareDataType 2>/dev/null > "$T/hw.txt"
getf() { grep -m1 "$1" "$T/hw.txt" | sed 's/^[^:]*: *//'; }
MODEL_NAME="$(getf 'Model Name')"; MODEL_ID="${OVR_MODEL:-$(getf 'Model Identifier')}"
[ -n "$MODEL_ID" ] || MODEL_ID="$(sysctl -n hw.model 2>/dev/null)"
CHIP="$(getf 'Chip')"; [ -n "$CHIP" ] || CHIP="$(getf 'Processor Name')"
SERIAL="$(getf 'Serial Number')"
BOARD="${OVR_BOARD:-$(ioreg -l 2>/dev/null | awk -F'"' '/board-id/ { print $4; exit }')}"
[ -n "$BOARD" ] || BOARD="$(ioreg -c IOPlatformExpertDevice -d 2 2>/dev/null | awk -F'"' '/board-id/ { print $4; exit }')"
MARKET="$(defaults read "$HOME/Library/Preferences/com.apple.SystemProfiler" 'CPU Names' 2>/dev/null | grep -o '= "[^"]*"' | head -n1 | sed 's/= "//; s/"$//')"
AS=0; bm_is_apple_silicon && AS=1
T2=0; grep -qi 'Apple T2' <(system_profiler SPiBridgeDataType 2>/dev/null) && T2=1
OSV="$(bm_os_version)"; OSMAJ="$(bm_os_major)"
TAHOE=0; [ "$AS" = "1" ] && [ "${OSMAJ:-0}" -ge 26 ] 2>/dev/null && TAHOE=1
info "${MARKET:-$MODEL_NAME}  ($MODEL_ID)  $CHIP"
info "board-id: ${BOARD:-n/a}   serial: $SERIAL   macOS $OSV   $([ "$AS" = 1 ] && printf 'Apple silicon' || { [ "$T2" = 1 ] && printf 'Intel + T2' || printf 'Intel'; })"
meta model "${MARKET:-$MODEL_NAME} ($MODEL_ID)"; meta chip "$CHIP"; meta serial "$SERIAL"; meta board_id "$BOARD"; meta os "macOS $OSV"
[ -n "$OVR_MODEL$OVR_BOARD" ] && warn "using the model/board you typed, not this Mac's"

# ---- AHT lookup -------------------------------------------------------------
hdr "2. Which hardware test does this Mac use?"
ROWS="$(grep -v '^#' "$AHT_TSV" | awk -F'\t' -v m="$MODEL_ID" '$1==m { print }')"
EXACT="$(printf '%s\n' "$ROWS" | awk -F'\t' -v b="$BOARD" '$2==b { print }')"
AHT_CODES=""
if [ -n "$EXACT" ]; then
  AHT_CODES="$(printf '%s\n' "$EXACT" | cut -f3 | tr '\n' ' ')"
  NOTES="$(printf '%s\n' "$EXACT" | cut -f4 | grep -v '^$' | tr '\n' ';')"
  good "Apple Hardware Test (AHT) Mac - exact board match. Download code(s): $AHT_CODES"
  [ -n "$NOTES" ] && dim "$NOTES"
  if printf '%s\n' "$EXACT" | grep -qi 'server'; then
    printf 'Is this the SERVER model? [y/N] '; read -r -n 1 a; printf '\n'
    case "$a" in y|Y) AHT_CODES="$(printf '%s\n' "$EXACT" | grep -i 'Server model' | grep -vi 'non-Server' | cut -f3)";; *) AHT_CODES="$(printf '%s\n' "$EXACT" | grep -i 'non-Server' | cut -f3)";; esac
    AHT_CODES="$(printf '%s' "$AHT_CODES" | tr '\n' ' ')"
  fi
elif [ -n "$ROWS" ]; then
  AHT_CODES="$(printf '%s\n' "$ROWS" | cut -f3 | tr '\n' ' ')"
  warn "AHT-era model, but board-id $BOARD is not in the table for $MODEL_ID - trying the codes listed for the model: $AHT_CODES"
  printf '%s\n' "$ROWS" | awk -F'\t' '{ printf "    %-18s %-24s %s %s\n", $1, $2, $3, $4 }'
fi
meta aht_codes "$AHT_CODES"

show_modern() {
  printf '\n'
  if [ "$TAHOE" = "1" ]; then
    good "Apple silicon Mac on macOS $OSV -> Command-D opens REPAIR ASSISTANT (it replaced Apple Diagnostics in macOS 26)."
    cat <<'EOF'
    1. Shut down. Unplug everything except power (keyboard/trackpad built in; a known Wi-Fi helps).
    2. Press and HOLD the power button until "Loading startup options" appears, then release.
    3. Press Command-D. Repair Assistant opens (it may ask to join Wi-Fi and sign in with an Apple Account
       to talk to Apple's servers - the customer's or the shop's account both work for tests).
    4. Pick the test that matches the complaint:
         Mac Resource Inspector      = the classic Apple Diagnostics pass (RAM, SSD, logic board, sensors, battery)
                                       -> gives the ADP/PPT/VDH... reference codes this tool decodes
         Display Anomalies           = lines, flicker, tint, dead zones - run it in a dim room
         Keyboard / Trackpad / Touch ID = every key, click, force click, Touch ID sensor pairing
         Audio                       = speakers + microphones (crackle, one channel dead)
       Run a failing test 2-3 times; note every reference code and the on-screen result text.
    5. AFTER A REPAIR (battery, Touch ID, display, logic board, lid angle sensor, top case):
         System Settings > General > About > Parts & Service > "Restart & Finish Repair"
       This runs the calibration that used to need Apple's AST 2 - True Tone / battery gauge /
       Touch ID pairing / lid sensor. Skip it and the part shows as Unknown/Unverified and may
       misbehave (no True Tone, wrong battery %, Touch ID refuses to enrol).
EOF
  elif [ "$AS" = "1" ]; then
    good "Apple silicon Mac -> Apple Diagnostics is built in. No download needed."
    cat <<'EOF'
    1. Shut down. Unplug everything except power, keyboard, mouse and (ideally) Ethernet or a known Wi-Fi.
    2. Press and HOLD the power button until "Loading startup options" appears, then release.
    3. Press Command-D. Diagnostics starts (it may ask to join Wi-Fi).
    4. Note every reference code (e.g. PPT004). Run it 2-3 times if the fault is intermittent.
    Extended test: after the first pass, choose "Run test again" and tick the extended option.
    Note: once this Mac is on macOS 26 (Tahoe), Command-D opens Repair Assistant instead - the same
    diagnostics live under its "Mac Resource Inspector" test, next to display/keyboard/audio tests.
EOF
  else
    good "Intel Mac from mid-2013 on -> Apple Diagnostics is built in. No download needed."
    cat <<'EOF'
    1. Shut down. Unplug everything except power, keyboard, mouse and Ethernet/Wi-Fi.
    2. Power on and IMMEDIATELY hold the D key until you see a language list / progress bar.
    3. If nothing happens (firmware chip replaced, T2 diag partition gone): hold Option-D instead
       to run Apple Diagnostics over the internet (needs a network - DHCP + open Wi-Fi/Ethernet).
    4. Note every reference code. Firmware password on? Enter it first (or clear it via Recovery).
    Tip: a Mac that boots to "Diagnosing your computer" then a folder-with-? has a dead/missing SSD.
EOF
  fi
  printf '    Then paste the codes here to decode them, or run:  Run-AppleDiagnostics.command -Code PPT004 VDH002\n'
}

if [ -z "$AHT_CODES" ]; then
  show_modern
  # T2 / AS also support Apple Diagnostics via internet; Option-D on 2011+ also runs internet AHT
  if [ "$TAHOE" = "1" ]; then
    bm_finding INFO "Diagnostics" "Repair Assistant Mac (macOS $OSV, Apple silicon)" "hold power > startup options > Cmd-D > Mac Resource Inspector / Display Anomalies / Keyboard-Trackpad-Touch ID / Audio; calibrate replaced parts via System Settings > General > About > Parts & Service > Restart & Finish Repair"
  else
    bm_finding INFO "Diagnostics" "Built-in Apple Diagnostics (no AHT image needed)" "$([ "$AS" = 1 ] && printf 'hold power > startup options > Cmd-D' || printf 'hold D at boot, or Option-D over the internet')"
  fi
  # ---- Parts & Service history (Apple silicon, macOS 26+) ----------------------
  if [ "$TAHOE" = "1" ]; then
    hdr "2b. Parts & Service history (read this before quoting or buying)"
    cat <<'EOF'
    System Settings > General > About > Parts & Service  lists every serviced part with a label:
      Genuine     - Apple part, installed and calibrated properly (Apple, AASP, or Self Service Repair + Repair Assistant)
      Used        - a genuine Apple part harvested from another Mac (fine functionally; say so on a trade-in grade)
      Unknown     - a part macOS cannot verify: third-party, or a genuine part never calibrated
                    (run "Restart & Finish Repair" - it often turns Unknown into Genuine/Used)
      Unverified  - the check could not be completed (offline, or the pairing failed) - re-run online
    Nothing listed = no serviced parts recorded (or a Mac that pre-dates the feature).
    For a trade-in: any Unknown display/battery/Touch ID part lowers the grade and can disable True Tone,
    battery health readouts or Touch ID - open the pane and photograph it for the ticket.
EOF
    bm_finding INFO "Trade-in" "Parts & Service pane available on this Mac" "System Settings > General > About > Parts & Service - record Genuine / Used / Unknown / Unverified for battery, display, Touch ID, logic board, top case"
  fi
else
  bm_finding INFO "Diagnostics" "AHT Mac - download code(s) $AHT_CODES" "image: $APPLE_URL/<code>.dmg (mirror: MaxTechnics/AHTFinder)"
  cat <<'EOF'

    Two ways to run AHT on this Mac:
      a) Option-D at boot = Internet AHT (Macs from 2011 with the firmware update; needs DHCP internet).
      b) The AHT USB this tool builds below - works on any AHT-era Mac, no internet, and it is the
         EXTENDED test (tick "Perform extended testing" - the quick test misses bad RAM).
    Some images are also inside the original install DVD's "Applications Install Disc 2".
EOF
fi

# ---- download + build the AHT stick -----------------------------------------
if [ -n "$AHT_CODES" ] && [ "$BM_QUIET" != "1" ]; then
  hdr "3. AHT image"
  mkdir -p "$IMG_DIR" 2>/dev/null
  GOT=""
  for code in $AHT_CODES; do
    f="$IMG_DIR/$code.dmg"
    if [ -s "$f" ] && hdiutil imageinfo "$f" >/dev/null 2>&1; then good "cached on the stick: $f"; GOT="$f"; break; fi
    printf 'Download %s.dmg now? [Y/n] ' "$code"; read -r -n 1 a; printf '\n'
    case "$a" in n|N) continue;; esac
    ok=0
    for u in "$APPLE_URL/$code.dmg" "$MIRROR_URL/$code.dmg"; do
      info "fetching $u"
      if curl -L --fail --progress-bar -m 900 -o "$f.part" "$u" && [ -s "$f.part" ] && hdiutil imageinfo "$f.part" >/dev/null 2>&1; then
        mv -f "$f.part" "$f"; ok=1; break
      fi
      rm -f "$f.part"; warn "not available from that source"
    done
    if [ "$ok" = "1" ]; then good "saved $f ($(du -h "$f" | cut -f1))"; GOT="$f"; break; fi
  done
  if [ -n "$GOT" ]; then
    meta aht_image "$(basename "$GOT")"
    printf '\nBuild a bootable AHT USB from this image now? (needs a SECOND USB stick, 1 GB+, it will be ERASED) [y/N] '
    read -r -n 1 a; printf '\n'
    if [ "$a" = "y" ] || [ "$a" = "Y" ]; then
      hdr "4. Build AHT USB"
      THISVOL="$(df "$APP_DIR" 2>/dev/null | tail -n1 | awk '{ print $1 }' | sed 's#/dev/##; s/s[0-9]*$//')"
      info "external physical disks (this Diagnostics stick = $THISVOL is excluded):"
      diskutil list external physical 2>/dev/null | grep -E '^/dev/disk' | sed 's#/dev/##; s/ (.*//' | grep -v "^$THISVOL$" > "$T/ext.txt"
      if [ ! -s "$T/ext.txt" ]; then warn "no other external disk found - plug the target stick in and re-run"; else
        while read -r d; do sz="$(diskutil info "$d" 2>/dev/null | grep -m1 'Disk Size' | sed 's/.*: *//; s/ (.*//')"; nm="$(diskutil info "$d" 2>/dev/null | grep -m1 'Device / Media Name' | sed 's/.*: *//')"; printf '    %-8s %-12s %s\n' "$d" "$sz" "$nm"; done < "$T/ext.txt"
        printf 'Type the disk identifier to ERASE (e.g. disk4), or Return to skip: '; read -r TGT
        if [ -n "$TGT" ] && grep -qx "$TGT" "$T/ext.txt"; then
          printf 'Last chance: ALL DATA on %s will be destroyed. Type YES to continue: ' "$TGT"; read -r yes
          if [ "$yes" = "YES" ]; then
            bm_sudo_start || { fail "admin rights are required to bless the stick"; }
            if [ "$BM_SUDO" = "1" ]; then
              info "erasing $TGT as GPT / Mac OS Extended (Journaled), volume 'AHT'..."
              diskutil unmountDisk force "/dev/$TGT" >/dev/null 2>&1
              if diskutil eraseDisk JHFS+ AHT GPT "/dev/$TGT"; then
                MP="$(hdiutil attach -nobrowse -readonly -noverify "$GOT" 2>/dev/null | grep -o '/Volumes/.*' | tail -n1)"
                if [ -z "$MP" ]; then
                  warn "image would not mount as-is (legacy format) - converting..."
                  hdiutil convert "$GOT" -format UDRO -o "$T/aht_conv.dmg" >/dev/null 2>&1 && MP="$(hdiutil attach -nobrowse -readonly -noverify "$T/aht_conv.dmg" 2>/dev/null | grep -o '/Volumes/.*' | tail -n1)"
                fi
                if [ -n "$MP" ] && [ -d "$MP/System" ]; then
                  info "copying $MP/System -> /Volumes/AHT (a few hundred MB)..."
                  if sudo -n cp -R "$MP/System" /Volumes/AHT/ && sudo -n bless --folder /Volumes/AHT/ --file "/Volumes/AHT/System/Library/CoreServices/.diagnostics/diags.efi" --label AHT; then
                    sync; hdiutil detach "$MP" >/dev/null 2>&1; diskutil eject "/dev/$TGT" >/dev/null 2>&1
                    good "AHT USB ready. On the customer Mac: hold OPTION at power-on, pick 'AHT', tick 'Perform extended testing'."
                    bm_finding INFO "Diagnostics" "Bootable AHT USB built on $TGT" "image $(basename "$GOT")"
                  else
                    fail "copy or bless failed - see above"; hdiutil detach "$MP" >/dev/null 2>&1
                  fi
                else
                  fail "could not mount the image or it has no System folder"; [ -n "$MP" ] && hdiutil detach "$MP" >/dev/null 2>&1
                fi
              else fail "erase failed"; fi
            fi
          else info "skipped"; fi
        else info "no valid disk chosen - skipped"; fi
      fi
    fi
  fi
fi

# ---- decode codes interactively ---------------------------------------------
if [ "$BM_QUIET" != "1" ]; then
  hdr "Reference codes"
  printf 'Paste the reference code(s) Apple Diagnostics showed (space separated), or Return to skip: '
  read -r line
  for c in $line; do decode_code "$c"; done
fi

bm_write_reports "$META" /dev/null "$([ -n "$AHT_CODES" ] && printf 'AHT MAC' || { [ "$TAHOE" = "1" ] && printf 'REPAIR ASSISTANT MAC' || printf 'APPLE DIAGNOSTICS MAC'; })"
bm_pause
exit 0
