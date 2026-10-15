#!/usr/bin/env bash
# Build the Restore Doctor engine from current libimobiledevice source.
#
#   Windows: open the "MSYS2 MINGW64" shell, cd to this folder, run:
#              ./build_engine.sh
#            Result: ..\engine\ with idevicerestore.exe, irecovery.exe,
#            ideviceinfo.exe, idevice_id.exe and every DLL they need.
#   Linux:   works the same (used for testing the app's parser against
#            real engine output).
#
# Why from source: the packaged MSYS2 idevicerestore is still 1.0.0, which
# predates current iPhones. libirecovery's device table must be current for
# the newest models to be recognised at all.
#
# Environment overrides:
#   REF=<branch|tag>   git ref to build for every repo (default: master)
#   WORK=<dir>         where sources and the install prefix go
#   OUT=<dir>          where the finished engine folder is written
#   SKIP_DEPS=1        don't run pacman (Windows) - you installed deps already
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="${WORK:-$ROOT/build/engine-src}"
OUT="${OUT:-$ROOT/engine}"
PREFIX="${PREFIX:-$WORK/prefix}"
REF="${REF:-master}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"

# Build order matters: each one needs the ones before it.
REPOS=(libplist libimobiledevice-glue libusbmuxd libtatsu libimobiledevice libirecovery idevicerestore)
TOOLS=(idevicerestore irecovery ideviceinfo idevice_id idevicecrashreport)

case "$(uname -s)" in
  MINGW*|MSYS*) WINDOWS=1; EXE=.exe ;;
  *)            WINDOWS=0; EXE= ;;
esac

say() { printf '\n==> %s\n' "$*"; }

if [ "$WINDOWS" = 1 ] && [ "${SKIP_DEPS:-0}" != 1 ]; then
  if [ "${MSYSTEM:-}" != "MINGW64" ]; then
    echo "Run this from the 'MSYS2 MINGW64' shell (MSYSTEM is '${MSYSTEM:-unset}')." >&2
    exit 1
  fi
  say "Installing build dependencies with pacman"
  pacman -S --needed --noconfirm \
    base-devel git make libtool autoconf automake-wrapper \
    mingw-w64-x86_64-gcc mingw-w64-x86_64-pkgconf \
    mingw-w64-x86_64-curl mingw-w64-x86_64-libzip \
    mingw-w64-x86_64-openssl mingw-w64-x86_64-zlib
fi

mkdir -p "$WORK" "$PREFIX"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
export PATH="$PREFIX/bin:$PATH"
export LD_LIBRARY_PATH="$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

VERSIONS="$WORK/VERSIONS.txt"
: > "$VERSIONS"

for repo in "${REPOS[@]}"; do
  src="$WORK/$repo"
  if [ ! -d "$src/.git" ]; then
    say "Cloning $repo"
    git clone --quiet "https://github.com/libimobiledevice/$repo.git" "$src"
  fi
  git -C "$src" fetch --quiet --tags origin
  git -C "$src" checkout --quiet "$REF"
  git -C "$src" pull --quiet --ff-only origin "$REF" 2>/dev/null || true
  echo "$repo $(git -C "$src" log -1 --format='%h %cs')" >> "$VERSIONS"

  extra=()
  ldflags="${LDFLAGS:-}"
  case "$repo" in
    libplist) extra+=(--without-cython) ;;
    libimobiledevice)
      # only what a restore needs: no Python bindings, no readline tools shell,
      # no Wi-Fi pairing
      extra+=(--without-cython --without-readline --disable-wireless-pairing) ;;
    idevicerestore)
      # idevicerestore's restore path uses a lot of stack; Windows' default
      # 1 MB main-thread stack is not enough. Reserve 64 MB.
      if [ "$WINDOWS" = 1 ]; then ldflags="$ldflags -Wl,--stack,67108864"; fi ;;
  esac

  say "Building $repo"
  (
    cd "$src"
    [ -f Makefile ] && make distclean >/dev/null 2>&1 || true
    NOCONFIGURE=1 ./autogen.sh >/dev/null
    LDFLAGS="$ldflags" ./configure --prefix="$PREFIX" --disable-static --enable-shared "${extra[@]}" >/dev/null
    make -j"$JOBS" >/dev/null
    make install >/dev/null
  )
done

say "Collecting the engine into $OUT"
rm -rf "$OUT"
mkdir -p "$OUT"
for t in "${TOOLS[@]}"; do
  cp "$PREFIX/bin/$t$EXE" "$OUT/"
done
cp "$VERSIONS" "$OUT/VERSIONS.txt"

if [ "$WINDOWS" = 1 ]; then
  # Copy every non-system DLL the tools load (ours from $PREFIX/bin, the rest
  # from /mingw64/bin). ldd lists the full chain.
  for t in "${TOOLS[@]}"; do
    ldd "$OUT/$t.exe" | awk '{print $3}' | grep -iE '^/(mingw64|.*prefix)/' || true
  done | sort -u | while read -r dll; do
    cp -n "$dll" "$OUT/" 2>/dev/null || true
  done
  # libcurl needs a CA bundle to reach Apple's signing server over HTTPS.
  if [ -f /mingw64/etc/ssl/certs/ca-bundle.crt ]; then
    cp /mingw64/etc/ssl/certs/ca-bundle.crt "$OUT/curl-ca-bundle.crt"
  fi
else
  # Linux test build: keep the shared libs next to the tools.
  mkdir -p "$OUT/lib"
  cp -a "$PREFIX"/lib/*.so* "$OUT/lib/"
fi

say "Engine ready:"
ls -1 "$OUT"
cat "$OUT/VERSIONS.txt"
