# VideoTurbo Windows Worker 管理脚本：安装、启动、停止、清理、状态都用这一个文件。
#
# 交互式（适合人工）：  .\deploy\worker\windows\worker.ps1
# 参数式（适合自动化）：.\deploy\worker\windows\worker.ps1 <命令> [选项]，详见 help。
#
# Windows Worker 只提供字幕识别服务（单实例）；影片修复仅支持 Linux。
# 本文件必须保存为 UTF-8 BOM，以兼容 Windows PowerShell 5.1。
param(
    [Parameter(Position = 0)]
    [string]$Command = "",
    # 不提问，按默认值执行；clean 时确认删除。
    [switch]$Yes,
    # install：未安装 Python 3.11 时允许通过 winget 安装。
    [switch]$InstallPython,
    # start：跳过首次自检。
    [switch]$SkipSelfTest,
    # logs：实时跟踪。
    [switch]$Follow,
    # logs：显示行数。
    [int]$Lines = 100,
    # clean：跳过某一类，可写多个，例如 -Skip models,pip。
    [ValidateSet("asr", "restore", "models", "pip")]
    [string[]]$Skip = @(),
    # self-test：只自检指定的识别模式；留空自检默认模式。
    [ValidateSet("", "whisperjav", "faster_whisper")]
    [string]$AsrMode = ""
)

$ErrorActionPreference = "Stop"

# ============================================================ 路径与常量

$ScriptPath = $MyInvocation.MyCommand.Path
$ScriptDir = Split-Path -Parent $ScriptPath
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..\..")).Path
$ScriptCmd = ".\deploy\worker\windows\worker.ps1"
$TaskName = "VideoTurbo-Worker"
$HiddenLauncher = Join-Path $ScriptDir "run-hidden.vbs"

$VenvDir = Join-Path $ProjectRoot ".venv"
# 从 uv.lock 导出的依赖清单（精确版本 + 哈希），见 tools/export_requirements.py。
$Requirements = Join-Path $ProjectRoot "deploy\worker\requirements\asr.txt"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$BackendDir = Join-Path $ProjectRoot ".venv-whisperjav"
$BackendMarker = Join-Path $BackendDir "video-turbo-whisperjav.json"
$StateFile = Join-Path $VenvDir "video-turbo-worker.state"
$SelfTestMarker = Join-Path $VenvDir "video-turbo-selftest.ok"
$WorkerConfig = Join-Path $ProjectRoot "config\worker.yaml"
if ($env:VIDEO_TURBO_WORKER_CONFIG) {
    # .NET 的文件接口按进程工作目录解析相对路径，这里统一换成绝对路径。
    $WorkerConfig = $env:VIDEO_TURBO_WORKER_CONFIG
    if (-not [System.IO.Path]::IsPathRooted($WorkerConfig)) { $WorkerConfig = Join-Path (Get-Location).Path $WorkerConfig }
}
$ConfigTemplate = Join-Path $ProjectRoot "config\worker.example.yaml"
$DefaultModelDir = if ($env:VIDEO_TURBO_DEFAULT_MODEL_DIR) { $env:VIDEO_TURBO_DEFAULT_MODEL_DIR } else { Join-Path $ProjectRoot "models" }
$LogDir = Join-Path $ProjectRoot "logs"
$LogFile = Join-Path $LogDir "worker.log"
$RestartDelaySeconds = 15

# ============================================================ 通用函数

# 界面语言：VIDEO_TURBO_LANGUAGE 优先，否则跟随 Windows 显示语言；中文以外一律英文。
$LanguageSetting = if ($env:VIDEO_TURBO_LANGUAGE) { $env:VIDEO_TURBO_LANGUAGE } else { (Get-UICulture).Name }
$UiLang = if ($LanguageSetting -like "zh*") { "zh" } else { "en" }
# 子程序（自检、清理工具、识别服务）按同一语言输出。
$env:VIDEO_TURBO_LANGUAGE = if ($UiLang -eq "zh") { "zh-Hans" } else { "en" }

function L([string]$Zh, [string]$En) { if ($UiLang -eq "zh") { $Zh } else { $En } }

function Write-Info([string]$Message) { Write-Host "[VideoTurbo] $Message" }
function Write-Warn([string]$Message) { Write-Host "[VideoTurbo] $Message" -ForegroundColor Yellow }

function Test-Interactive {
    if ($Yes -or $env:VIDEO_TURBO_NONINTERACTIVE -eq "1") { return $false }
    if (-not [Environment]::UserInteractive) { return $false }
    try { return -not [Console]::IsInputRedirected } catch { return $false }
}

