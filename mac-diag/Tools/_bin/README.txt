Tools/_bin - third-party binaries shipped on the Mac Diagnostics stick
=====================================================================

smartctl   smartmontools 7.4 (2023-08-01), universal macOS binary (x86_64 + arm64)
           Source of this build: github.com/CodingMarkus/smartmontools_macos_release
           (tag RELEASE_7_4, asset smartctl.tar.xz). Upstream: www.smartmontools.org
           Licence: GNU GPL v2 (see COPYING). Redistributed unmodified; no BenchTool
           code links against it - the scripts call it as a separate process.
           sha256 7c4b0be87b6be373711665f22ac9f03fef1c5589f651518ad0af7988fb21b52b

How the stick uses it
  The stick is exFAT, which has no execute bit, so benchmac.sh (bm_smartctl)
  copies the binary into the run's temp folder, chmod +x, clears xattrs and
  ad-hoc signs it if macOS still refuses to run it. It needs the admin
  password (sudo) to read a drive's SMART log.
    MacDiag        -> storage section: Percentage Used / Available Spare /
                      Media and Data Integrity Errors / TB written / power-on
                      hours (NVMe), reallocated / pending / CRC (ATA), scored
                      HIGH / MED / INFO; -SelfTest runs a short self-test.
    Dashboard      -> collector.sh runs it every ~5 min; the Disks page shows
                      a wear bar + REPLACE / WATCH / HEALTHY per drive.
  `diskutil info` only reports Verified / Failing, and roughly a quarter of
  dying SSDs never trip that flag - these counters are the ones that predict
  a failure.
