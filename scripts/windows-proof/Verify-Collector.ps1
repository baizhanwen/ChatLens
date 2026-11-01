# Fixed portable runtime; private paths and keys stay on this PC.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:FailureReason = 'collector_launcher_failed'
$locationPushed = $false

function Stop-Check {
    param([string]$Reason)
    $script:FailureReason = $Reason
    throw [InvalidOperationException]::new($Reason)
}
function Local-Directory {
    param([string]$PathValue)
    try {
        if ([string]::IsNullOrEmpty($PathValue) -or $PathValue -notmatch '^[A-Za-z]:\\') { return $false }
        $full = [IO.Path]::GetFullPath($PathValue)
        $root = [IO.Path]::GetPathRoot($full)
        $drive = New-Object IO.DriveInfo($root)
        if ($drive.DriveType -ne [IO.DriveType]::Fixed) { return $false }
        $current = $root
        foreach ($part in $full.Substring($root.Length).Split([char]'\', [StringSplitOptions]::RemoveEmptyEntries)) {
            $current = Join-Path $current $part
            $info = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (-not $info.PSIsContainer -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
        }
        return $true
    } catch { return $false }
}
function Local-File {
    param([string]$PathValue)
    try {
        if (-not (Local-Directory ([IO.Path]::GetDirectoryName($PathValue)))) { return $false }
        $info = Get-Item -LiteralPath $PathValue -Force -ErrorAction Stop
        return (-not $info.PSIsContainer -and ($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0)
    } catch { return $false }
}
function Read-SmallJson {
    param([string]$PathValue, [long]$Maximum)
    if (-not (Local-File $PathValue)) { Stop-Check 'collector_json_not_local_regular_file' }
    $info = Get-Item -LiteralPath $PathValue -Force -ErrorAction Stop
    if ($info.Length -gt $Maximum -or $info.Length -lt 2) { Stop-Check 'collector_json_size_invalid' }
    return ([IO.File]::ReadAllText($PathValue, [Text.Encoding]::UTF8) | ConvertFrom-Json -ErrorAction Stop)
}
function Assert-PrivateAcl {
    param([string]$PathValue, [bool]$Directory)
    if (($Directory -and -not (Local-Directory $PathValue)) -or (-not $Directory -and -not (Local-File $PathValue))) {
        Stop-Check 'collector_private_metadata_path_unsafe'
    }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $acl = Get-Acl -LiteralPath $PathValue -ErrorAction Stop
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if ($owner -ne $sid -or -not $acl.AreAccessRulesProtected -or $rules.Count -ne 1) {
        Stop-Check 'collector_private_metadata_acl_invalid'
    }
    $rule = $rules[0]
    $flags = 0
    if ($Directory) { $flags = 3 }
    if ($rule.IdentityReference.Value -ne $sid -or $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
        [long]$rule.FileSystemRights -ne 2032127 -or [int]$rule.InheritanceFlags -ne $flags -or
        [int]$rule.PropagationFlags -ne 0 -or $rule.IsInherited) {
        Stop-Check 'collector_private_metadata_acl_invalid'
    }
}
function Read-PrivateMetadata {
    param([string]$PathValue)
    # Verify current-user-only parent and file before reading any JSON bytes.
    Assert-PrivateAcl ([IO.Path]::GetDirectoryName($PathValue)) $true
    Assert-PrivateAcl $PathValue $false
    return (Read-SmallJson $PathValue 2097152)
}

function Verify-Payload {
    param([string]$PackageRoot)
    $script:FailureReason = 'manifest_validation_failed'
    $manifestPath = Join-Path $PackageRoot 'COLLECTOR-MANIFEST.json'
    $manifest = Read-SmallJson $manifestPath 262144
    if ($manifest.schema_version -ne 1) { Stop-Check 'manifest_schema_unsupported' }
    $files = @($manifest.files)
    if ($files.Count -lt 6 -or $files.Count -gt 512) { Stop-Check 'manifest_file_count_invalid' }
    $names = @{}
    foreach ($entry in $files) {
        $name = [string]$entry.name
        if ($name -notmatch '^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$') { Stop-Check 'manifest_payload_name_invalid' }
        foreach ($segment in $name.Split([char]'/')) {
            if ($segment -eq '.' -or $segment -eq '..' -or $segment.EndsWith('.') -or $segment -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)') {
                Stop-Check 'manifest_payload_name_refused'
            }
        }
        $lower = $name.ToLowerInvariant()
        if ($lower -eq 'collector-manifest.json' -or $lower -eq 'results' -or $lower.StartsWith('results/') -or $lower -eq 'collector-data' -or $lower.StartsWith('collector-data/') -or $names.ContainsKey($lower)) {
            Stop-Check 'manifest_payload_duplicate_or_reserved'
        }
        if ([string]$entry.sha256 -notmatch '^[0-9a-fA-F]{64}$' -or [string]$entry.bytes -notmatch '^\d{1,12}$') {
            Stop-Check 'manifest_payload_digest_or_size_invalid'
        }
        $path = Join-Path $PackageRoot $name.Replace([char]'/', [char]'\')
        if (-not (Local-File $path)) { Stop-Check 'manifest_payload_not_local_regular_file' }
        $info = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if ([long]$info.Length -ne [long]$entry.bytes) { Stop-Check 'manifest_payload_size_mismatch' }
        $digest = (Get-FileHash -LiteralPath $path -Algorithm SHA256 -ErrorAction Stop).Hash
        if (-not [string]::Equals($digest, [string]$entry.sha256, [StringComparison]::OrdinalIgnoreCase)) { Stop-Check 'manifest_payload_hash_mismatch' }
        $names[$lower] = $true
    }
    foreach ($required in @('runtime/python.exe', 'runtime/python314.dll', 'runtime/python314.zip', 'runtime/python314._pth', 'runtime/_sqlite3.pyd', 'runtime/sqlite3.dll', 'runtime/sqlcipher3/_sqlite3.cp314-win_amd64.pyd', 'runtime/zstandard/backend_c.cp314-win_amd64.pyd', 'collector_service.py', 'collector_supervisor.py', 'collector_private.py', 'windows_account_probe.py', 'windows_process_detection.py', 'windows_mapped_files.py', 'continuous_reader.py', 'continuous_store.py', 'windows_selected_engine.py', 'windows_selected_native.py', 'windows_selected_supervisor.py', 'windows_keyproof_core.py', 'windows_keyproof_platform.py', 'Verify-Collector.ps1', 'Start-Collector.cmd', 'README-COLLECTOR.txt', 'THIRD-PARTY-SELECTED-NOTICES.txt')) {
        if (-not $names.ContainsKey($required.ToLowerInvariant())) { Stop-Check 'manifest_required_payload_missing' }
    }
    # Enumerate payload only; never descend into local reports or private collector data.
    $queue = New-Object Collections.Queue
    $queue.Enqueue([ordered]@{ path = $PackageRoot; relative = '' })
    $visited = 0
    while ($queue.Count -gt 0) {
        $directory = $queue.Dequeue()
        foreach ($info in @(Get-ChildItem -LiteralPath $directory.path -Force -ErrorAction Stop)) {
            $visited += 1
            if ($visited -gt 1024) { Stop-Check 'manifest_package_entry_limit' }
            if (($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Stop-Check 'manifest_package_reparse_refused' }
            $relative = $info.Name
            if ($directory.relative -ne '') { $relative = $directory.relative + '/' + $info.Name }
            $lower = $relative.ToLowerInvariant()
            if ($info.PSIsContainer) {
                if ($lower -eq 'results' -or $lower -eq 'collector-data') { continue }
                $queue.Enqueue([ordered]@{ path = $info.FullName; relative = $relative })
            } elseif ($lower -ne 'collector-manifest.json' -and -not $names.ContainsKey($lower)) {
                Stop-Check 'manifest_unlisted_payload_refused'
            }
        }
    }
}

try {
    $packageRoot = [IO.Path]::GetFullPath($PSScriptRoot)
    if (-not (Local-Directory $packageRoot)) { Stop-Check 'local_non_reparse_package_directory_required' }
    Verify-Payload $packageRoot
    $nativeArch = $env:PROCESSOR_ARCHITECTURE
    if (-not [string]::IsNullOrEmpty($env:PROCESSOR_ARCHITEW6432)) { $nativeArch = $env:PROCESSOR_ARCHITEW6432 }
    if ($env:OS -ne 'Windows_NT' -or $nativeArch -ne 'AMD64' -or -not [Environment]::Is64BitProcess) { Stop-Check 'windows_x64_process_required' }
    if ([Threading.Thread]::CurrentThread.ApartmentState -ne 'STA') { Stop-Check 'sta_launcher_required' }
    Push-Location -LiteralPath $packageRoot
    $locationPushed = $true
    Write-Host '文件完整性检查完成。本包使用固定便携 Python，无需安装，不使用其他 Python。'
    Write-Host '本次持续采集此前选定私聊和群的新文字；正文与检查点仅保存于本机 collector-data/private。'
    Write-Host '本地采集数据库是普通 SQLite，未加密；不要回传 collector-data、private、配置或密钥。'
    Write-Host '首次 READY 前建立新消息基线；后续从同一检查点恢复，不重新跳到最新消息。'
    Write-Host '保持电脑开机、微信登录和联网；微信置于后台、屏幕保持解锁。看到 READY 后才发送测试文字。'
    Write-Host '按一次 Ctrl+C 正常停止，确认已停止后再发送停机测试两条，然后重开同一个入口。'
    Write-Host '本包不再扫描内存，不自动发送消息，不调用模型，不重启或修改微信。'
    $dataDirectory = Join-Path $packageRoot 'collector-data'
    $selectedConfigPath = $null
    $existingData = Get-Item -LiteralPath $dataDirectory -Force -ErrorAction SilentlyContinue
    $script:FailureReason = 'collector_private_metadata_validation_failed'
    if ($null -ne $existingData) {
        if (-not (Local-Directory $dataDirectory)) { Stop-Check 'collector_existing_data_directory_unsafe' }
        $privateDirectory = Join-Path $dataDirectory 'private'
        $scope = Read-PrivateMetadata (Join-Path $privateDirectory 'scope.json')
        if ($scope.schema_version -ne 1) { Stop-Check 'collector_scope_schema_unsupported' }
        $expectedExe = [string]$scope.exe_path
        Write-Host '已找到本机持续采集配置；本次恢复原有范围和检查点。'
    } else {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object Windows.Forms.OpenFileDialog
        try {
            $dialog.Title = '选择此前成功 r2 本机生成的 private/selected-config.json；不要回传此文件。'
            $dialog.Filter = 'selected-config.json|selected-config.json'
            $dialog.CheckFileExists = $true
            $dialog.Multiselect = $false
            $dialog.RestoreDirectory = $true
            if ($dialog.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) { $selectedConfigPath = $dialog.FileName }
        } finally { $dialog.Dispose() }
        if ($null -eq $selectedConfigPath) { Stop-Check 'collector_initial_configuration_selection_cancelled' }
        if ([IO.Path]::GetFileName($selectedConfigPath) -cne 'selected-config.json') { Stop-Check 'collector_selected_configuration_filename_invalid' }
        $selectedConfig = Read-PrivateMetadata $selectedConfigPath
        $keyPath = Join-Path ([IO.Path]::GetDirectoryName($selectedConfigPath)) 'selected-keys.json'
        $keyMetadata = Read-PrivateMetadata $keyPath
        if ($keyMetadata.schema_version -ne 1) { Stop-Check 'collector_key_schema_unsupported' }
        $expectedExe = [string]$keyMetadata.exe_path
        # Only the executable metadata is consumed here; no keys/configuration are displayed.
        $selectedConfig = $null
        $keyMetadata = $null
    }
    if (-not (Local-File $expectedExe) -or [IO.Path]::GetFileName($expectedExe) -notmatch '^(Weixin|WeChat)\.exe$') {
        Stop-Check 'collector_executable_not_local_or_supported'
    }
    # Never use a historical PID. Match exact saved executable and tested version.
    $script:FailureReason = 'collector_current_main_process_revalidation_failed'
    $candidates = @()
    $rows = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='Weixin.exe' OR Name='WeChat.exe'" -Property 'ProcessId', 'ExecutablePath' -ErrorAction Stop)
    foreach ($row in $rows) {
        try {
            $currentExe = [string]$row.ExecutablePath
            if (-not [string]::Equals($currentExe, $expectedExe, [StringComparison]::OrdinalIgnoreCase)) { continue }
            if (-not (Local-File $currentExe)) { continue }
            $version = [string][Diagnostics.FileVersionInfo]::GetVersionInfo($currentExe).ProductVersion
            if ($version -cne '4.1.15.56') { continue }
            $process = Get-Process -Id ([int]$row.ProcessId) -ErrorAction Stop
            if ($process.ProcessName -ine 'Weixin' -and $process.ProcessName -ine 'WeChat') { continue }
            if ($process.MainWindowHandle -eq [IntPtr]::Zero) { continue }
            $candidates += $process
        } catch { continue }
    }
    if ($candidates.Count -eq 0) { Stop-Check 'collector_matching_current_main_window_not_found' }
    $selected = 0
    if ($candidates.Count -gt 1) {
        Write-Host '找到多个符合原程序路径和已测版本的主窗口候选；请选择此前测试的微信。'
        for ($index = 0; $index -lt $candidates.Count; $index++) {
            Write-Host ('候选 ' + ($index + 1) + '：PID=' + $candidates[$index].Id)
        }
        $answer = Read-Host '输入候选编号，留空取消'
        $choice = 0
        if (-not [int]::TryParse($answer, [ref]$choice) -or $choice -lt 1 -or $choice -gt $candidates.Count) {
            Stop-Check 'collector_current_main_process_selection_cancelled_or_invalid'
        }
        $selected = $choice - 1
    }
    $process = $candidates[$selected]
    $processNumber = [int]$process.Id
    $creationTime = [long]$process.StartTime.ToFileTimeUtc()
    if ($creationTime -le 0) { Stop-Check 'collector_process_creation_time_unavailable' }
    $resultsRoot = Join-Path $packageRoot 'results'
    $existingResults = Get-Item -LiteralPath $resultsRoot -Force -ErrorAction SilentlyContinue
    if ($null -ne $existingResults) {
        if (-not (Local-Directory $resultsRoot)) { Stop-Check 'local_results_parent_required' }
    } else { [void](New-Item -ItemType Directory -Path $resultsRoot -ErrorAction Stop) }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $outputDirectory = Join-Path $resultsRoot ($stamp + '-continuous')
    $runtime = Join-Path $packageRoot 'runtime\python.exe'
    $core = Join-Path $packageRoot 'collector_supervisor.py'
    $creationText = $creationTime.ToString([Globalization.CultureInfo]::InvariantCulture)
    $processText = $processNumber.ToString([Globalization.CultureInfo]::InvariantCulture)
    $runtimeArguments = @('-I', '-S', '-B', '-X', 'utf8', $core, '--data-dir', $dataDirectory,
                          '--pid', $processText, '--creation-time', $creationText, '--output', $outputDirectory)
    if ($null -ne $selectedConfigPath) { $runtimeArguments += @('--selected-config', $selectedConfigPath) }
    $script:FailureReason = 'fixed_runtime_collector_failed'
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $runtime @runtimeArguments 2>$null
        $nativeExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedPreference }
    if ($null -eq $nativeExit) { Stop-Check 'fixed_runtime_exit_code_unavailable' }
    Write-Host ('本次采集进程已结束；退出码：' + $nativeExit)
    Write-Host ('仅回传本次 results\' + $stamp + '-continuous\result-to-send.json 和 supervisor.json。')
    Write-Host 'collector-data/private、旧包 private、配置、密钥和 SQLite 数据库留在本机。'
    Write-Host '停机补采和去重是否通过，需要两轮实际消息报告对照；正常停止本身不等于业务验收。'
    exit $nativeExit
} catch {
    Write-Host '本次持续采集未完成。'
    Write-Host ('reason=' + $script:FailureReason + '; exception_type=' + $_.Exception.GetType().FullName)
    Write-Host '原有数据和检查点保留；只反馈固定状态，不发送私有路径、配置或密钥。'
    exit 2
} finally {
    if ($locationPushed) { try { Pop-Location -ErrorAction Stop } catch {} }
}
