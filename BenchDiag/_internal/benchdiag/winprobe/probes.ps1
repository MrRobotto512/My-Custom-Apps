# BenchDiag — one batched hardware probe for the in-OS Windows build.
#
# Every cheap CIM/registry query the diagnostic suite needs, collected in ONE
# PowerShell launch and emitted as a single JSON document on stdout. The Python
# side (benchdiag/winprobe.py) caches that blob and answers ~40 hwexec probe
# keys out of it, reshaping each one into exactly the form the Linux tool it
# replaces would have produced — so the parsers and tests never learn which OS
# they are on.
#
# Why one script instead of a call per probe: launching powershell.exe costs
# ~250-400 ms. Thirty of those is fifteen seconds of a tech standing there.
#
# Every section is individually wrapped: a machine with no battery, no TPM or
# no Wi-Fi must still produce a complete document for everything else. A
# section that fails contributes null and an entry in "errors", never an
# exception — a probe layer that throws is a diagnostic tool that can't
# diagnose the interesting machines.
#
# Run as: powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File probes.ps1

$ErrorActionPreference = 'Stop'

# Emit UTF-8 whether we are piped into Python, redirected to a file, or run by
# hand. Windows PowerShell 5.1 otherwise hands a pipe the OEM codepage and a
# redirect UTF-16LE, and the first device name with a (R) or an accent in it
# then arrives mangled or unparseable. Both lines are needed: OutputEncoding
# governs the pipe, [Console]::OutputEncoding the console/redirect.
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}
try { $OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}
$out = [ordered]@{ schema = 1; collected = (Get-Date).ToString('o'); errors = @() }

function Section($name, $block) {
    try { $out[$name] = & $block }
    catch {
        $out[$name] = $null
        $out.errors += ('{0}: {1}' -f $name, $_.Exception.Message)
    }
}
# CIM that must not throw when the class is absent on this SKU
function TryCim($class, $ns = 'root/cimv2') {
    try { Get-CimInstance -ClassName $class -Namespace $ns -ErrorAction Stop } catch { $null }
}
# Dates arrive as [datetime] from CIM but as a plain string from some
# Get-NetAdapter builds, where .ToString('yyyy-MM-dd') is an overload error.
function DateStr($v) {
    if ($null -eq $v) { return $null }
    if ($v -is [datetime]) { return $v.ToString('yyyy-MM-dd') }
    try { return ([datetime]$v).ToString('yyyy-MM-dd') } catch { return [string]$v }
}

Section 'system' {
    $cs = TryCim Win32_ComputerSystem; $bios = TryCim Win32_BIOS; $bb = TryCim Win32_BaseBoard
    $os = TryCim Win32_OperatingSystem
    [ordered]@{
        manufacturer = $cs.Manufacturer; model = $cs.Model
        serial       = $bios.SerialNumber
        bios         = $bios.SMBIOSBIOSVersion
        bios_date    = DateStr $bios.ReleaseDate
        bios_vendor  = $bios.Manufacturer
        baseboard    = ('{0} {1}' -f $bb.Manufacturer, $bb.Product).Trim()
        os_caption   = $os.Caption; os_build = $os.BuildNumber; os_version = $os.Version
        os_arch      = $os.OSArchitecture
        installed_on = DateStr $os.InstallDate
        last_boot    = DateStr $os.LastBootUpTime
        computer     = $env:COMPUTERNAME
        # which volume Windows is actually on. The Health page used to open
        # whichever NTFS volume sorted first — a 5 TB games drive, as often as
        # not — and then truthfully report "no Windows installation on this
        # volume" about a machine that was running Windows at the time.
        system_drive = $env:SystemDrive
        windir       = $env:SystemRoot
    }
}

