#!/usr/bin/env bash
# Cross-compile the Restore Doctor engine for 64-bit Windows from Linux.
#
# Prebuilt Windows dependencies (curl, libzip, openssl, zlib and theirs) come
# from MSYS2's mingw64 repository; the libimobiledevice stack is built from
# current source with the mingw-w64 cross compiler. Same result as running
# build_engine.sh inside MSYS2 on Windows, without needing a Windows machine.
#
#   sudo apt install gcc-mingw-w64-x86-64 mingw-w64-tools zstd autoconf automake libtool pkg-config
#   ./tools/cross_build_engine.sh            # -> ./engine-win64/
#
# Environment: WORK (scratch dir), OUT (result dir), REF (git ref, default master)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="${WORK:-$ROOT/build/cross}"
OUT="${OUT:-$ROOT/engine-win64}"
REF="${REF:-master}"
HOST=x86_64-w64-mingw32
SYSROOT="$WORK/sysroot"
JOBS="${JOBS:-$(nproc)}"

REPOS=(libplist libimobiledevice-glue libusbmuxd libtatsu libimobiledevice libirecovery idevicerestore)
TOOLS=(idevicerestore irecovery ideviceinfo idevice_id idevicecrashreport)
say() { printf '\n==> %s\n' "$*"; }

mkdir -p "$WORK/dl" "$SYSROOT"
say "Fetching Windows dependencies from MSYS2"
curl -fsS -m 120 -o "$WORK/dl/mingw64.db" https://repo.msys2.org/mingw/mingw64/mingw64.db
python3 "$HERE/msys2_sysroot.py" "$WORK/dl/mingw64.db" "$WORK/dl" "$SYSROOT" curl libzip openssl zlib

export PKG_CONFIG=pkg-config
export PKG_CONFIG_LIBDIR="$SYSROOT/mingw64/lib/pkgconfig"
export PKG_CONFIG_PATH=
export PKG_CONFIG_SYSROOT_DIR="$SYSROOT"
export CPPFLAGS="-I$SYSROOT/mingw64/include"

# Windows is case-insensitive about library names; the Linux linker isn't.
# Some projects link e.g. -lIphlpapi, which mingw ships as libiphlpapi.a.
CASEFIX="$WORK/casefix"
mkdir -p "$CASEFIX"
MINGW_LIB="$(dirname "$(x86_64-w64-mingw32-gcc -print-file-name=libkernel32.a)")"
for name in Iphlpapi Ws2_32 Setupapi Shlwapi Advapi32 Crypt32 Bcrypt Userenv Ole32 Shell32 Uuid; do
  lower="$(echo "$name" | tr 'A-Z' 'a-z')"
  [ -f "$MINGW_LIB/lib$lower.a" ] && ln -sf "$MINGW_LIB/lib$lower.a" "$CASEFIX/lib$name.a"
done
export LDFLAGS_BASE="-L$SYSROOT/mingw64/lib -L$CASEFIX -static-libgcc"

: > "$WORK/VERSIONS.txt"
for repo in "${REPOS[@]}"; do
  src="$WORK/src/$repo"
  if [ ! -d "$src/.git" ]; then
    say "Cloning $repo"
    git clone --quiet "https://github.com/libimobiledevice/$repo.git" "$src"
  fi
  git -C "$src" fetch --quiet --tags origin
  git -C "$src" checkout --quiet "$REF"
  git -C "$src" pull --quiet --ff-only origin "$REF" 2>/dev/null || true
  echo "$repo $(git -C "$src" describe --tags --always 2>/dev/null) $(git -C "$src" log -1 --format='%h %cs')" >> "$WORK/VERSIONS.txt"

  extra=()
  ldflags="$LDFLAGS_BASE"
  case "$repo" in
    libplist) extra+=(--without-cython --without-tests) ;;
    libimobiledevice) extra+=(--without-cython --without-readline --disable-wireless-pairing) ;;
    idevicerestore) ldflags="$ldflags -Wl,--stack,67108864" ;;   # restore path needs a big stack
  esac

  say "Building $repo"
  (
    cd "$src"
    [ -f Makefile ] && make distclean >/dev/null 2>&1 || true
    NOCONFIGURE=1 ./autogen.sh >/dev/null 2>&1
    LDFLAGS="$ldflags" ./configure --host=$HOST --prefix=/mingw64 --enable-shared --disable-static \
      "${extra[@]}" >"$WORK/$repo.configure.log" 2>&1 || { tail -30 "$WORK/$repo.configure.log"; exit 1; }
    make -j"$JOBS" >"$WORK/$repo.make.log" 2>&1 || { tail -40 "$WORK/$repo.make.log"; exit 1; }
    make install DESTDIR="$SYSROOT" >/dev/null
  )
  # libtool archives point at /mingw64 paths that only exist inside the sysroot
  find "$SYSROOT/mingw64/lib" -name '*.la' -delete
done

say "Collecting the engine into $OUT"
rm -rf "$OUT"
mkdir -p "$OUT"
for t in "${TOOLS[@]}"; do
  cp "$SYSROOT/mingw64/bin/$t.exe" "$OUT/"
done

# Copy every DLL the tools need, following imports recursively.
need_dlls() { x86_64-w64-mingw32-objdump -p "$1" | awk '/DLL Name:/ {print $3}'; }
queue=("${TOOLS[@]/%/.exe}")
declare -A done_
while [ ${#queue[@]} -gt 0 ]; do
  f="${queue[0]}"; queue=("${queue[@]:1}")
  for dll in $(need_dlls "$OUT/$f"); do
    key="${dll,,}"
    [ -n "${done_[$key]:-}" ] && continue
    done_[$key]=1
    found=""
    for dir in "$SYSROOT/mingw64/bin" /usr/lib/gcc/$HOST/*-posix /usr/lib/gcc/$HOST/*-win32 /usr/$HOST/lib; do
      cand=$(find $dir -maxdepth 1 -iname "$dll" 2>/dev/null | head -1)
      if [ -n "$cand" ]; then found="$cand"; break; fi
    done
    if [ -n "$found" ]; then
      cp -n "$found" "$OUT/"
      queue+=("$(basename "$found")")
    fi   # not found = a Windows system DLL
  done
done

cp "$SYSROOT/mingw64/etc/ssl/certs/ca-bundle.crt" "$OUT/curl-ca-bundle.crt" 2>/dev/null || true
cp "$WORK/VERSIONS.txt" "$OUT/VERSIONS.txt"
say "Engine ready in $OUT"
ls -1 "$OUT"
cat "$OUT/VERSIONS.txt"
