# Fixed portable runtime; private paths and keys stay on this PC.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:FailureReason = 'web_launcher_failed'
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
    $manifestPath = Join-Path $PackageRoot 'WEB-MANIFEST.json'
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
        if ($lower -eq 'web-manifest.json' -or $lower -eq 'results' -or $lower.StartsWith('results/') -or $lower -eq 'collector-data' -or $lower.StartsWith('collector-data/') -or $names.ContainsKey($lower)) {
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
    foreach ($required in @('runtime/python.exe', 'runtime/python314.dll', 'runtime/python314.zip', 'runtime/python314._pth', 'runtime/_sqlite3.pyd', 'runtime/sqlite3.dll', 'runtime/sqlcipher3/_sqlite3.cp314-win_amd64.pyd', 'runtime/zstandard/backend_c.cp314-win_amd64.pyd', 'collector_service.py', 'collector_supervisor.py', 'collector_private.py', 'continuous_reader.py', 'continuous_store.py', 'conversation_scope.py', 'wechat_identity.py', 'windows_selected_engine.py', 'windows_selected_native.py', 'windows_selected_supervisor.py', 'windows_keyproof_core.py', 'windows_keyproof_platform.py', 'Start-Web.ps1', 'Start-ChatLens.cmd', 'README-WEB.txt', 'web_runtime.py', 'web_server.py', 'windows_web_manager.py', 'windows_web_server.py', 'windows_account_probe.py', 'web_data.py', 'collector_web_meta.py', 'ai_knowledge.py', 'ai_provider.py', 'ai_query.py', 'ai_statistics.py', 'ai_service.py', 'ai_web_server.py', 'web/index.html', 'web/app.js', 'web/styles.css', 'web/connect.html', 'web/connect.js', 'web/connect.css', 'web/assets/chatlens-mark.svg', 'THIRD-PARTY-SELECTED-NOTICES.txt')) {
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
            } elseif ($lower -ne 'web-manifest.json' -and -not $names.ContainsKey($lower)) {
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
    Write-Host '文件完整性检查完成。本包使用固定便携 Python，无需安装其他运行环境。'
    Write-Host '本次原位连接此前已验证的 collector-data，保留原会话、消息、检查点与模型配置。'
    Write-Host '网页仅在本机 127.0.0.1 访问。统计在本机计算；云端分析需在网页另行配置和确认。'
    Write-Host '若旧采集窗口仍运行，请先在旧窗口按一次 Ctrl+C 正常停止，再继续启动。'
    Write-Host '请选择原 collector-data 文件夹。设置中可多选1至50个会话；新旧范围独立保存，可切回恢复。'
    Add-Type -AssemblyName System.Windows.Forms
    $dataDirectory = $null
    $dialog = New-Object Windows.Forms.FolderBrowserDialog
    try {
        $dialog.Description = '选择之前持续采集验证通过的 collector-data 文件夹；保留 private 子目录。'
        $dialog.ShowNewFolderButton = $false
        if ($dialog.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) { $dataDirectory = $dialog.SelectedPath }
    } finally { $dialog.Dispose() }
    if ([string]::IsNullOrEmpty($dataDirectory)) { Stop-Check 'web_existing_collector_data_selection_cancelled' }
    Assert-PrivateAcl $dataDirectory $true
    $privateDirectory = Join-Path $dataDirectory 'private'
    Assert-PrivateAcl $privateDirectory $true
    $scope = Read-PrivateMetadata (Join-Path $privateDirectory 'scope.json')
    if ($scope.schema_version -ne 1) { Stop-Check 'web_scope_schema_unsupported' }
    $expectedExe = [string]$scope.exe_path
    $scope = $null
    # Read-only browsing remains available if the previous client is absent.
    $script:FailureReason = 'web_current_main_process_revalidation_failed'
    $candidates = @()
    if ((Local-File $expectedExe) -and [IO.Path]::GetFileName($expectedExe) -match '^(Weixin|WeChat)\.exe$') {
        try {
            $rows = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='Weixin.exe' OR Name='WeChat.exe'" -Property 'ProcessId', 'ExecutablePath' -ErrorAction Stop)
        } catch { $rows = @() }
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
    }
    $selected = 0
    if ($candidates.Count -gt 1) {
        Write-Host '找到多个符合原程序路径和已测版本的主窗口，请选择此前测试的微信。'
        for ($index = 0; $index -lt $candidates.Count; $index++) {
            Write-Host ('候选 ' + ($index + 1) + '：PID=' + $candidates[$index].Id)
        }
        $answer = Read-Host '输入候选编号，留空取消'
        $choice = 0
        if (-not [int]::TryParse($answer, [ref]$choice) -or $choice -lt 1 -or $choice -gt $candidates.Count) {
            Stop-Check 'web_current_main_process_selection_cancelled_or_invalid'
        }
        $selected = $choice - 1
    }
    $resultsRoot = Join-Path $packageRoot 'results'
    $existingResults = Get-Item -LiteralPath $resultsRoot -Force -ErrorAction SilentlyContinue
    if ($null -ne $existingResults) {
        if (-not (Local-Directory $resultsRoot)) { Stop-Check 'local_results_parent_required' }
    } else { [void](New-Item -ItemType Directory -Path $resultsRoot -ErrorAction Stop) }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $outputDirectory = Join-Path $resultsRoot ($stamp + '-web')
    $runtime = Join-Path $packageRoot 'runtime\python.exe'
    $core = Join-Path $packageRoot 'web_runtime.py'
    $runtimeArguments = @('-I', '-S', '-B', '-X', 'utf8', $core, '--data-dir', $dataDirectory, '--output', $outputDirectory, '--manage-scopes')
    if ($candidates.Count -eq 0) {
        Write-Host '未找到原路径、已测版本的已登录微信。本次仅展示已保存消息；启动微信后重开可继续采集。'
        $runtimeArguments += '--read-only'
    } else {
        $process = $candidates[$selected]
        $processNumber = [int]$process.Id
        $creationTime = [long]$process.StartTime.ToFileTimeUtc()
        if ($creationTime -le 0) { Stop-Check 'web_process_creation_time_unavailable' }
        $runtimeArguments += @('--pid', $processNumber.ToString([Globalization.CultureInfo]::InvariantCulture),
                               '--creation-time', $creationTime.ToString([Globalization.CultureInfo]::InvariantCulture))
    }
    $script:FailureReason = 'fixed_runtime_web_start_failed'
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $runtime @runtimeArguments 2>$null
        $nativeExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedPreference }
    if ($null -eq $nativeExit) { Stop-Check 'fixed_runtime_exit_code_unavailable' }
    Write-Host ('本次网页与采集进程已结束；退出码：' + $nativeExit)
    Write-Host ('只回传本次 results\' + $stamp + '-web 中已生成的公开诊断 JSON。')
    Write-Host 'collector-data/private、配置、密钥、数据库和真实聊天截图留在本机。'
    Write-Host '真实界面联通和新消息展示，须以本机页面实际结果与本次脱敏报告分别核对。'
    exit $nativeExit
} catch {
    Write-Host '本次网页启动未完成。'
    Write-Host ('reason=' + $script:FailureReason + '; exception_type=' + $_.Exception.GetType().FullName)
    Write-Host '原有数据与检查点保留。只反馈固定原因，不发送私人路径、配置或密钥。'
    exit 2
} finally {
    if ($locationPushed) { try { Pop-Location -ErrorAction Stop } catch {} }
}