Section 'cpu' {
    $p = @(TryCim Win32_Processor)[0]
    # Feature bits CIM does not expose; IsProcessorFeaturePresent() is the
    # documented source and these ordinals are the ones the suite reports on.
    $sig = '[DllImport("kernel32.dll")] public static extern bool IsProcessorFeaturePresent(uint f);'
    $k = Add-Type -MemberDefinition $sig -Name BdFeat -Namespace BdWin -PassThru -ErrorAction SilentlyContinue
    $feat = @()
    if ($k) {
        $map = @{ 'sse' = 6; 'sse2' = 10; 'sse3' = 13; 'sse4_1' = 37; 'sse4_2' = 38; 'avx' = 39; 'avx2' = 40; 'avx512f' = 41 }
        foreach ($n in $map.Keys) { if ($k::IsProcessorFeaturePresent($map[$n])) { $feat += $n } }
        # POPCNT ships with SSE4.2 on every part the suite cares about
        if ($feat -contains 'sse4_2') { $feat += 'popcnt' }
    }
    [ordered]@{
        name = $p.Name; cores = $p.NumberOfCores; threads = $p.NumberOfLogicalProcessors
        mhz = $p.MaxClockSpeed; arch = $env:PROCESSOR_ARCHITECTURE
        l2_kb = $p.L2CacheSize; l3_kb = $p.L3CacheSize
        sockets = @(TryCim Win32_Processor).Count
        features = $feat
    }
}

Section 'memory' {
    $os = TryCim Win32_OperatingSystem
    $mods = @(TryCim Win32_PhysicalMemory | ForEach-Object {
            [ordered]@{ bank = $_.BankLabel; slot = $_.DeviceLocator; size = $_.Capacity
                speed  = $_.ConfiguredClockSpeed; mfr = $_.Manufacturer; part = $_.PartNumber
            }
        })
    [ordered]@{
        total_kb = $os.TotalVisibleMemorySize; free_kb = $os.FreePhysicalMemory
        # Windows has no direct MemAvailable; free + standby is the closest
        # honest analogue, and the suite only uses it for a headroom figure.
        available_kb = $os.FreePhysicalMemory
        modules = $mods
    }
}

Section 'cpu_load' {
    @(TryCim Win32_PerfFormattedData_PerfOS_Processor | Where-Object { $_.Name -ne '_Total' } |
        ForEach-Object { [ordered]@{ id = $_.Name; pct = [double]$_.PercentProcessorTime } })
}

Section 'disks' {
    $parts = @(TryCim Win32_DiskPartition)
    $la = @(TryCim Win32_LogicalDiskToPartition)
    @(TryCim Win32_DiskDrive | Sort-Object Index | ForEach-Object {
            $d = $_
            $idx = $d.Index
            $phys = $null
            try { $phys = Get-PhysicalDisk -DeviceNumber $idx -ErrorAction Stop } catch {}
            $myparts = @($parts | Where-Object { $_.DiskIndex -eq $idx } | ForEach-Object {
                    $pt = $_
                    $letter = $null
                    foreach ($l in $la) {
                        if ($l.Antecedent.DeviceID -eq $pt.DeviceID) { $letter = $l.Dependent.DeviceID }
                    }
                    [ordered]@{ index = $pt.Index; size = $pt.Size; type = $pt.Type
                        bootable = $pt.Bootable; letter = $letter
                    }
                })
            [ordered]@{
                index = $idx; device = $d.DeviceID; model = ($d.Model -replace '\s+$', '')
                serial = ($d.SerialNumber -replace '\s+$', ''); size = $d.Size
                bus = if ($phys) { $phys.BusType } else { $d.InterfaceType }
                media = if ($phys) { $phys.MediaType } else { $d.MediaType }
                spindle = if ($phys) { $phys.SpindleSpeed } else { $null }
                health = if ($phys) { $phys.HealthStatus } else { $null }
                removable = ($d.MediaType -like '*Removable*')
                partitions = $myparts
            }
        })
}

