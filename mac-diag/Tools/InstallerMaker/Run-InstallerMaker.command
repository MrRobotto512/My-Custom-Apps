#!/bin/bash
#  macOS Installer Maker  (BenchTool Mac Diagnostics)
#  ============================================================================
#  Turns a USB stick into a bootable macOS installer - one version, or SEVERAL
#  versions on one stick (one partition each, like MultiMac) so a single
#  32/64/128 GB drive covers every Mac that walks in. Also downloads full
#  installers from Apple and converts an installer to an .iso for VMs.
#
#  It runs ON A MAC (any Mac, 10.13+ recommended) because only macOS ships
#  Apple's createinstallmedia. Take this stick to any working Mac on the bench.
#
#   1. Build USB     pick installers found in /Applications (and any
#                    InstallAssistant.pkg / Install macOS *.app you dropped in
#                    this folder's Installers/), pick the target stick, go.
#                    Single installer = whole stick. Several = GPT partitions
#                    sized per version, remainder to the last one.
#   2. Download      softwareupdate --list-full-installers -> choose -> Apple
#                    downloads "Install macOS X.app" into /Applications
#                    (macOS 10.15.4+ on the bench Mac; older = use the App
#                    Store links printed on screen).
#   3. Make ISO      installer -> sparse image -> createinstallmedia -> .iso
#                    (for VMware / Parallels / UTM / Proxmox), saved to ISO/
#                    on this stick or ~/Desktop.
#
#  Safety: only EXTERNAL PHYSICAL disks are offered, this Diagnostics stick is
#  excluded, and you must type the disk identifier + YES. Internal disks are
#  never touched.
#
#  Switches:  -Quiet (no pause at the end)     -List (just list installers)
#
#  Inspired by KantBStoppd/MultiMac (MIT) - same eraseDisk / partitionDisk /
#  createinstallmedia flow, rewritten as a plain bash tool for the stick.
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR" || exit 1
. "$APP_DIR/../_shared/benchmac.sh" || { echo "missing _shared/benchmac.sh"; exit 1; }