# 提问；非交互时直接采用默认值。
function Confirm-Action([string]$Question, [bool]$Default = $true) {
    if (-not (Test-Interactive)) { return $Default }
    $hint = if ($Default) { "Y/n" } else { "y/N" }
    $answer = (Read-Host "$Question ($hint)").Trim().ToLower()
    if (-not $answer) { return $Default }
    return ($answer -eq "y" -or $answer -eq "yes")
}

# 运行只关心退出码的探测命令。Windows PowerShell 5.1 在 ErrorActionPreference=Stop 时，
# 会把原生命令写到 stderr 的内容当作终止错误，所以探测命令统一在这里放宽后再丢弃输出。
function Invoke-Probe([scriptblock]$Script) {
    $ErrorActionPreference = "Continue"
    try { & $Script 2>$null | Out-Null } catch { return 1 }
    return $LASTEXITCODE
}

# 不用 -like：路径里的方括号会被当成通配符。
function Test-Contains([string]$Text, [string]$Needle) {
    if (-not $Text -or -not $Needle) { return $false }
    return ($Text.IndexOf($Needle, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user;$env:LOCALAPPDATA\Microsoft\WinGet\Links"
}

function Get-ProjectVersion {
    $versionFile = Join-Path $ProjectRoot "src\video_turbo\__init__.py"
    if (-not (Test-Path $versionFile)) { return "" }
    foreach ($line in [System.IO.File]::ReadAllLines($versionFile)) {
        if ($line -match '^__version__\s*=\s*"([^"]+)"') { return $Matches[1] }
    }
    return ""
}

function Get-InstalledVersion {
    if (-not (Test-Path $StateFile)) { return "" }
    foreach ($line in [System.IO.File]::ReadAllLines($StateFile)) {
        if ($line -match '^version=(.*)$') { return $Matches[1].Trim() }
    }
    return ""
}

function Test-Installed {
    return ((Test-Path $VenvPython) -and (Test-Path $BackendMarker) -and (Get-InstalledVersion))
}

# 读取 worker.yaml 的顶层标量（server_url、worker_token），不依赖 Python。
function Get-ConfigValue([string]$Key) {
    if (-not (Test-Path $WorkerConfig)) { return "" }
    foreach ($line in [System.IO.File]::ReadAllLines($WorkerConfig)) {
        if ($line -match "^$([regex]::Escape($Key))\s*:\s*(.*)$") {
            $value = ($Matches[1] -replace '\s+#.*$', '').Trim()
            return $value.Trim('"').Trim("'")
        }
    }
    return ""
}

function Get-MissingConfig {
    $missing = @()
    if (-not (Get-ConfigValue "server_url")) { $missing += "server_url" }
    if (-not (Get-ConfigValue "worker_token")) { $missing += "worker_token" }
    return $missing
}

function Assert-Installed {
    if (-not (Test-Installed)) { throw (L "这台电脑还没有安装服务，请先运行：$ScriptCmd install" "This PC doesn't have the service installed yet; run first: $ScriptCmd install") }
}

function Assert-Config {
    if (-not (Test-Path $WorkerConfig)) { throw (L "找不到 $WorkerConfig，请先运行：$ScriptCmd install" "$WorkerConfig not found; run first: $ScriptCmd install") }
    $missing = Get-MissingConfig
    if ($missing.Count -gt 0) {
        throw (L "$WorkerConfig 尚未填写 $($missing -join '、')。重新运行 $ScriptCmd install 可交互填写，或直接编辑该文件。" "$WorkerConfig is missing $($missing -join ', '). Run $ScriptCmd install again to fill it in, or edit the file directly.")
    }
}

# ============================================================ 进程与计划任务

function Get-WorkerTask {
    return Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

# 本项目的后台守护进程（隐藏启动器和它启动的 PowerShell）。
function Get-SupervisorProcesses {
    return @(Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and (
            ((Test-Contains $_.CommandLine "__supervise") -and (Test-Contains $_.CommandLine $ScriptPath)) -or
            (Test-Contains $_.CommandLine $HiddenLauncher)
        )
    })
}

# 本项目的 Worker 主进程。venv 的 python.exe 是启动器，真正的解释器是它的子进程，
# 两者的命令行都带有项目的配置文件路径，按进程树结束即可。
function Get-WorkerProcesses {
    return @(Get-CimInstance Win32_Process | Where-Object {
        (Test-Contains $_.CommandLine "video_turbo.worker.main") -and
        (Test-Contains $_.CommandLine $WorkerConfig) -and
        -not (Test-Contains $_.CommandLine "--self-test")
    })
}

function Test-WorkerRunning {
    return ((Get-WorkerProcesses).Count -gt 0 -or (Get-SupervisorProcesses).Count -gt 0)
}

function Stop-ProcessTree([int]$ProcessId) {
    Invoke-Probe { taskkill.exe /PID $ProcessId /T /F } | Out-Null
}

function Register-WorkerTask {
    if (-not (Test-Path $HiddenLauncher)) { throw (L "找不到隐藏启动器：$HiddenLauncher" "Hidden launcher not found: $HiddenLauncher") }
    $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\wscript.exe" -Argument "`"$HiddenLauncher`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 10 `
        -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description "VideoTurbo GPU Worker" -Force | Out-Null
}

# ============================================================ 安装

# 下载源：PyPI、PyTorch、Hugging Face、GitHub 各有官方源和镜像。用前花几秒并行测连通性、
# 文件是否存在和速度，自动选最快的；已设置 VIDEO_TURBO_* 环境变量的不测速。
# 结果写入环境变量，本次运行内的后续步骤和子进程直接沿用。
function Select-Sources([string[]]$Names) {
    $tool = Join-Path $ProjectRoot "tools\select_sources.py"
    if ((Test-Path $tool) -and (Test-Path $VenvPython)) {
        $output = & $VenvPython $tool --config $WorkerConfig --format json @Names
        if ($LASTEXITCODE -eq 0 -and $output) {
            $selected = ($output | Select-Object -Last 1) | ConvertFrom-Json
            foreach ($property in $selected.PSObject.Properties) {
                Set-Item "Env:$($property.Name)" $property.Value
            }
        }
    }
    if (-not $env:VIDEO_TURBO_PYPI_INDEX_URL) { $env:VIDEO_TURBO_PYPI_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple" }
    $env:PIP_INDEX_URL = $env:VIDEO_TURBO_PYPI_INDEX_URL
    $env:UV_DEFAULT_INDEX = $env:VIDEO_TURBO_PYPI_INDEX_URL
    if ($env:VIDEO_TURBO_HF_ENDPOINT) {
        $env:HF_ENDPOINT = $env:VIDEO_TURBO_HF_ENDPOINT
        # 镜像下载可能跳转到 Xet CAS 后 401，统一用普通 HTTP 下载。
        if (-not $env:HF_HUB_DISABLE_XET) { $env:HF_HUB_DISABLE_XET = "1" }
    }
}

function Find-Python311 {
    if (-not (Get-Command py.exe -ErrorAction SilentlyContinue)) { return $false }
    $code = Invoke-Probe { py.exe -3.11 -c "import sys; assert sys.version_info[:2] == (3, 11)" }
    return ($code -eq 0)
}

function Install-WingetPackage([string]$Id, [string]$Label, [switch]$Silent) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw (L "找不到 winget，请手动安装 $Label 后重新运行安装。" "winget not found; install $Label by hand and run the installation again.")
    }
    Write-Info (L "通过 winget 安装 $Label。" "Installing $Label with winget.")
    $arguments = @("install", "--id", $Id, "--exact", "--accept-package-agreements", "--accept-source-agreements")
    if ($Silent) { $arguments += "--silent" }
    & winget.exe @arguments
    Update-SessionPath
}

function Invoke-Checked([string]$Failure, [scriptblock]$Script) {
    & $Script
    if ($LASTEXITCODE -ne 0) { throw $Failure }
}

function Invoke-ConfigureWorker {
    $arguments = @(
        (Join-Path $ProjectRoot "tools\configure_worker.py"),
        "--config", $WorkerConfig,
        "--template", $ConfigTemplate,
        "--default-download-root", $DefaultModelDir,
        # Windows 电脑通常与桌面、游戏共用显卡：新建配置时开启空闲检测，专用机器可在 worker.yaml 中关闭。
        "--shared-machine"
    )
    if (-not (Test-Interactive)) { $arguments += "--non-interactive" }
    Invoke-Checked (L "写入 $WorkerConfig 失败。" "Failed to write $WorkerConfig.") { & $VenvPython @arguments }
}

function Invoke-Install {
    Write-Info (L "项目目录：$ProjectRoot" "Project folder: $ProjectRoot")

    # 上传不完整时 pip 只会报找不到 src，提前给出明确提示。
    foreach ($required in @("pyproject.toml", "src\video_turbo\__init__.py", "tools\install_whisperjav.py",
            "tools\configure_worker.py", "config\worker.example.yaml", "deploy\worker\requirements\asr.txt")) {
        if (-not (Test-Path (Join-Path $ProjectRoot $required))) {
            throw (L "项目文件不完整：缺少 $required。请把项目的全部目录（src、config、deploy、tools）复制到 $ProjectRoot。" "The project files are incomplete: $required is missing. Copy every folder of the project (src, config, deploy, tools) to $ProjectRoot.")
        }
    }

    if (-not (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue)) {
        throw (L "找不到 nvidia-smi.exe，请先安装 NVIDIA 驱动。" "nvidia-smi.exe not found; install the NVIDIA driver first.")
    }
    & nvidia-smi.exe --query-gpu=name,driver_version,memory.total --format=csv,noheader

    # 项目固定使用 Python 3.11，避免新版 Python 与 CUDA 依赖不兼容。
    if (-not (Find-Python311)) {
        if ($InstallPython -or ((Test-Interactive) -and (Confirm-Action (L "未找到 Python 3.11，是否通过 winget 安装？" "Python 3.11 not found. Install it with winget?")))) {
            Install-WingetPackage "Python.Python.3.11" "Python 3.11 x64"
        }
        if (-not (Find-Python311)) {
            throw (L "未找到 Python 3.11。运行 $ScriptCmd install -InstallPython，或手动安装 Python 3.11 x64 后重试（刚装完时请重新打开 PowerShell）。" "Python 3.11 not found. Run $ScriptCmd install -InstallPython, or install Python 3.11 x64 by hand and retry (reopen PowerShell right after installing).")
        }
    }

    # 运行中的 Worker 先停止，装完后恢复。
    $wasRunning = Test-WorkerRunning
    if ($wasRunning) {
        Write-Info (L "程序正在运行，先停止；安装完成后自动恢复。正在识别的任务会由主程序重新排队。" "The program is running; stopping it first and restarting after the installation. Jobs being recognized are queued again by the main app.")
        Invoke-Stop
    }

    # CTranslate2 在 Windows 上需要 Visual C++ x64 运行库。已安装时 winget 会返回非零，忽略即可。
    if (Get-Command winget.exe -ErrorAction SilentlyContinue) {
        Invoke-Probe { winget.exe install --id Microsoft.VCRedist.2015+.x64 --exact --silent --accept-package-agreements --accept-source-agreements } | Out-Null
    }
    foreach ($dependency in @(@{Command = "git"; Id = "Git.Git"}, @{Command = "ffmpeg"; Id = "Gyan.FFmpeg"})) {
        if (-not (Get-Command $dependency.Command -ErrorAction SilentlyContinue)) {
            Install-WingetPackage $dependency.Id $dependency.Command
            if (-not (Get-Command $dependency.Command -ErrorAction SilentlyContinue)) {
                throw (L "$($dependency.Command) 安装后仍不可用，请手动安装并加入 PATH，然后重新运行安装。" "$($dependency.Command) still isn't available after installing; install it by hand, add it to PATH and run the installation again.")
            }
        }
    }

    Remove-Item $StateFile, $SelfTestMarker -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $VenvPython)) {
        Write-Info (L "创建程序运行环境 .venv。" "Creating the program environment .venv.")
        Invoke-Checked (L "创建程序运行环境失败。" "Failed to create the program environment.") { & py.exe -3.11 -m venv $VenvDir }
    }

    Select-Sources @("pypi", "torch", "hf", "github")
    Write-Info (L "Python 组件下载地址：$env:PIP_INDEX_URL" "Python package index: $env:PIP_INDEX_URL")

    # 在耗时的依赖下载前填写配置：首次安装复制模板并询问 server_url、worker_token 和模型目录。
    Write-Info (L "配置 $WorkerConfig。" "Configuring $WorkerConfig.")
    Invoke-ConfigureWorker

    Write-Info (L "安装所需组件（第一次要下载显卡运行库，耗时较长）。" "Installing the required components (the first run downloads the GPU runtime and takes a while).")
    Invoke-Checked (L "更新安装工具失败。" "Failed to update the installer tools.") { & $VenvPython -m pip install --upgrade pip wheel }
    # 按依赖清单安装，版本与 CI 测试一致；项目代码随后单独安装，不再解析依赖。
    Invoke-Checked (L "组件安装失败。" "Failed to install the components.") { & $VenvPython -m pip install --require-hashes -r $Requirements }
    # 项目版本号未变化时也强制替换本地业务代码，避免继续加载旧 Worker。
    Invoke-Checked (L "程序安装失败。" "Failed to install the program.") { & $VenvPython -m pip install --force-reinstall --no-deps $ProjectRoot }

    Write-Info (L "安装默认识别模式需要的组件。" "Installing the components for the default recognition mode.")
    Invoke-Checked (L "语音识别组件安装失败，程序还没更新完。" "Failed to install the speech recognition components; the update isn't finished.") {
        & $VenvPython (Join-Path $ProjectRoot "tools\install_whisperjav.py")
    }

    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    Register-WorkerTask
    [System.IO.File]::WriteAllText($StateFile, "version=$(Get-ProjectVersion)`n")

    Write-Host ""
    Write-Info (L "安装完成（v$(Get-ProjectVersion)）。已注册登录后自动启动的计划任务 $TaskName。" "Installed (v$(Get-ProjectVersion)). Registered the scheduled task $TaskName to start after sign-in.")
    $missing = Get-MissingConfig
    if ($missing.Count -gt 0) {
        Write-Warn (L "请编辑 $WorkerConfig，填写 $($missing -join '、')，然后运行：$ScriptCmd start" "Edit $WorkerConfig, fill in $($missing -join ', '), then run: $ScriptCmd start")
    } elseif ($wasRunning) {
        Invoke-Start
    } else {
        Write-Info (L "下一步：$ScriptCmd start（首次启动会先自检并下载模型）。" "Next: $ScriptCmd start (the first start runs a self-test and downloads models).")
    }
}

# ============================================================ 自检、启动、停止

function Invoke-SelfTest {
    Assert-Installed
    Assert-Config
    Write-Info (L "开始自检：提前下载缺失的模型，逐个检查识别模型、VAD 和字幕合并。首次需下载模型，耗时较长。" "Starting the self-test: downloading missing models ahead of time and checking the recognition models, VAD and subtitle merge one by one. The first run downloads models and takes a while.")
    $arguments = @("-m", "video_turbo.worker.main", "--config", $WorkerConfig, "--self-test")
    if ($AsrMode) { $arguments += @("--asr-mode", $AsrMode) }
    Select-Sources @("hf")
    Set-Location $ProjectRoot
    & $VenvPython @arguments
    if ($LASTEXITCODE -ne 0) {
        Remove-Item $SelfTestMarker -Force -ErrorAction SilentlyContinue
        throw (L "自检失败（代码 $LASTEXITCODE），程序不会启动。排查后重新运行：$ScriptCmd self-test" "The self-test failed (code $LASTEXITCODE), so the program won't start. After fixing it, run: $ScriptCmd self-test")
    }
    # 只自检一种模式时不记为完整通过。
    if (-not $AsrMode) { [System.IO.File]::WriteAllText($SelfTestMarker, "version=$(Get-InstalledVersion)`n") }
    Write-Info (L "自检通过。" "Self-test passed.")
}

function Invoke-Start {
    Assert-Installed
    Assert-Config
    if (Test-WorkerRunning) {
        Write-Info (L "程序已在运行。需要重启请运行：$ScriptCmd restart" "The program is already running. To restart it, run: $ScriptCmd restart")
        return
    }
    if (-not (Get-WorkerTask)) { Register-WorkerTask }
    if (-not $SkipSelfTest -and -not (Test-Path $SelfTestMarker)) { Invoke-SelfTest }
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    if (Test-WorkerRunning) {
        Write-Info (L "程序已在后台启动，登录 Windows 后也会自动启动。日志：$LogFile" "The program started in the background and also starts after you sign in to Windows. Log: $LogFile")
        Write-Info (L "查看日志：$ScriptCmd logs -Follow" "View the log: $ScriptCmd logs -Follow")
    } else {
        throw (L "计划任务已触发，但没有发现程序在运行。请查看 $LogFile，或运行 $ScriptCmd status。" "The scheduled task ran, but the program isn't running. Check $LogFile, or run $ScriptCmd status.")
    }
}

function Invoke-Stop {
    $task = Get-WorkerTask
    if ($task -and $task.State -eq "Running") { Stop-ScheduledTask -TaskName $TaskName }
    # 先结束守护进程，避免它在 Worker 退出后立刻重新拉起。
    foreach ($process in Get-SupervisorProcesses) { Stop-ProcessTree $process.ProcessId }
    foreach ($process in Get-WorkerProcesses) { Stop-ProcessTree $process.ProcessId }
    Start-Sleep -Seconds 1
    if (Test-WorkerRunning) { throw (L "还有程序没能停下，请在任务管理器里检查后重试。" "Some programs didn't stop; check Task Manager and retry.") }
    Write-Info (L "程序已停止。正在识别的任务稍后会由主程序重新排队。" "The program stopped. Jobs being recognized are queued again by the main app shortly.")
}

# 计划任务通过隐藏启动器调用：循环守护 Worker，主控或网络暂时不可用时自动重连。
function Invoke-Supervise {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    Set-Location $ProjectRoot
    Select-Sources @("hf")
    while ($true) {
        # 由 Python 直接以 UTF-8 写日志，不经过 PowerShell 的输出流。
        & $VenvPython -m video_turbo.worker.main --config $WorkerConfig --log-file $LogFile
        Start-Sleep -Seconds $RestartDelaySeconds
    }
}

# ============================================================ 状态、日志、清理、卸载

function Show-Status {
    Write-Host (L "==== VideoTurbo 服务状态（项目版本 v$(Get-ProjectVersion)） ====" "==== VideoTurbo service status (project version v$(Get-ProjectVersion)) ====")
    $installed = Get-InstalledVersion
    if (Test-Installed) {
        $note = if ($installed -ne (Get-ProjectVersion)) { (L "，可更新到 v$(Get-ProjectVersion)（运行 install）" ", update to v$(Get-ProjectVersion) available (run install)") } else { "" }
        Write-Host (L "字幕识别：已安装 v$installed$note" "Subtitle recognition: installed v$installed$note")
    } else {
        Write-Host (L "字幕识别：未安装" "Subtitle recognition: not installed")
    }
    Write-Host ((L "首次自检：" "First self-test: ") + $(if (Test-Path $SelfTestMarker) { (L "已通过" "passed") } else { (L "未通过或未运行" "not passed or not run") }))

    if (Test-Path $WorkerConfig) {
        $token = if (Get-ConfigValue "worker_token") { (L "已填写" "set") } else { (L "未填写" "empty") }
        Write-Host (L "配置：$WorkerConfig" "Config: $WorkerConfig")
        Write-Host "  server_url=$(Get-ConfigValue 'server_url')  worker_token=$token"
    } else {
        Write-Host (L "配置：尚未创建（运行 install）" "Config: not created yet (run install)")
    }

    $task = Get-WorkerTask
    Write-Host ((L "计划任务：" "Scheduled task: ") + $(if ($task) { (L "$TaskName（$($task.State)）" "$TaskName ($($task.State))") } else { (L "未注册" "not registered") }))
    $workers = Get-WorkerProcesses
    if ($workers.Count -gt 0) {
        Write-Host (L "程序：运行中（PID $(($workers | ForEach-Object { $_.ProcessId }) -join '、')）" "Program: running (PID $(($workers | ForEach-Object { $_.ProcessId }) -join ', '))")
    } elseif ((Get-SupervisorProcesses).Count -gt 0) {
        Write-Host (L "程序：刚才意外退出，正在自动重启（看日志了解原因）" "Program: exited unexpectedly and is restarting automatically (see the log for why)")
    } else {
        Write-Host (L "程序：未运行" "Program: not running")
    }

    if (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue) {
        Write-Host (L "GPU：" "GPU:")
        & nvidia-smi.exe --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader
    }
    $drive = Get-PSDrive -Name $ProjectRoot.Substring(0, 1) -ErrorAction SilentlyContinue
    if ($drive) { Write-Host ((L "磁盘 {0}: 剩余 {1:N1} GB" "Disk {0}: {1:N1} GB free") -f $drive.Name, ($drive.Free / 1GB)) }
    Write-Host (L "日志：$LogFile" "Log: $LogFile")
}

function Show-Logs {
    if (-not (Test-Path $LogFile)) { throw (L "还没有日志文件：$LogFile" "No log file yet: $LogFile") }
    if ($Follow) {
        Write-Info (L "实时跟踪日志，按 Ctrl+C 退出。" "Following the log; press Ctrl+C to quit.")
        Get-Content -Path $LogFile -Tail $Lines -Wait -Encoding UTF8
    } else {
        Get-Content -Path $LogFile -Tail $Lines -Encoding UTF8
    }
}

function Invoke-Clean {
    if (-not (Test-Path $VenvPython)) { throw (L "还没安装程序，请先运行：$ScriptCmd install" "The program isn't installed yet; run first: $ScriptCmd install") }
    $arguments = @((Join-Path $ProjectRoot "tools\cleanup_worker.py"), "--config", $WorkerConfig)
    foreach ($item in $Skip) { $arguments += @("--skip", $item) }

    # 先列出可清理的内容和大小。
    & $VenvPython @arguments
    if ($LASTEXITCODE -ne 0) { throw (L "清理检查失败（代码 $LASTEXITCODE）。" "The cleanup check failed (code $LASTEXITCODE).") }

    $confirmed = [bool]$Yes
    if (-not $confirmed -and (Test-Interactive)) {
        Write-Warn (L "删除后不可恢复；已下载完成的模型、配置和日志不会删除。清理前会强制停止程序，正在识别的任务由主程序重新排队。" "Deleted items can't be recovered; downloaded models, config and logs are kept. The program is stopped before cleaning, and jobs being recognized are queued again by the main app.")
        $confirmed = Confirm-Action (L "确认删除以上内容？" "Delete the items above?") $false
    }
    if (-not $confirmed) {
        Write-Info (L "未删除任何内容。确认删除请运行：$ScriptCmd clean -Yes" "Nothing was deleted. To delete, run: $ScriptCmd clean -Yes")
        return
    }
    $wasRunning = Test-WorkerRunning
    if ($wasRunning) { Invoke-Stop }
    $arguments += "--yes"
    & $VenvPython @arguments
    if ($LASTEXITCODE -ne 0) { throw (L "清理失败（代码 $LASTEXITCODE）。" "Cleanup failed (code $LASTEXITCODE).") }
    if ($wasRunning) { Write-Info (L "程序已停止，需要时运行：$ScriptCmd start" "The program stopped; when needed run: $ScriptCmd start") }
}

function Invoke-Uninstall {
    if (-not (Confirm-Action (L "确认停止程序并删除计划任务 $TaskName？模型、配置和日志会保留。" "Stop the program and delete the scheduled task $TaskName? Models, config and logs are kept."))) {
        Write-Info (L "已取消。" "Cancelled.")
        return
    }
    Invoke-Stop
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Info (L "已删除计划任务；项目中的文件保持不变，需要彻底删除时直接删除项目目录。" "Scheduled task deleted; the project files are unchanged. To remove everything, delete the project folder.")
}

# ============================================================ 菜单与入口

function Show-Usage {
    if ($UiLang -eq "zh") {
        Write-Host @"
用法：
  $ScriptCmd                  进入交互式菜单
  $ScriptCmd <命令> [选项]     直接运行（适合自动化）

Windows 电脑只提供字幕识别服务；影片修复仅支持 Linux。

命令：
  install      安装或更新（重复运行即更新；运行中的程序会先停止、装完后恢复）
  start        启动。首次启动会先自检并下载模型
  stop         停止（正在识别的任务由主程序重新排队）
  restart      重启
  status       安装版本、配置、计划任务、进程、GPU、磁盘
  logs         查看日志，例如 logs -Follow
  self-test    重新运行自检
  clean        列出可清理的残留；加 -Yes 停止程序后删除
  uninstall    停止并删除计划任务，保留模型、配置和日志
  help         显示本帮助

选项：
  -Yes              不提问，按默认值执行；clean 时确认删除
  -InstallPython    install 时允许通过 winget 安装 Python 3.11
  -SkipSelfTest     start 时跳过首次自检
  -Follow           logs 实时跟踪
  -Lines N          logs 显示行数（默认 100）
  -Skip asr,models,pip   clean 时跳过某一类
  -AsrMode whisperjav|faster_whisper   self-test 只自检一种识别模式

高级选项（环境变量）：VIDEO_TURBO_LANGUAGE=zh-Hans|en、VIDEO_TURBO_NONINTERACTIVE=1、VIDEO_TURBO_PYPI_INDEX_URL、VIDEO_TURBO_TORCH_INDEX_URL、
  VIDEO_TURBO_HF_ENDPOINT、VIDEO_TURBO_GITHUB_PROXY、VIDEO_TURBO_TORCH_CUDA、
  VIDEO_TURBO_DEFAULT_MODEL_DIR、VIDEO_TURBO_WORKER_CONFIG
"@
    } else {
        Write-Host @"
Usage:
  $ScriptCmd                  open the interactive menu
  $ScriptCmd <command> [options]   run directly (for automation)

Windows PCs only provide subtitle recognition; video restore needs Linux.

Commands:
  install      install or update (run again to update; a running program is stopped and restarted afterwards)
  start        start. The first start runs a self-test and downloads models
  stop         stop (jobs being recognized are queued again by the main app)
  restart      restart
  status       installed version, config, scheduled task, processes, GPU, disk
  logs         show the log, e.g. logs -Follow
  self-test    run the self-test again
  clean        list leftovers that can be removed; add -Yes to stop the program and delete them
  uninstall    stop and delete the scheduled task; models, config and logs are kept
  help         show this help

Options:
  -Yes              don't ask, use defaults; confirms deletion for clean
  -InstallPython    allow install to install Python 3.11 with winget
  -SkipSelfTest     skip the first self-test on start
  -Follow           follow logs live
  -Lines N          number of log lines (default 100)
  -Skip asr,models,pip   skip a category for clean
  -AsrMode whisperjav|faster_whisper   self-test only one recognition mode

Advanced (environment variables): VIDEO_TURBO_LANGUAGE=zh-Hans|en, VIDEO_TURBO_NONINTERACTIVE=1, VIDEO_TURBO_PYPI_INDEX_URL, VIDEO_TURBO_TORCH_INDEX_URL,
  VIDEO_TURBO_HF_ENDPOINT, VIDEO_TURBO_GITHUB_PROXY, VIDEO_TURBO_TORCH_CUDA,
  VIDEO_TURBO_DEFAULT_MODEL_DIR, VIDEO_TURBO_WORKER_CONFIG
"@
    }
}

function Invoke-WorkerCommand([string]$Name) {
    switch ($Name) {
        "install" { Invoke-Install }
        "start" { Invoke-Start }
        "stop" { Invoke-Stop }
        "restart" { Invoke-Stop; Invoke-Start }
        "status" { Show-Status }
        "logs" { Show-Logs }
        "self-test" { Invoke-SelfTest }
        "clean" { Invoke-Clean }
        "uninstall" { Invoke-Uninstall }
        "help" { Show-Usage }
        default { throw (L "未知命令：$Name（$ScriptCmd help 查看用法）" "Unknown command: $Name (see $ScriptCmd help)") }
    }
}

function Show-Menu {
    while ($true) {
        Write-Host ""
        Write-Host (L "==== VideoTurbo 服务管理（Windows · v$(Get-ProjectVersion)） ====" "==== VideoTurbo service manager (Windows · v$(Get-ProjectVersion)) ====")
        $state = if (Test-Installed) { (L "已安装 v$(Get-InstalledVersion)" "installed v$(Get-InstalledVersion)") } else { (L "未安装" "not installed") }
        $running = if (Test-WorkerRunning) { (L "运行中" "running") } else { (L "未运行" "not running") }
        Write-Host (L "字幕识别：$state，$running" "Subtitle recognition: $state, $running")
        Write-Host (L "  1) 安装 / 更新" "  1) Install / update")
        Write-Host (L "  2) 启动" "  2) Start")
        Write-Host (L "  3) 停止" "  3) Stop")
        Write-Host (L "  4) 清理临时文件" "  4) Clean up temporary files")
        Write-Host (L "  5) 状态" "  5) Status")
        Write-Host (L "  6) 查看日志" "  6) View logs")
        Write-Host (L "  7) 重新自检" "  7) Run the self-test again")
        Write-Host (L "  8) 卸载计划任务" "  8) Remove the scheduled task")
        Write-Host (L "  0) 退出" "  0) Exit")
        $choice = (Read-Host (L "请选择" "Choose")).Trim()
        $name = switch ($choice) {
            "1" { "install" } "2" { "start" } "3" { "stop" } "4" { "clean" } "5" { "status" }
            "6" { "logs" } "7" { "self-test" } "8" { "uninstall" } "0" { "" } "q" { "" }
            default { "?" }
        }
        if ($name -eq "") { return }
        if ($name -eq "?") { Write-Warn (L "请输入 0～8。" "Enter 0–8."); continue }
        # 菜单里的一次失败不退出菜单，方便看完提示后继续操作。
        try { Invoke-WorkerCommand $name } catch { Write-Host (L "错误：$($_.Exception.Message)" "Error: $($_.Exception.Message)") -ForegroundColor Red }
    }
}

# 下载源由 Select-Sources 在安装和启动时测速选择。
# 不保留 pip/uv 下载缓存：CUDA 版 PyTorch 等 wheel 缓存会额外占用数 GB。
$env:PIP_NO_CACHE_DIR = "1"
$env:UV_NO_CACHE = "1"
# 不从其他环境继承的 CUDA 源解析 PyTorch。
foreach ($variable in @("PIP_EXTRA_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_INDEX")) {
    Remove-Item "Env:$variable" -ErrorAction SilentlyContinue
}

try {
    if ($Command -eq "__supervise") {
        Invoke-Supervise
    } elseif ($Command) {
        Invoke-WorkerCommand $Command.ToLower()
    } elseif (Test-Interactive) {
        Show-Menu
    } else {
        Show-Usage
    }
} catch {
    Write-Host (L "错误：$($_.Exception.Message)" "Error: $($_.Exception.Message)") -ForegroundColor Red
    exit 1
}
exit 0