# SMART, without smartctl.exe.
#
# Windows has had the drive's own SMART data all along: the storage driver
# publishes the raw 512-byte ATA attribute table through root\wmi, and the
# Storage module's reliability counters cover NVMe, which the ATA classes do
# not. Reading it here means the Windows build measures real drive health on a
# machine with nothing installed — the alternative was telling the tech to go
# and find smartctl.exe, which is a worse answer than a number.
#
# The two buffers are handed over as hex because a 512-element JSON array of
# bytes per drive is 10x the size and no clearer; Python decodes the ATA
# layout (30 x 12-byte entries after a 2-byte revision) exactly as smartctl
# does. A drive behind a USB bridge or a RAID controller usually publishes
# neither — that is the bridge hiding it, and shows up as "not reported".
Section 'smart' {
    $status = @(TryCim MSStorageDriver_FailurePredictStatus 'root/wmi')
    $data = @(TryCim MSStorageDriver_FailurePredictData 'root/wmi')
    $thr = @(TryCim MSStorageDriver_FailurePredictThresholds 'root/wmi')
    @(TryCim Win32_DiskDrive | Sort-Object Index | ForEach-Object {
            $d = $_; $idx = $d.Index
            # MSStorageDriver_* key off the device's PNP id with a '_0' tail
            $pnp = [string]$d.PNPDeviceID
            $key = { param($c) if (-not $pnp) { return $null }
                $c | Where-Object { ($_.InstanceName -replace '_0$', '') -eq $pnp } | Select-Object -First 1 }
            $st = & $key $status; $dt = & $key $data; $th = & $key $thr
            $rc = $null; $phys = $null
            try { $phys = Get-PhysicalDisk -DeviceNumber $idx -ErrorAction Stop } catch {}
            try { if ($phys) { $rc = $phys | Get-StorageReliabilityCounter -ErrorAction Stop } } catch {}
            $hex = { param($b) if ($null -eq $b) { $null } else { (($b | ForEach-Object { '{0:x2}' -f $_ }) -join '') } }
            [ordered]@{
                index           = $idx
                predict_failure = if ($st) { [bool]$st.PredictFailure } else { $null }
                attributes      = & $hex $dt.VendorSpecific
                thresholds      = & $hex $th.VendorSpecific
                health          = if ($phys) { [string]$phys.HealthStatus } else { $null }
                temperature     = if ($rc) { $rc.Temperature } else { $null }
                power_on_hours  = if ($rc) { $rc.PowerOnHours } else { $null }
                wear            = if ($rc) { $rc.Wear } else { $null }
                read_err        = if ($rc) { $rc.ReadErrorsUncorrected } else { $null }
                write_err       = if ($rc) { $rc.WriteErrorsUncorrected } else { $null }
                start_stop      = if ($rc) { $rc.StartStopCycleCount } else { $null }
            }
        })
}

# CPU / system temperature.
#
# Windows has no equivalent of `sensors`: package temperature comes off a
# model-specific register, and reading an MSR needs a kernel driver, which is
# exactly why the fan/thermal test is dropped on this build. What IS readable
# without one is the board's ACPI thermal zone, when the firmware declares one
# — common on laptops and small-form-factor machines, absent on most enthusiast
# desktops. Tenths of a Kelvin, hence the /10 - 273.15 on the Python side.
# Nothing here is invented: no zone means no number, and the UI says so.
Section 'thermal' {
    $zones = @()
    foreach ($z in @(TryCim MSAcpi_ThermalZoneTemperature 'root/wmi')) {
        if ($null -ne $z.CurrentTemperature) {
            $zones += [ordered]@{ name = [string]$z.InstanceName; dk = [int]$z.CurrentTemperature }
        }
    }
    if (-not $zones.Count) {
        # the same ACPI zones through the perf counter, which some SKUs expose
        # when the root\wmi class is locked down
        foreach ($z in @(TryCim Win32_PerfFormattedData_Counters_ThermalZoneInformation)) {
            if ($null -ne $z.Temperature -and $z.Temperature -gt 0) {
                $zones += [ordered]@{ name = [string]$z.Name; dk = ([int]$z.Temperature * 10) }
            }
        }
    }
    $zones
}

# The Windows version and its installed drivers, read LIVE.
#
# The bootable stick reads these out of the SOFTWARE and SYSTEM hives of an
# unbooted install with hivex. Neither is possible here: hivex is a Linux tool,
# and on a running Windows those hive FILES are held open by the kernel with no
# sharing — a tech would get "audit failed" on the machine they are standing in
# front of. The registry itself holds exactly the same keys, so the same
# DriverDesc / DriverVersion / DriverDate / ProviderName that Device Manager
# shows come straight out of HKLM here, in the shape drivers_audit already
# parses. Same answers, same judgement, different door.
Section 'winver' {
    $k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    $v = $null
    try { $v = Get-ItemProperty -Path $k -ErrorAction Stop } catch {}
    if ($null -eq $v) { $null }
    else {
        [ordered]@{
            product         = [string]$v.ProductName
            edition         = [string]$v.EditionID
            build           = [string]$v.CurrentBuildNumber
            ubr             = $v.UBR
            display_version = [string]$(if ($v.DisplayVersion) { $v.DisplayVersion } else { $v.ReleaseId })
            install_type    = [string]$v.InstallationType
        }
    }
}