LISTONLY=0
bm_parse_common "$@"
set -- $BM_REST
while [ $# -gt 0 ]; do case "$1" in -List|-list) LISTONLY=1;; esac; shift; done

bm_init "InstallerMaker" "1.0"
T="$BM_TMP"
STICK_INST="$APP_DIR/Installers"; ISO_DIR="$APP_DIR/ISO"
mkdir -p "$STICK_INST" 2>/dev/null
OSV="$(bm_os_version)"
AS=0; bm_is_apple_silicon && AS=1
info "bench Mac: $(sysctl -n hw.model 2>/dev/null)  macOS $OSV  $([ "$AS" = 1 ] && printf 'Apple silicon' || printf 'Intel')"

# ---- installer discovery ----------------------------------------------------
# INST list file: path US name US version US size_gb_partition
INST="$T/installers.txt"
# Partition size (GB) createinstallmedia needs per macOS major - same table MultiMac ships.
part_gb() {
  case "$1" in
    10.12*|10.13*) printf 16 ;; 10.14*|10.15*) printf 20 ;; 11*|12*) printf 25 ;; *) printf 30 ;;
  esac
}
installer_version() {   # prints the macOS version the installer app installs
  local app="$1" v
  v="$(bm_plist "$app/Contents/Info.plist" DTPlatformVersion)"
  [ -n "$v" ] || v="$(bm_plist "$app/Contents/SharedSupport/InstallInfo.plist" 'System Image Info:version')"
  [ -n "$v" ] || v="$(bm_plist "$app/Contents/Info.plist" CFBundleShortVersionString)"
  printf '%s' "$v"
}
scan_installers() {
  : > "$INST"
  local app nm v
  for app in /Applications/Install\ macOS*.app /Applications/Install\ OS\ X*.app "$HOME"/Applications/Install\ macOS*.app "$STICK_INST"/Install\ macOS*.app "$STICK_INST"/Install\ OS\ X*.app; do
    [ -d "$app" ] || continue
    [ -x "$app/Contents/Resources/createinstallmedia" ] || { warn "$(basename "$app") has no createinstallmedia (stub/incomplete download?) - skipped"; continue; }
    nm="$(basename "$app" .app | sed 's/^Install //')"; v="$(installer_version "$app")"
    printf '%s%s%s%s%s%s%s\n' "$app" "$BM_US" "$nm" "$BM_US" "$v" "$BM_US" "$(part_gb "$v")" >> "$INST"
  done
}
list_installers() {
  scan_installers
  if [ ! -s "$INST" ]; then warn "no 'Install macOS' apps found in /Applications (use option 2 to download one)"; return 1; fi
  local i=0
  while IFS="$BM_US" read -r app nm v gb; do
    i=$((i+1)); printf '   %2d) %-28s %-10s %s\n' "$i" "$nm" "$v" "$app"
  done < "$INST"
  return 0
}
pick_installers() {   # sets PICKED (newline separated rows) from "1 3" style input
  PICKED=""
  local sel n row
  printf 'Installer number(s), space separated (e.g. 1 or 1 3): '; read -r sel
  for n in $sel; do
    row="$(sed -n "${n}p" "$INST" 2>/dev/null)"
    [ -n "$row" ] && PICKED="${PICKED}${row}
"
  done
  [ -n "$PICKED" ]
}
# InstallAssistant.pkg dropped on the stick -> install to get the app
offer_pkgs() {
  local p
  for p in "$STICK_INST"/*.pkg; do
    [ -f "$p" ] || continue
    printf 'Found %s on the stick - install it to /Applications now? [y/N] ' "$(basename "$p")"; read -r -n 1 a; printf '\n'
    case "$a" in y|Y) bm_sudo_start && caffeinate -i sudo -n installer -pkg "$p" -target / && good "installed - the Install macOS app is now in /Applications";; esac
  done
}

# ---- target disk ------------------------------------------------------------
pick_target() {   # sets TGT (diskN) or empty
  TGT=""
  local thisvol d sz nm
  thisvol="$(df "$APP_DIR" 2>/dev/null | tail -n1 | awk '{ print $1 }' | sed 's#/dev/##; s/s[0-9]*$//')"
  diskutil list external physical 2>/dev/null | grep -E '^/dev/disk' | sed 's#/dev/##; s/ (.*//' | grep -v "^$thisvol$" > "$T/ext.txt"
  if [ ! -s "$T/ext.txt" ]; then warn "no other external disk found - plug in the target USB and try again (this Diagnostics stick $thisvol is excluded)"; return 1; fi
  info "external physical disks (this Diagnostics stick = $thisvol is excluded):"
  while read -r d; do
    sz="$(diskutil info "$d" 2>/dev/null | grep -m1 'Disk Size' | sed 's/.*: *//; s/ (.*//')"
    nm="$(diskutil info "$d" 2>/dev/null | grep -m1 'Device / Media Name' | sed 's/.*: *//')"
    printf '    %-8s %-12s %s\n' "$d" "$sz" "$nm"
  done < "$T/ext.txt"
  printf 'Type the disk identifier to ERASE (e.g. disk4), or Return to cancel: '; read -r TGT
  [ -n "$TGT" ] || return 1
  grep -qx "$TGT" "$T/ext.txt" || { fail "$TGT is not one of the external disks above"; TGT=""; return 1; }
  TGT_BYTES="$(diskutil info "$TGT" 2>/dev/null | grep -m1 'Disk Size' | sed 's/.*(\([0-9]*\) Bytes.*/\1/')"
  return 0
}

