# Windows PowerShell 5.1 / PowerShell 7. Local metadata only.
# This script never starts Python, reads chat contents or keys, or contacts a network.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:SafeErrors = @()

function Add-SafeError {
    param([string]$Area, [string]$Reason, [System.Exception]$Exception)
    $type = 'unknown'
    if ($null -ne $Exception) { $type = $Exception.GetType().FullName }
    $script:SafeErrors += [ordered]@{ area = $Area; reason = $Reason; exception_type = $type }
}

function Safe-Version {
    param($Value)
    if ($null -eq $Value) { return $null }
    $text = [string]$Value
    if ($text -match '^\d{1,6}(\.\d{1,6}){1,5}([abrc]+\d{1,3})?$') { return $text }
    return $null
}

function Safe-Architecture {
    param($Value)
    switch (([string]$Value).ToUpperInvariant()) {
        'AMD64' { return 'x64' }
        'ARM64' { return 'arm64' }
        'X86' { return 'x86' }
        'IA64' { return 'ia64' }
        default { return 'unknown' }
    }
}

function Test-LocalPlainDirectory {
    param([string]$Directory)
    try {
        # Reject UNC, provider/device paths and relative entries before any I/O.
        if ([string]::IsNullOrEmpty($Directory) -or $Directory -notmatch '^[A-Za-z]:\\') { return $false }
        $full = [System.IO.Path]::GetFullPath($Directory)
        $root = [System.IO.Path]::GetPathRoot($full)
        $drive = New-Object System.IO.DriveInfo($root)
        if ($drive.DriveType -ne [System.IO.DriveType]::Fixed) { return $false }
        $current = $root
        $parts = @($full.Substring($root.Length).Split([char]'\', [System.StringSplitOptions]::RemoveEmptyEntries))
        foreach ($part in $parts) {
            $current = Join-Path -Path $current -ChildPath $part
            $info = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (-not $info.PSIsContainer -or ($info.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
        }
        return $true
    } catch { return $false }
}

function Check-RuntimePresence {
    param([string]$CommandName)
    # Deliberately do not search PATH: it may contain remote shares or junctions.
    return [ordered]@{
        command = $CommandName; found = $null; windows_apps_candidate = $null
        file_version = $null; presence_status = '未验证'
        execution_status = '未验证'; executable_started = $false
        search_scope = 'not_checked'
        reason = 'automatic_runtime_discovery_not_performed'
    }
}

function Check-DefaultDataDirectory {
    param([string]$Kind)
    # Custom, redirected and legacy layouts remain unknown in this first check.
    return [ordered]@{
        kind = $Kind; exists = $null; accessible = $null; db_storage_count = $null
        status = '未验证'; reason = 'automatic_directory_discovery_not_performed'
    }
}

try {
    if (-not (Test-LocalPlainDirectory $PSScriptRoot)) {
        Write-Host '环境检测：待外部条件。'
        Write-Host '原因：local_non_reparse_package_directory_required。'
        Write-Host '请把检测包解压到本机普通目录，再运行；没有验证微信聊天读取。'
        exit 2
    }
    $nativeArch = $env:PROCESSOR_ARCHITECTURE
    if (-not [string]::IsNullOrEmpty($env:PROCESSOR_ARCHITEW6432)) { $nativeArch = $env:PROCESSOR_ARCHITEW6432 }
    $edition = 'unknown'
    if ($PSVersionTable.ContainsKey('PSEdition')) { $edition = [string]$PSVersionTable.PSEdition }
    $result = [ordered]@{
        schema_version = 1
        recorded_at_utc = [DateTime]::UtcNow.ToString('o')
        status = '未验证'; status_scope = 'environment_metadata_only'; reason = 'environment_check_incomplete'
        business_read_status = '未验证'; chat_messages_read = 0
        os = [ordered]@{
            caption = $null; version = $null; build = $null; metadata_source = $null
            native_architecture = Safe-Architecture $nativeArch
            process_architecture = Safe-Architecture $env:PROCESSOR_ARCHITECTURE
            is_64bit_os = [Environment]::Is64BitOperatingSystem
            is_64bit_process = [Environment]::Is64BitProcess
        }
        powershell = [ordered]@{ version = $PSVersionTable.PSVersion.ToString(); edition = $edition }
        privilege = [ordered]@{ administrator = $null }
        wechat = [ordered]@{ process_count = 0; processes = @(); status = '未验证' }
        runtime_presence_checks = @()
        default_data_locations = @()
        custom_data_directory_status = '未验证'
        custom_data_directory_reason = 'custom_directories_not_scanned'
        operations = [ordered]@{
            chat_contents_read = $false; database_headers_read = $false; credential_files_read = $false
            python_executed = $false; network_requested = $false; dependencies_installed = $false
            privilege_elevation_requested = $false; persistent_execution_policy_changed = $false
            services_or_scheduled_tasks_created = $false; wechat_processes_changed = $false
        }
        errors = @()
    }
    if ($env:OS -ne 'Windows_NT') {
        $result.status = '待外部条件'
        $result.reason = 'windows_environment_required'
    } else {
        try {
            $system = Get-CimInstance -ClassName Win32_OperatingSystem -Property 'Caption', 'Version', 'BuildNumber' -ErrorAction Stop
            $safeCaption = [string]$system.Caption -replace '[\r\n\x00-\x1f]', ''
            if ($safeCaption.Length -gt 160) { $safeCaption = $safeCaption.Substring(0, 160) }
            $result.os.caption = $safeCaption
            $result.os.version = Safe-Version $system.Version
            if ([string]$system.BuildNumber -match '^\d{1,10}$') { $result.os.build = [string]$system.BuildNumber }
            $result.os.metadata_source = 'local_cim_win32_operatingsystem'
        } catch {
            $result.os.version = [Environment]::OSVersion.Version.ToString()
            $result.os.metadata_source = 'dotnet_fallback_caption_build_unverified'
            Add-SafeError 'os_metadata' 'cim_metadata_unavailable' $_.Exception
        }
        try {
            $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
            try {
                $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
                $result.privilege.administrator = $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
            } finally { $identity.Dispose() }
        } catch {
            Add-SafeError 'privilege_metadata' 'administrator_check_unavailable' $_.Exception
        }
        $processRows = @()
        $running = @(Get-Process -Name 'WeChat', 'Weixin' -ErrorAction SilentlyContinue)
        foreach ($process in $running) {
            if ($process.ProcessName -ine 'WeChat' -and $process.ProcessName -ine 'Weixin') { continue }
            $name = 'WeChat'
            if ($process.ProcessName -ieq 'Weixin') { $name = 'Weixin' }
            $row = [ordered]@{ name = $name; pid = [int]$process.Id; product_version = $null; version_status = '未验证' }
            try {
                $row.product_version = Safe-Version $process.MainModule.FileVersionInfo.ProductVersion
                if ($null -ne $row.product_version) { $row.version_status = '通过' }
            } catch {
                Add-SafeError 'wechat_version' 'running_process_version_unavailable' $_.Exception
            }
            $processRows += $row
        }
        $result.wechat.processes = @($processRows)
        $result.wechat.process_count = $processRows.Count
        if ($processRows.Count -eq 0) {
            $result.wechat.status = '待外部条件'
            $result.status = '待外部条件'
            $result.reason = 'wechat_main_process_not_running'
        } elseif (@($processRows | Where-Object { $_.version_status -ne '通过' }).Count -gt 0) {
            $result.wechat.status = '未验证'
            $result.status = '未验证'
            $result.reason = 'wechat_running_version_unverified'
        } else {
            $result.wechat.status = '通过'
            $result.status = '通过'
            $result.reason = 'environment_metadata_collected_only'
        }
        foreach ($commandName in @('python.exe', 'python3.exe', 'py.exe')) {
            $result.runtime_presence_checks += Check-RuntimePresence $commandName
        }
        foreach ($kind in @('xwechat_files', 'WeChat Files')) {
            $result.default_data_locations += Check-DefaultDataDirectory $kind
        }
    }
    $result.errors = @($script:SafeErrors)
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $relative = 'results\' + $stamp + '\result-to-send.json'
    $resultsDirectory = Join-Path -Path $PSScriptRoot -ChildPath 'results'
    $resultsInfo = Get-Item -LiteralPath $resultsDirectory -Force -ErrorAction SilentlyContinue
    if ($null -ne $resultsInfo -and -not (Test-LocalPlainDirectory $resultsDirectory)) {
        throw [System.IO.IOException]::new('local_results_directory_required')
    }
    $outputDirectory = Join-Path -Path $resultsDirectory -ChildPath $stamp
    [void](New-Item -ItemType Directory -Path $outputDirectory -ErrorAction Stop)
    $outputPath = Join-Path -Path $outputDirectory -ChildPath 'result-to-send.json'
    $json = $result | ConvertTo-Json -Depth 12
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($outputPath, $json + [Environment]::NewLine, $utf8)
    Write-Host ''
    Write-Host ('环境元信息检测：' + $result.status)
    Write-Host ('微信主进程数量：' + $result.wechat.process_count)
    Write-Host '业务读取：未验证；本次读取聊天消息：0。'
    Write-Host ('请回传文件：' + $relative)
    Write-Host '本检测不表示微信后台聊天读取已通过。'
    exit 0
} catch {
    Write-Host '环境检测：失败。'
    Write-Host ('原因：preflight_operation_failed；异常类型：' + $_.Exception.GetType().FullName)
    Write-Host '本次没有验证微信聊天读取。请将以上固定错误信息反馈给维护人员。'
    exit 2
}