Section 'drivers' {
    $out = @()
    foreach ($guid in @(
            '{4d36e968-e325-11ce-bfc1-08002be10318}',   # display
            '{4d36e97d-e325-11ce-bfc1-08002be10318}',   # system / chipset
            '{f2e7dd72-6468-4e36-b6f1-6488f42c1b52}',   # firmware
            '{4d36e96a-e325-11ce-bfc1-08002be10318}',   # hdc
            '{4d36e97b-e325-11ce-bfc1-08002be10318}',   # scsi adapter
            '{4d36e972-e325-11ce-bfc1-08002be10318}',   # net
            '{4d36e96c-e325-11ce-bfc1-08002be10318}',   # media / audio
            '{e0cbf06c-cd8b-4647-bb8a-263b43f0f974}',   # bluetooth
            '{36fc9e60-c465-11cf-8056-444553540000}',   # usb
            '{d94ee5d8-d189-4994-83d2-f68d7d41b0e6}')) { # security device / TPM
        $base = "HKLM:\SYSTEM\CurrentControlSet\Control\Class\$guid"
        $subs = @()
        try { $subs = @(Get-ChildItem -Path $base -ErrorAction Stop | Where-Object { $_.PSChildName -match '^\d{4}$' }) } catch {}
        foreach ($s in $subs) {
            $p = $null
            try { $p = Get-ItemProperty -Path $s.PSPath -ErrorAction Stop } catch {}
            if ($null -eq $p -or -not $p.DriverDesc) { continue }
            $out += [ordered]@{
                guid     = $guid
                desc     = [string]$p.DriverDesc
                provider = [string]$p.ProviderName
                version  = [string]$p.DriverVersion
                date     = [string]$p.DriverDate
                inf      = [string]$p.InfPath
                match    = [string]$p.MatchingDeviceId
            }
        }
    }
    $out
}

Section 'volumes' {
    @(TryCim Win32_LogicalDisk | Where-Object { $_.DriveType -in 2, 3 } | ForEach-Object {
            [ordered]@{ letter = $_.DeviceID; fs = $_.FileSystem; label = $_.VolumeName
                size   = $_.Size; free = $_.FreeSpace; type = $_.DriveType
            }
        })
}

Section 'batteries' {
    $bats = @(TryCim Win32_Battery)
    $full = @(TryCim BatteryFullChargedCapacity 'root/wmi')
    $stat = @(TryCim BatteryStatus 'root/wmi')
    $design = @(TryCim BatteryStaticData 'root/wmi')
    $ac = $true
    if ($bats.Count -gt 0) { $ac = ($bats[0].BatteryStatus -ne 1) }
    $i = 0
    @($bats | ForEach-Object {
            $b = $_
            $r = [ordered]@{
                name = ('BAT{0}' -f $i); status = $b.BatteryStatus; percent = $b.EstimatedChargeRemaining
                design_mwh = $design[$i].DesignedCapacity; full_mwh = $full[$i].FullChargedCapacity
                cycles = $design[$i].CycleCount
                rate_mw = if ($stat.Count -gt $i) { $stat[$i].DischargeRate } else { $null }
                ac_online = $ac
                chemistry = $b.Chemistry
            }
            $i++; $r
        })
}

Section 'ac_online' {
    $b = @(TryCim Win32_Battery)
    if ($b.Count -eq 0) { $true } else { ($b[0].BatteryStatus -ne 1) }
}

# PnP devices: one pass, split by bus prefix. ConfigManagerErrorCode 0 means a
# driver is bound and happy; 28 is "no driver installed" — the Windows twin of
# the stick's "enumerated but NO driver bound" finding.
Section 'pnp' {
    @(TryCim Win32_PnPEntity | ForEach-Object {
            [ordered]@{ id = $_.PNPDeviceID; name = $_.Name; class = $_.PNPClass
                service = $_.Service; problem = $_.ConfigManagerErrorCode
                mfr = $_.Manufacturer; status = $_.Status
            }
        })
}