# ---- build ------------------------------------------------------------------
do_build() {
  hdr "Build USB installer"
  offer_pkgs
  list_installers || return 1
  pick_installers || { warn "nothing picked"; return 1; }
  local n; n="$(printf '%s' "$PICKED" | grep -c .)"
  local need=0 gb
  printf '%s' "$PICKED" | while IFS="$BM_US" read -r app nm v gb; do [ -n "$app" ] && printf '    + %s (%s) -> %s GB partition\n' "$nm" "$v" "$gb"; done
  need="$(printf '%s' "$PICKED" | awk -F"$BM_US" '{ s+=$4 } END { print s+0 }')"
  pick_target || return 1
  local have_gb; have_gb="$(awk -v b="$TGT_BYTES" 'BEGIN{ printf "%d", b/1000000000 }')"
  if [ "$n" -eq 1 ]; then
    info "single installer: the whole ${have_gb} GB stick will be used (needs 16 GB+)"
    [ "$have_gb" -lt 15 ] && { fail "stick too small"; return 1; }
  else
    info "$n installers need about ${need} GB; stick is ${have_gb} GB"
    [ "$have_gb" -lt "$need" ] && { fail "stick too small - drop an installer or use a bigger stick"; return 1; }
  fi
  printf 'Last chance: ALL DATA on %s will be destroyed. Type YES to continue: ' "$TGT"; read -r yes
  [ "$yes" = "YES" ] || { info "cancelled"; return 1; }
  bm_sudo_start || { fail "createinstallmedia needs administrator rights"; return 1; }
  diskutil unmountDisk force "/dev/$TGT" >/dev/null 2>&1
  local i=0 vols=""
  if [ "$n" -eq 1 ]; then
    info "erasing $TGT as GPT / JHFS+ 'Installer'..."
    diskutil eraseDisk JHFS+ Installer GPT "/dev/$TGT" || { fail "erase failed"; return 1; }
    vols="Installer"
  else
    # partitionDisk disk GPT JHFS+ Name1 25g JHFS+ Name2 R   (last gets the remainder)
    local args="" k=0
    while IFS="$BM_US" read -r app nm v gb; do
      [ -n "$app" ] || continue
      k=$((k+1)); local vn; vn="INST$k"
      if [ "$k" -eq "$n" ]; then args="$args JHFS+ $vn R"; else args="$args JHFS+ $vn ${gb}g"; fi
      vols="$vols $vn"
    done <<EOF
$PICKED
EOF
    info "partitioning $TGT: $args"
    # shellcheck disable=SC2086
    diskutil partitionDisk "/dev/$TGT" GPT $args || { fail "partitioning failed"; return 1; }
  fi
  sleep 2
  i=0
  local rc=0
  while IFS="$BM_US" read -r app nm v gb; do
    [ -n "$app" ] || continue
    i=$((i+1)); local vn; vn="$(printf '%s' "$vols" | awk -v i="$i" '{ print $i }')"
    local vol="/Volumes/$vn"
    [ -d "$vol" ] || { fail "$vol not mounted"; rc=1; continue; }
    hdr "createinstallmedia: $nm ($v) -> $vol"
    info "this takes 5-20 minutes per installer; the volume gets renamed to 'Install $nm'"
    local extra=""
    case "$v" in 10.12*|10.11*|10.10*|10.9*) extra="--applicationpath" ;; esac
    if [ -n "$extra" ]; then
      caffeinate -i sudo -n "$app/Contents/Resources/createinstallmedia" --volume "$vol" --applicationpath "$app" --nointeraction
    else
      caffeinate -i sudo -n "$app/Contents/Resources/createinstallmedia" --volume "$vol" --nointeraction
    fi
    if [ $? -eq 0 ]; then good "$nm done"; else fail "$nm failed (see above)"; rc=1; fi
  done <<EOF
$PICKED
EOF
  sync
  if [ "$rc" -eq 0 ]; then
    good "USB installer ready on $TGT. Intel: hold OPTION at power-on and pick it. Apple silicon: hold the power button > startup options > pick it."
    [ "$AS" = "1" ] && dim "Apple silicon Macs can only boot an installer at or above the macOS version they shipped with."
    diskutil eject "/dev/$TGT" >/dev/null 2>&1 && dim "ejected"
  fi
  return $rc
}

# ---- download ---------------------------------------------------------------
do_download() {
  hdr "Download a full installer from Apple"
  if ! bm_ver_ge "$OSV" 10.15.4; then
    warn "softwareupdate --fetch-full-installer needs macOS 10.15.4+ on THIS Mac (it has $OSV)."
    cat <<'EOF'
    Use a newer bench Mac, or Apple's App Store pages (open on any Mac):
      Tahoe 26 / Sequoia 15 / Sonoma 14 / Ventura 13 / Monterey 12 / Big Sur 11 / Catalina 10.15 / Mojave 10.14 / High Sierra 10.13
      -> support.apple.com/102662  "How to download and install macOS"  (links to every installer)
    Drop the resulting InstallAssistant.pkg or Install macOS *.app into this folder's Installers/ and re-run.
EOF
    return 1
  fi
  bm_online || { fail "no internet"; return 1; }
  info "asking Apple for the installer list (~30 s)..."
  bm_timeout 120 softwareupdate --list-full-installers 2>&1 | grep -E '^\* Title' > "$T/full.txt"
  if [ ! -s "$T/full.txt" ]; then fail "no list returned (network / catalog problem)"; return 1; fi
  local i=0
  sed -E 's/^\* Title: ([^,]*), Version: ([^,]*), Size: ([0-9]*)KiB(, Build: ([^,]*))?.*/\1|\2|\3|\5/' "$T/full.txt" > "$T/full2.txt"
  while IFS='|' read -r ttl ver kib bld; do
    i=$((i+1)); printf '   %2d) %-22s %-10s %6.1f GB  %s\n' "$i" "$ttl" "$ver" "$(awk -v k="$kib" 'BEGIN{ printf "%.1f", k/1048576 }')" "$bld"
  done < "$T/full2.txt"
  printf 'Number to download (Return to cancel): '; read -r n
  [ -n "$n" ] || return 1
  local row; row="$(sed -n "${n}p" "$T/full2.txt")"; [ -n "$row" ] || { fail "bad choice"; return 1; }
  local ver; ver="$(printf '%s' "$row" | cut -d'|' -f2)"
  info "downloading macOS $ver to /Applications (12-16 GB, Apple's CDN, 10-40 min)..."
  if caffeinate -i softwareupdate --fetch-full-installer --full-installer-version "$ver"; then
    good "done - it is now in /Applications as 'Install macOS ...'. Optional: copy it into this stick's Installers/ to carry it around."
  else
    fail "download failed - Apple's catalog sometimes refuses versions newer than this Mac supports; try another version"
  fi
}

# ---- ISO --------------------------------------------------------------------
do_iso() {
  hdr "Make a .iso for virtual machines"
  list_installers || return 1
  printf 'Installer number: '; read -r n
  local row; row="$(sed -n "${n}p" "$INST" 2>/dev/null)"; [ -n "$row" ] || { fail "bad choice"; return 1; }
  local app nm v gb; app="$(printf '%s' "$row" | cut -d"$BM_US" -f1)"; nm="$(printf '%s' "$row" | cut -d"$BM_US" -f2)"; v="$(printf '%s' "$row" | cut -d"$BM_US" -f3)"; gb="$(printf '%s' "$row" | cut -d"$BM_US" -f4)"
  local out="$ISO_DIR"; mkdir -p "$out" 2>/dev/null; ( : > "$out/.w" ) 2>/dev/null && rm -f "$out/.w" || out="$HOME/Desktop"
  local safe; safe="$(printf '%s' "$nm" | tr ' ' '_')"
  local sparse="$T/$safe.sparseimage" mnt="/Volumes/iso_build" iso="$out/macOS_$safe.iso"
  info "building $iso (needs ~${gb} GB free in ${TMPDIR:-/tmp} and on the destination)"
  bm_sudo_start || { fail "createinstallmedia needs administrator rights"; return 1; }
  hdiutil create -o "$sparse" -size "${gb}g" -layout SPUD -fs 'HFS+J' -type SPARSE >/dev/null || { fail "hdiutil create failed"; return 1; }
  local dev; dev="$(hdiutil attach "$sparse" -noverify -nobrowse -mountpoint "$mnt" 2>/dev/null | awk '/^\/dev\/disk/ { print $1; exit }')"
  [ -n "$dev" ] || { fail "attach failed"; return 1; }
  case "$v" in 10.12*|10.11*|10.10*|10.9*) caffeinate -i sudo -n "$app/Contents/Resources/createinstallmedia" --volume "$mnt" --applicationpath "$app" --nointeraction ;;
               *) caffeinate -i sudo -n "$app/Contents/Resources/createinstallmedia" --volume "$mnt" --nointeraction ;; esac
  local rc=$?
  # createinstallmedia renames the volume - detach the image by its device, not its name
  sleep 2; hdiutil detach "$dev" -force >/dev/null 2>&1 || { sleep 5; hdiutil detach "$dev" -force >/dev/null 2>&1; }
  [ "$rc" -eq 0 ] || { fail "createinstallmedia failed"; rm -f "$sparse"; return 1; }
  info "converting to ISO (UDTO)..."
  rm -f "$iso" "$iso.cdr"
  if hdiutil convert "$sparse" -format UDTO -o "$iso" >/dev/null; then
    [ -f "$iso.cdr" ] && mv -f "$iso.cdr" "$iso"
    rm -f "$sparse"; good "ISO: $iso ($(du -h "$iso" | cut -f1))"
  else fail "convert failed"; rm -f "$sparse"; return 1; fi
}

# ---- menu -------------------------------------------------------------------
if [ "$LISTONLY" = "1" ]; then list_installers; exit $?; fi
while true; do
  hdr "macOS Installer Maker"
  list_installers >/dev/null 2>&1 && { info "installers on this Mac / stick:"; list_installers; }
  cat <<'EOF'

   1) Build a bootable USB installer (one or several macOS versions on one stick)
   2) Download a full installer from Apple (into /Applications)
   3) Make a .iso from an installer (for VMware / Parallels / UTM)
   4) Quit
EOF
  printf 'Choice: '; read -r c
  case "$c" in
    1) do_build ;;
    2) do_download ;;
    3) do_iso ;;
    4|q|Q|"") break ;;
    *) warn "1-4" ;;
  esac
done
bm_pause
exit 0