Section 'net' {
    $cfg = @(TryCim Win32_NetworkAdapterConfiguration)
    @(TryCim Win32_NetworkAdapter | Where-Object { $_.PhysicalAdapter } | ForEach-Object {
            $a = $_
            $c = $cfg | Where-Object { $_.Index -eq $a.Index } | Select-Object -First 1
            $net = $null
            try { $net = Get-NetAdapter -InterfaceIndex $a.InterfaceIndex -ErrorAction Stop } catch {}
            [ordered]@{
                name = $a.NetConnectionID; desc = $a.Name; mac = $a.MACAddress
                speed = $a.Speed; up = ($a.NetConnectionStatus -eq 2)
                pnp = $a.PNPDeviceID
                wifi = ($a.Name -match 'Wi-?Fi|Wireless|802\.11' -or ($net -and $net.MediaType -match '802\.11'))
                driver = if ($net) { $net.DriverFileName } else { $null }
                driver_version = if ($net) { $net.DriverVersion } else { $null }
                driver_date = if ($net) { DateStr $net.DriverDate } else { $null }
                ipv4 = @($c.IPAddress | Where-Object { $_ -match '^\d+\.' })
                dhcp = $c.DHCPEnabled
            }
        })
}

Section 'monitors' {
    $ids = @(TryCim WmiMonitorID 'root/wmi')
    $par = @(TryCim WmiMonitorBasicDisplayParams 'root/wmi')
    $i = 0
    @($ids | ForEach-Object {
            $m = $_
            $name = if ($m.UserFriendlyName) { -join [char[]]($m.UserFriendlyName | Where-Object { $_ -gt 0 }) } else { $null }
            $ser = if ($m.SerialNumberID) { -join [char[]]($m.SerialNumberID | Where-Object { $_ -gt 0 }) } else { $null }
            $mfr = if ($m.ManufacturerName) { -join [char[]]($m.ManufacturerName | Where-Object { $_ -gt 0 }) } else { $null }
            # the raw EDID blob lives in the PnP key, not in WMI
            $edid = $null
            try {
                $inst = $m.InstanceName -replace '_0$', ''
                $k = 'HKLM:\SYSTEM\CurrentControlSet\Enum\' + $inst + '\Device Parameters'
                $bytes = (Get-ItemProperty -Path $k -Name EDID -ErrorAction Stop).EDID
                $edid = ($bytes | ForEach-Object { '{0:x2}' -f $_ }) -join ''
            } catch {}
            $p = if ($par.Count -gt $i) { $par[$i] } else { $null }
            $i++
            [ordered]@{ name = $name; serial = $ser; mfr = $mfr; instance = $m.InstanceName
                active = $m.Active; edid_hex = $edid
                width_cm = if ($p) { $p.MaxHorizontalImageSize } else { $null }
                height_cm = if ($p) { $p.MaxVerticalImageSize } else { $null }
            }
        })
}

Section 'tpm' {
    $t = TryCim Win32_Tpm 'root/cimv2/security/microsofttpm'
    if (-not $t) { $null }
    else {
        $v = $null
        try { $v = ($t.SpecVersion -split ',')[0].Trim() } catch {}
        [ordered]@{ present = $true; version = $v; enabled = $t.IsEnabled_InitialValue
            activated = $t.IsActivated_InitialValue; owned = $t.IsOwned_InitialValue
            vendor = $t.ManufacturerIdTxt
        }
    }
}

Section 'elevated' {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { $null }
}

Section 'secureboot' {
    try { [bool](Confirm-SecureBootUEFI) } catch { $null }
}

Section 'firmware_type' {
    # UEFI vs legacy BIOS, without needing the stick's efivars
    try { if ($env:firmware_type) { $env:firmware_type } else { (Get-ComputerInfo -Property BiosFirmwareType -ErrorAction Stop).BiosFirmwareType } } catch { $null }
}

# Which physical disk is BenchDiag itself running from? The Windows build must
# leave that one alone exactly as the stick leaves its own medium alone.
Section 'self_disk' {
    try {
        $root = [System.IO.Path]::GetPathRoot($PSScriptRoot)
        if (-not $root) { $null }
        else {
            $letter = $root.TrimEnd('\')
            $la = @(TryCim Win32_LogicalDiskToPartition)
            $pa = @(TryCim Win32_DiskDriveToDiskPartition)
            $part = ($la | Where-Object { $_.Dependent.DeviceID -eq $letter } | Select-Object -First 1).Antecedent.DeviceID
            $disk = ($pa | Where-Object { $_.Dependent.DeviceID -eq $part } | Select-Object -First 1).Antecedent.DeviceID
            [ordered]@{ letter = $letter; partition = $part; device = $disk }
        }
    } catch { $null }
}

$out | ConvertTo-Json -Depth 7 -Compress
