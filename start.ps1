<#
.SYNOPSIS
    银行 AI Agent 一键启动：Agent 后端(:8000) + 模拟银行(:8001) + 前端(:5173)。

.DESCRIPTION
    流程：环境检查 -> 依赖补齐 -> 数据库迁移与种子 -> 后台拉起三个服务 -> 健康检查 -> 打开浏览器。

    三个服务都在后台运行，日志落在 logs/：
        backend.out.log   / backend.err.log
        bank.out.log      / bank.err.log
        frontend.out.log  / frontend.err.log

    停止服务请运行 stop.ps1，或双击 停止项目.bat。

.PARAMETER Database
    连哪套数据库。cloud（默认）读仓库根 database.txt，行为与以前完全一致；
    local 读 database.local.txt（本机 PostgreSQL），并通过 DATABASE_FILE 环境变量
    把应用、两边 alembic、种子、checkpointer 和建库脚本一起切到本机实例。

    本机模式下如果银行库是空的（没有 simulation_state），脚本会在启动服务前自动补一次
    银行演示种子（基础种子 + 理财 + 订阅 + 客户池）。云端模式和已有数据的库都不会触发，
    见「银行首次初始化」一节的三条守卫。

.PARAMETER Restart
    先停掉已经在跑的服务，再重新启动。

.PARAMETER SkipMigrate
    跳过数据库迁移与种子，只启动服务（最快，适合一天内反复重启）。

.PARAMETER NoBrowser
    不自动打开浏览器。

.PARAMETER TimeoutSec
    单个服务等待健康检查的秒数，默认 120。

.PARAMETER Follow
    启动完成后持续跟踪三个服务的日志。Ctrl+C 只退出跟踪，服务继续运行。

.EXAMPLE
    ./start.ps1

.EXAMPLE
    ./start.ps1 -Restart -SkipMigrate
#>
[CmdletBinding()]
param(
    [ValidateSet('cloud', 'local')]
    [string]$Database = 'cloud',
    [switch]$Restart,
    [switch]$SkipMigrate,
    [switch]$NoBrowser,
    [int]$TimeoutSec = 120,
    [switch]$Follow
)

$ErrorActionPreference = 'Stop'

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

# 健康检查用 Invoke-WebRequest 轮询，PS 5.1 默认会画进度条。进度条走的是另一条
# 控制台通道，和 Write-Host 的输出会互相穿插，日志变成「[OK] 本本机机…」这种叠字。
$ProgressPreference = 'SilentlyContinue'

$Root       = $PSScriptRoot
$LogDir     = Join-Path $Root 'logs'
$PidFile    = Join-Path $LogDir 'pids.json'
$StopScript = Join-Path $Root 'stop.ps1'
$FrontendUrl = 'http://127.0.0.1:5173/'

# 两套数据库入口共用这一份实现，区别只在凭据来源：cloud 读仓库根 database.txt，
# local 读 database.local.txt。切换靠 DATABASE_FILE 环境变量，子进程全部继承它，
# 所以两个应用、两边 alembic、种子、checkpointer 和建库脚本会一起切过去。
# 不用 DATABASE_URL 的原因：Agent 连 test、模拟银行连 bank_db，一个变量没法同时给
# 两个兄弟进程不同的库名。
$DatabaseFile  = Join-Path $Root 'database.txt'
$DatabaseTitle = '云端'
if ($Database -eq 'local') {
    $DatabaseFile  = Join-Path $Root 'database.local.txt'
    $DatabaseTitle = '本机'
    $env:DATABASE_FILE = $DatabaseFile
}

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

# 子进程输出必须走 UTF-8 且不缓冲：否则中文日志在重定向文件里会变成乱码，
# 而且崩溃信息要等到缓冲区写满才出现，健康检查超时时就看不到原因。
$env:PYTHONUTF8       = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

# ---------------------------------------------------------------- 输出
function Write-Step  { param([string]$Text) Write-Host ''; Write-Host ('== ' + $Text) -ForegroundColor Cyan }
function Write-Ok    { param([string]$Text) Write-Host ('  [OK]   ' + $Text) -ForegroundColor Green }
function Write-Note  { param([string]$Text) Write-Host ('  [..]   ' + $Text) -ForegroundColor DarkGray }
function Write-Caution { param([string]$Text) Write-Host ('  [WARN] ' + $Text) -ForegroundColor Yellow }
function Write-Fail  { param([string]$Text) Write-Host ('  [FAIL] ' + $Text) -ForegroundColor Red }

# ---------------------------------------------------------------- 基础工具
function Test-PortOpen {
    param([int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(600)) { return $false }
        $client.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Read-DatabaseEndpoint {
    param([string]$Path)
    $endpoint = [pscustomobject]@{ Host = ''; Port = 0 }
    if (-not (Test-Path $Path)) { return $endpoint }
    foreach ($line in (Get-Content -Path $Path -Encoding UTF8)) {
        if ($line -match '^外网地址[：:]\s*(.+?)\s*$') { $endpoint.Host = $Matches[1] }
        elseif ($line -match '^端口[：:]\s*(\d+)') { $endpoint.Port = [int]$Matches[1] }
    }
    return $endpoint
}

function Repair-DuplicateEnvironmentNames {
    # Windows 的环境块允许 `HTTPS_PROXY` 与 `https_proxy` 同时存在，而 PowerShell 5.1 的
    # `Start-Process` 用**区分大小写**的字典拷贝环境，撞上这种成对出现就抛
    # 「已添加项。字典中的关键字:"HTTPS_PROXY"所添加的关键字:"https_proxy"」。
    #
    # 脚本开头设了 `$ErrorActionPreference='Stop'`，于是第一个 `Start-Process` 就把整次启动
    # 打死：三个服务一个都没起来，前端自然也没有，症状就是「5173 打不开」，而控制台看不出
    # 是环境变量的锅。上面走 `& $Exe` 的 Invoke-Native 不受影响，所以迁移、种子都正常，
    # 只有「启动服务」这一步炸——现象很容易被误判成前端坏了。
    #
    # 收敛方式是每个名字只留一个拼写、值不变（Windows 的环境变量本来就大小写不敏感）。
    $vars = [System.Environment]::GetEnvironmentVariables([System.EnvironmentVariableTarget]::Process)
    $groups = @{}
    foreach ($name in @($vars.Keys)) {
        $key = ([string]$name).ToUpperInvariant()
        if ($groups.ContainsKey($key)) { $groups[$key] += @([string]$name) }
        else { $groups[$key] = @([string]$name) }
    }

    $repaired = @()
    foreach ($key in @($groups.Keys)) {
        $names = @($groups[$key])
        $value = [string]$vars[$names[0]]
        # httpx（OpenAI SDK 内部使用）无法解析 NO_PROXY 里的方括号 IPv6 写法，
        # 例如 "[::1]" 会抛 InvalidURL: Invalid port: ':1]'，导致模型启动探测失败。
        # 本应用连本机模型只需 localhost/127.0.0.1，这里去掉方括号项和 IPv6 回环。
        if ($key -eq 'NO_PROXY' -and $value) {
            $value = (($value -split ',') |
                ForEach-Object { $_.Trim() } |
                Where-Object { $_ -and $_ -ne '::1' -and $_ -notmatch '^\[.*\]$' }) -join ','
        }
        if ($names.Count -lt 2) {
            if ($key -eq 'NO_PROXY') { [System.Environment]::SetEnvironmentVariable($names[0], $value) }
            continue
        }
        foreach ($name in $names) { [System.Environment]::SetEnvironmentVariable($name, $null) }
        [System.Environment]::SetEnvironmentVariable($names[0], $value)
        $repaired += ($names -join ' / ')
    }
    return $repaired
}

function Wait-Health {
    param([string]$Url, [int]$TimeoutSec)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
            if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 700
    }
    return $false
}

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [string]$WorkDir)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $output = ''
    $code = 1
    Push-Location $WorkDir
    try {
        $output = & $Exe $Arguments 2>&1 | Out-String
        if ($null -ne $LASTEXITCODE) { $code = $LASTEXITCODE }
    } catch {
        $output = $_.Exception.Message
        $code = 1
    } finally {
        Pop-Location
        $ErrorActionPreference = $previous
    }
    return [pscustomobject]@{ Code = $code; Output = $output }
}

function Show-LogTail {
    param([string]$Name, [int]$Lines = 30)
    foreach ($suffix in @('.out.log', '.err.log')) {
        $file = Join-Path $LogDir ($Name + $suffix)
        if (-not (Test-Path $file)) { continue }
        $tail = @(Get-Content -Path $file -Tail $Lines -ErrorAction SilentlyContinue)
        if ($tail.Count -eq 0) { continue }
        Write-Host ('  --- ' + $Name + $suffix + ' 末尾 ' + $tail.Count + ' 行 ---') -ForegroundColor DarkGray
        foreach ($line in $tail) { Write-Host ('    ' + $line) -ForegroundColor DarkGray }
    }
}

function Read-PidRecord {
    $record = @{}
    if (-not (Test-Path $PidFile)) { return $record }
    try {
        $data = Get-Content -Path $PidFile -Raw -ErrorAction Stop | ConvertFrom-Json
    } catch {
        return $record
    }
    if ($null -eq $data) { return $record }
    foreach ($property in $data.PSObject.Properties) {
        $record[$property.Name] = $property.Value
    }
    return $record
}

function Write-PidRecord {
    param([hashtable]$Record)
    $json = $Record | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($PidFile, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function Test-RecordedProcess {
    param($Entry, [string]$ExpectedExe)
    if (-not $Entry -or -not $Entry.pid -or -not $Entry.started) { return $false }
    try {
        $process = Get-Process -Id ([int]$Entry.pid) -ErrorAction Stop
        if (-not $process.Path -or [IO.Path]::GetFullPath($process.Path) -ne [IO.Path]::GetFullPath($ExpectedExe)) { return $false }
        if ($Entry.started -is [DateTime]) {
            $started = $Entry.started.ToUniversalTime()
        } elseif ($Entry.started -is [DateTimeOffset]) {
            $started = $Entry.started.UtcDateTime
        } else {
            $started = [DateTimeOffset]::Parse([string]$Entry.started).UtcDateTime
        }
        return [Math]::Abs(($process.StartTime.ToUniversalTime() - $started).TotalSeconds) -lt 2
    } catch { return $false }
}

function Find-ServicePort {
    param([int]$Preferred, [int[]]$Reserved, $Entry, [string]$ExpectedExe)
    if ((Test-RecordedProcess $Entry $ExpectedExe) -and $Entry.port -and (Test-PortOpen ([int]$Entry.port))) {
        return [int]$Entry.port
    }
    if (-not (Test-PortOpen $Preferred) -and $Preferred -notin $Reserved) { return $Preferred }
    for ($candidate = $Preferred + 1; $candidate -le 65535; $candidate++) {
        if ($candidate -notin $Reserved -and -not (Test-PortOpen $candidate)) { return $candidate }
    }
    throw '找不到可用端口'
}

# ---------------------------------------------------------------- 服务定义
$NodeExe = $null
$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
if ($nodeCommand) { $NodeExe = $nodeCommand.Source }

$Services = @(
    [pscustomobject]@{
        Name   = 'backend'
        Title  = 'Agent 后端'
        Dir    = (Join-Path $Root 'backend')
        Exe    = (Join-Path $Root 'backend/.venv/Scripts/python.exe')
        Args   = @('scripts/serve.py')
        Port   = 8000
        Health = 'http://127.0.0.1:8000/api/health'
        Docs   = 'http://127.0.0.1:8000/docs'
    },
    [pscustomobject]@{
        Name   = 'bank'
        Title  = '模拟银行'
        Dir    = (Join-Path $Root 'bank_backend')
        Exe    = (Join-Path $Root 'bank_backend/.venv/Scripts/python.exe')
        Args   = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8001')
        Port   = 8001
        Health = 'http://127.0.0.1:8001/api/health'
        Docs   = 'http://127.0.0.1:8001/docs'
    },
    [pscustomobject]@{
        Name   = 'frontend'
        Title  = '前端'
        Dir    = (Join-Path $Root 'frontend')
        Exe    = $NodeExe
        Args   = @('node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', '5173', '--strictPort')
        Port   = 5173
        Health = 'http://127.0.0.1:5173/'
        Docs   = 'http://127.0.0.1:5173/'
    }
)

# ---------------------------------------------------------------- 启动
Write-Host ''
Write-Host '  银行 AI Agent 一键启动' -ForegroundColor White
Write-Host '  --------------------------------------------------' -ForegroundColor DarkGray
Write-Host ('  仓库: ' + $Root) -ForegroundColor DarkGray
Write-Host ('  时间: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -ForegroundColor DarkGray
Write-Host ('  数据库: ' + $DatabaseTitle + '（' + (Split-Path $DatabaseFile -Leaf) + '）') -ForegroundColor DarkGray

if ($Restart) {
    Write-Step '停止已在运行的服务 (-Restart)'
    # 只停止经 PID、可执行路径与启动时间核验的本工程进程，不清理其他项目的端口。
    if (Test-Path $StopScript) {
        & $StopScript -Quiet
    } else {
        Write-Caution ('找不到 ' + $StopScript + '，跳过停止步骤')
    }
}

# 占用端口不代表是本工程。只有记录中身份仍匹配的进程可以复用。
$running = Read-PidRecord
$selectedPorts = @()
foreach ($svc in $Services) {
    $preferred = $svc.Port
    $entry = $running[$svc.Name]
    if ((Test-RecordedProcess $entry $svc.Exe) -and $entry.database -and $entry.database -ne $Database) {
        Write-Fail '已有本工程进程连接另一套数据库，请用 -Restart 明确切换。'
        exit 1
    }
    $reserved = @($selectedPorts) + @($Services | Where-Object { $_.Name -ne $svc.Name } | ForEach-Object { $_.Port })
    $svc.Port = Find-ServicePort -Preferred $preferred -Reserved $reserved -Entry $entry -ExpectedExe $svc.Exe
    if ($svc.Port -ne $preferred) {
        Write-Caution ($svc.Title + ' 默认端口 :' + $preferred + ' 被占用，本工程使用 :' + $svc.Port)
    }
    $selectedPorts += $svc.Port
    $svc.Health = 'http://127.0.0.1:' + $svc.Port + $(if ($svc.Name -eq 'frontend') { '/' } else { '/api/health' })
    $svc.Docs = 'http://127.0.0.1:' + $svc.Port + $(if ($svc.Name -eq 'frontend') { '/' } else { '/docs' })
}
$backendService = $Services | Where-Object { $_.Name -eq 'backend' } | Select-Object -First 1
$bankService = $Services | Where-Object { $_.Name -eq 'bank' } | Select-Object -First 1
$frontendService = $Services | Where-Object { $_.Name -eq 'frontend' } | Select-Object -First 1
$backendService.Args = @('scripts/serve.py', '--port', [string]$backendService.Port)
$bankService.Args = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', [string]$bankService.Port)
$frontendService.Args = @('node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', [string]$frontendService.Port, '--strictPort')
$FrontendUrl = $frontendService.Docs
$env:BANK_AGENT_API_TARGET = 'http://127.0.0.1:' + $backendService.Port
$env:BANK_BACKEND_BASE_URL = 'http://127.0.0.1:' + $bankService.Port + '/api'

Write-Step '检查运行环境'
if (-not $NodeExe) {
    Write-Fail '找不到 node.exe，请先安装 Node.js 22+ 并加入 PATH'
    exit 1
}
Write-Ok ('node  ' + $NodeExe)

$DatabaseEndpoint = Read-DatabaseEndpoint -Path $DatabaseFile
if ($Database -eq 'local') {
    if (-not (Test-Path $DatabaseFile)) {
        Write-Fail ('找不到 ' + $DatabaseFile)
        Write-Note '它是被 .gitignore 忽略的本机凭据文件，不会随仓库分发，需要自己建一个。'
        Write-Note '四行内容，字段名照抄（冒号全角半角都认）：'
        Write-Note '  账户：bank'
        Write-Note '  密码：bank_local'
        Write-Note '  外网地址：127.0.0.1'
        Write-Note '  端口：55432'
        Write-Note '上面这组对应本项目的 Docker 容器 bank-agent-db-1。容器没起来时先执行：'
        Write-Note '  docker start bank-agent-db-1'
        Write-Note '用系统安装的 PostgreSQL 也行，那就把端口改成 5432、账户密码换成自己的。'
        exit 1
    }
    if (-not $DatabaseEndpoint.Host -or $DatabaseEndpoint.Port -le 0) {
        Write-Fail ($DatabaseFile + ' 里读不出「外网地址」或「端口」')
        Write-Note '检查这两行是否存在，以及端口是不是纯数字。'
        exit 1
    }
    if (-not (Test-PortOpen $DatabaseEndpoint.Port)) {
        Write-Fail ('本机 PostgreSQL 没有在 ' + $DatabaseEndpoint.Host + ':' + $DatabaseEndpoint.Port + ' 监听')
        if ($DatabaseEndpoint.Port -eq 55432) {
            # 本项目的本地库是 Docker 容器，不归系统服务管。
            Write-Note '这个端口对应 Docker 容器 bank-agent-db-1，先启动它：'
            Write-Note '  docker start bank-agent-db-1'
            Write-Note '查看状态：docker ps -a --filter name=bank-agent-db-1'
        } else {
            Write-Note '请先启动本机 PostgreSQL 服务；本脚本不会替你动系统服务。'
            Write-Note '若用的是 Docker 容器，把 database.local.txt 的端口改成容器映射出来的那个。'
        }
        Write-Note '要连云端数据库，请改用「启动项目-云端数据库.bat」。'
        exit 1
    }
    Write-Ok ('本机 PostgreSQL ' + $DatabaseEndpoint.Host + ':' + $DatabaseEndpoint.Port + ' 可达')
} elseif ($DatabaseEndpoint.Host) {
    Write-Ok ('云端 PostgreSQL ' + $DatabaseEndpoint.Host + ':' + $DatabaseEndpoint.Port)
}

$UvExe = $null
$uvCommand = Get-Command uv.exe -ErrorAction SilentlyContinue
if ($uvCommand) { $UvExe = $uvCommand.Source }

# 只补缺的：.venv 在就完全不碰，避免每次启动都做一遍解析。
$backendServices = @($Services | Where-Object { $_.Name -ne 'frontend' })
$missingVenv = @($backendServices | Where-Object { -not (Test-Path $_.Exe) })
if ($missingVenv.Count -gt 0) {
    if (-not $UvExe) {
        Write-Fail '存在未初始化的 .venv，但找不到 uv.exe，请先安装 uv 后重试'
        exit 1
    }
    foreach ($svc in $missingVenv) {
        Write-Note ($svc.Title + ' 缺少 .venv，执行 uv sync --frozen')
        $result = Invoke-Native -Exe $UvExe -Arguments @('sync', '--frozen') -WorkDir $svc.Dir
        if ($result.Code -ne 0) {
            Write-Fail ($svc.Title + ' 依赖安装失败')
            Write-Host $result.Output -ForegroundColor DarkGray
            exit 1
        }
        Write-Ok ($svc.Title + ' 依赖就绪')
    }
} else {
    Write-Ok '两个 Python 虚拟环境都在'
}

$viteEntry = Join-Path $Root 'frontend/node_modules/vite/bin/vite.js'
if (-not (Test-Path $viteEntry)) {
    Write-Note 'frontend 缺少 node_modules，执行 npm ci'
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npmCommand) { $npmCommand = Get-Command npm -ErrorAction SilentlyContinue }
    if (-not $npmCommand) {
        Write-Fail '找不到 npm，请先安装 Node.js 22+'
        exit 1
    }
    $result = Invoke-Native -Exe $npmCommand.Source -Arguments @('ci') -WorkDir (Join-Path $Root 'frontend')
    if ($result.Code -ne 0) {
        Write-Fail 'npm ci 失败'
        Write-Host $result.Output -ForegroundColor DarkGray
        exit 1
    }
    Write-Ok 'frontend 依赖就绪'
} else {
    Write-Ok 'frontend 依赖在'
}

# ---------------------------------------------------------------- 服务间凭证
# bank_backend 的所有客户级接口都要求 X-Service-Token，清库 / 推时钟 / 跑批额外要求
# X-Ops-Token。两把钥匙按机器生成一次、写进两个 .env（都在 .gitignore 里），之后原地复用：
#
#   backend/.env       BANK_SERVICE_TOKEN=...        （app 出站时带）
#   bank_backend/.env  SERVICE_TOKEN=...  OPS_TOKEN=...（bank 入站时校验）
#
# 写文件而不是只设进程环境变量，是为了覆盖不经过本脚本的路径：手工 uvicorn 起 8001、
# 只重启一个进程、pytest 直连 8001、运维脚本手工调 /api/seed/reset —— 它们都读各自目录
# 下的 .env，因此拿到的是同一把钥匙。
#
# 也**故意不**导出到当前进程环境：子进程会继承父进程环境，那样 backend 会连 OPS_TOKEN
# 一起拿到，等于把「清空全库」的钥匙塞进 app 进程，正好破坏这把钥匙存在的理由。
function Read-EnvValue {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path $Path)) { return $null }
    foreach ($line in [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)) {
        if ($line -match ('^\s*' + [regex]::Escape($Key) + '\s*=\s*(.*)$')) {
            $value = $Matches[1].Trim().Trim('"').Trim("'")
            if ($value -ne '') { return $value }
        }
    }
    return $null
}

function Set-EnvValue {
    param([string]$Path, [string]$Key, [string]$Value, [string]$Header = '')
    $lines = @()
    if (Test-Path $Path) {
        $lines = @([System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8))
    } elseif ($Header -ne '') {
        $lines = @($Header)
    }
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match ('^\s*' + [regex]::Escape($Key) + '\s*=')) {
            $lines[$i] = ($Key + '=' + $Value)
            $found = $true
        }
    }
    if (-not $found) { $lines = @($lines) + ($Key + '=' + $Value) }
    # 无 BOM：pydantic 能读带 BOM 的文件，但 BOM 会让第一条注释显示成乱码。
    [System.IO.File]::WriteAllText($Path, (($lines -join "`r`n") + "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
}

function New-ServiceToken {
    # 两段 GUID：128 位随机，够用于「证明请求来自本机后端」。这里不是用户口令，
    # 不需要慢哈希——它的生命周期与进程同长，且只在本机/内网传输。
    return ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N'))
}

Write-Step '准备服务间凭证'
$backendEnvPath = Join-Path $Root 'backend/.env'
$bankEnvPath    = Join-Path $Root 'bank_backend/.env'

# bank 侧是校验方，所以它是唯一事实来源：app 侧的值只允许与它相同。
$serviceToken = Read-EnvValue -Path $bankEnvPath -Key 'SERVICE_TOKEN'
$opsToken     = Read-EnvValue -Path $bankEnvPath -Key 'OPS_TOKEN'
$generated    = @()
if (-not $serviceToken) {
    $serviceToken = New-ServiceToken
    Set-EnvValue -Path $bankEnvPath -Key 'SERVICE_TOKEN' -Value $serviceToken -Header '# 本地生成，勿提交。bank_backend 校验入站请求用（X-Service-Token）。'
    $generated += 'SERVICE_TOKEN'
}
if (-not $opsToken) {
    $opsToken = New-ServiceToken
    Set-EnvValue -Path $bankEnvPath -Key 'OPS_TOKEN' -Value $opsToken
    $generated += 'OPS_TOKEN'
}
$appToken = Read-EnvValue -Path $backendEnvPath -Key 'BANK_SERVICE_TOKEN'
if ($appToken -ne $serviceToken) {
    Set-EnvValue -Path $backendEnvPath -Key 'BANK_SERVICE_TOKEN' -Value $serviceToken -Header '# 本地生成，勿提交。app 出站调 bank_backend 用（X-Service-Token）。'
    if ($appToken) { $generated += 'BANK_SERVICE_TOKEN(已与银行侧对齐)' } else { $generated += 'BANK_SERVICE_TOKEN' }
}

if ($generated.Count -gt 0) {
    Write-Ok ('已生成/对齐：' + ($generated -join '、'))
} else {
    Write-Ok '两侧凭证已存在且一致'
}
Write-Note ('  ' + $bankEnvPath + '  ← SERVICE_TOKEN / OPS_TOKEN')
Write-Note ('  ' + $backendEnvPath + '  ← BANK_SERVICE_TOKEN')

# ---------------------------------------------------------------- 数据库
if ($SkipMigrate) {
    Write-Step '跳过数据库迁移与种子 (-SkipMigrate)'
} else {
    Write-Step '准备数据库（迁移 + 种子）'
    # 说明两点边界：
    #  1. init_test_database.py 是幂等的，库已存在时什么都不做，所以每次都跑是安全的。
    #  2. 模拟银行只跑迁移。bank_backend/README.md 明确要求 seed/reset 必须由开发者显式执行，
    #     而且 POST /api/seed/reset 会清空业务数据，绝不能塞进一键启动里。
    $steps = @()
    if ($Database -eq 'local') {
        # 云端实例上 bank_db 早就存在；本机实例没有，所以这一步只在本地模式跑（幂等）。
        $steps += [pscustomobject]@{ Label = '银行: 确保 bank_db 库存在'; Dir = 'bank_backend'; Args = @('scripts/init_database.py') }
    }
    $steps += @(
        [pscustomobject]@{ Label = 'Agent: 确保 test 库存在';      Dir = 'backend';      Args = @('scripts/init_test_database.py') },
        [pscustomobject]@{ Label = 'Agent: alembic upgrade head'; Dir = 'backend';      Args = @('-m', 'alembic', 'upgrade', 'head') },
        [pscustomobject]@{ Label = 'Agent: 校对演示种子';          Dir = 'backend';      Args = @('-m', 'app.storage.seed') },
        [pscustomobject]@{ Label = 'Agent: 初始化 checkpoint 表';  Dir = 'backend';      Args = @('scripts/init_checkpointer.py') },
        [pscustomobject]@{ Label = '银行: alembic upgrade head';  Dir = 'bank_backend'; Args = @('-m', 'alembic', 'upgrade', 'head') }
    )
    $preserveExternalAgentSchema = $false
    foreach ($step in $steps) {
        if ($preserveExternalAgentSchema -and $step.Dir -eq 'backend') { continue }
        $dir = Join-Path $Root $step.Dir
        $python = Join-Path $dir '.venv/Scripts/python.exe'
        Write-Note $step.Label
        $result = Invoke-Native -Exe $python -Arguments $step.Args -WorkDir $dir
        if ($result.Code -ne 0) {
            if ($step.Label -eq 'Agent: alembic upgrade head' -and $result.Output -match "Can't locate revision identified by") {
                Write-Caution 'Agent 数据库使用团队其他迁移版本，先检查当前运行所需结构；不覆盖迁移历史。'
                $compatibility = Invoke-Native -Exe $python -Arguments @('scripts/check_runtime_schema.py') -WorkDir $dir
                if ($compatibility.Code -eq 0) {
                    Write-Caution '结构兼容：保留现有 Agent 数据库及迁移版本，跳过旧迁移和种子。'
                    $preserveExternalAgentSchema = $true
                    continue
                }
                Write-Host $compatibility.Output -ForegroundColor DarkGray
            }
            Write-Fail ($step.Label + ' 失败（退出码 ' + $result.Code + '）')
            Write-Host $result.Output -ForegroundColor DarkGray
            exit 1
        }
        $lines = @($result.Output -split [Environment]::NewLine | Where-Object { $_.Trim() -ne '' })
        if ($lines.Count -gt 0) { Write-Note ('  ' + $lines[-1].Trim()) }
    }
    Write-Ok '数据库已就绪'
}

# ---------------------------------------------------------------- 银行首次初始化
# 这里解决一个必然出现的死结：bank 的 /api/health 会校验「模拟状态」是否存在，
# 全新库上它只能 503；而补种子的 POST /api/seed/reset 会清空整库，所以
# bank_backend/README.md 规定它只能由开发者显式执行。两条撞在一起，新库永远等不到健康。
#
# 判断放在启动服务之前，直接查库而不是探 /api/health：health 的 503 响应体只有
# {"code":1005,"message":"invalid_state"}，具体原因（未初始化 / 余额不平 / 连不上）
# 并不外泄，靠它区分不出来。scripts.init_demo_data --check 读 simulation_state，
# 库为空才返回 3。放在拉起进程之前还有个好处：银行进程一启动读到的就是种子数据。
#
# 守卫是三条的交集，任何一条不成立就一个字都不写：
#   1. 本机模式。云端库里可能存着真实演示数据，一律不碰。
#   2. 没带 -SkipMigrate。那个开关的语义就是「别动数据库」。
#   3. 库里没有 simulation_state。这是库为空的确证，说明没有业务数据可清，
#      此时 reset 与「清空数据」的顾虑不冲突。
if ($Database -eq 'local' -and -not $SkipMigrate) {
    $bankService = $Services | Where-Object { $_.Name -eq 'bank' } | Select-Object -First 1
    $bankDir     = $bankService.Dir
    $bankPython  = $bankService.Exe

    $probe = Invoke-Native -Exe $bankPython -Arguments @('-m', 'scripts.init_demo_data', '--check') -WorkDir $bankDir
    if ($probe.Code -eq 3) {
        Write-Step '模拟银行首次初始化（本机空库，自动补种子）'
        Write-Note '只在「本机模式 + 库为空」时触发，库已有数据时这一步不会执行'

        $init = Invoke-Native -Exe $bankPython -Arguments @('-m', 'scripts.init_demo_data') -WorkDir $bankDir
        if ($init.Code -ne 0) {
            Write-Fail ('银行演示数据初始化失败（退出码 ' + $init.Code + '）')
            Write-Host $init.Output -ForegroundColor DarkGray
            exit 1
        }
        # 成功时不回显输出：三个种子脚本都会打整段 JSON 快照，几百行淹掉启动日志。
        Write-Ok '银行演示数据已就绪（基础种子 + 理财 + 订阅 + 客户池）'
    } elseif ($probe.Code -eq 0) {
        Write-Ok '模拟银行已有数据，跳过首次初始化'
    } else {
        # 连不上库或脚本报错：不要继续往下走，否则后面只会看到一堆 503。
        Write-Fail ('无法判断银行库状态（退出码 ' + $probe.Code + '）')
        Write-Host $probe.Output -ForegroundColor DarkGray
        exit 1
    }
}

# ---------------------------------------------------------------- 拉起进程
Write-Step '启动服务'

# 必须在第一个 Start-Process 之前跑：大小写重复的环境变量会让它直接抛异常。
$repairedEnv = @(Repair-DuplicateEnvironmentNames)
if ($repairedEnv.Count -gt 0) {
    Write-Caution ('环境变量存在大小写重复，已收敛为单一拼写：' + ($repairedEnv -join '；'))
}

$previous = Read-PidRecord
$record = @{}
foreach ($key in $previous.Keys) {
    $entry = $previous[$key]
    $alive = $false
    $definition = $Services | Where-Object { $_.Name -eq $key } | Select-Object -First 1
    if ($definition) { $alive = Test-RecordedProcess $entry $definition.Exe }
    if ($alive) { $record[$key] = $entry }
}

$reusedPort = $false
foreach ($svc in $Services) {
    if (Test-PortOpen $svc.Port) {
        if (-not (Test-RecordedProcess $record[$svc.Name] $svc.Exe)) {
            Write-Fail ($svc.Title + ' :' + $svc.Port + ' 在启动过程中被其他进程占用，请重试。')
            exit 1
        }
        Write-Note ($svc.Title + ' :' + $svc.Port + ' 沿用本工程已核验的进程')
        $reusedPort = $true
        continue
    }
    if (-not (Test-Path $svc.Exe)) {
        Write-Fail ('找不到可执行文件 ' + $svc.Exe)
        exit 1
    }
    $outLog = Join-Path $LogDir ($svc.Name + '.out.log')
    $errLog = Join-Path $LogDir ($svc.Name + '.err.log')
    # 不要在这里预写时间戳：Start-Process 打开重定向文件时会截断它，
    # 而且子进程持有句柄期间文件是独占的，追加不进去。
    $process = $null
    try {
        $process = Start-Process -FilePath $svc.Exe -ArgumentList $svc.Args -WorkingDirectory $svc.Dir -RedirectStandardOutput $outLog -RedirectStandardError $errLog -WindowStyle Hidden -PassThru -ErrorAction Stop
    } catch {
        # 拉起失败必须说清是哪个服务、什么原因，并把已经起来的收回去；否则脚本会以一个
        # 裸异常退出，剩下的服务半死不活，看上去就像「前端没起来」却查不到原因。
        Write-Fail ($svc.Title + ' 启动失败：' + $_.Exception.Message)
        if (Test-Path $StopScript) { & $StopScript -Quiet -PidsOnly }
        exit 1
    }
    $record[$svc.Name] = [pscustomobject]@{
        pid     = $process.Id
        exe     = $svc.Exe
        port    = $svc.Port
        started = $process.StartTime.ToUniversalTime().ToString('o')
        database = $Database
    }
    Write-Ok ($svc.Title + ' 已启动  PID ' + $process.Id + '  :' + $svc.Port)
    Start-Sleep -Milliseconds 400
}
Write-PidRecord -Record $record

if ($reusedPort) {
    Write-Note '已复用本工程进程；更新代码后请使用 -Restart。'
}

# ---------------------------------------------------------------- 健康检查
Write-Step ('等待服务就绪（每个最长 ' + $TimeoutSec + ' 秒）')
$failed = @()
foreach ($svc in $Services) {
    Write-Note ($svc.Title + '  ' + $svc.Health)
    if (Wait-Health -Url $svc.Health -TimeoutSec $TimeoutSec) {
        Write-Ok ($svc.Title + ' 就绪')
    } else {
        Write-Fail ($svc.Title + ' 未在 ' + $TimeoutSec + ' 秒内就绪')
        $failed += $svc
    }
}

if ($failed.Count -gt 0) {
    Write-Host ''
    Write-Fail '有服务启动失败，回滚本次启动的进程'
    foreach ($svc in $failed) { Show-LogTail -Name $svc.Name }
    if (Test-Path $StopScript) { & $StopScript -Quiet -PidsOnly }
    exit 1
}

# ---------------------------------------------------------------- 收尾
if (-not $NoBrowser) { Start-Process $FrontendUrl | Out-Null }

Write-Host ''
Write-Host '  全部就绪' -ForegroundColor Green
Write-Host '  --------------------------------------------------' -ForegroundColor DarkGray
Write-Host ('  前端        ' + $FrontendUrl) -ForegroundColor White
Write-Host ('  Agent API   ' + $backendService.Docs) -ForegroundColor White
Write-Host ('  模拟银行    ' + $bankService.Docs) -ForegroundColor White
Write-Host ('  数据库      ' + $DatabaseTitle + '  ' + $DatabaseEndpoint.Host + ':' + $DatabaseEndpoint.Port + '（test + bank_db）') -ForegroundColor White
Write-Host ('  日志        ' + $LogDir) -ForegroundColor DarkGray
Write-Host ('  停止        ' + (Join-Path $Root 'stop.ps1')) -ForegroundColor DarkGray
Write-Host ''

if ($Follow) {
    Write-Step '跟踪日志（Ctrl+C 退出跟踪，服务继续运行）'
    $targets = @{}
    foreach ($svc in $Services) {
        foreach ($suffix in @('.out.log', '.err.log')) {
            $targets[(Join-Path $LogDir ($svc.Name + $suffix))] = $svc.Title
        }
    }
    $offset = @{}
    foreach ($file in $targets.Keys) {
        if (Test-Path $file) { $offset[$file] = (Get-Item $file).Length } else { $offset[$file] = 0 }
    }
    try {
        while ($true) {
            foreach ($file in @($targets.Keys)) {
                if (-not (Test-Path $file)) { continue }
                $length = (Get-Item $file).Length
                if ($length -le $offset[$file]) { continue }
                $stream = [System.IO.File]::Open($file, 'Open', 'Read', 'ReadWrite')
                try {
                    $stream.Seek($offset[$file], 'Begin') | Out-Null
                    $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
                    $text = $reader.ReadToEnd()
                    $reader.Close()
                } finally {
                    $stream.Close()
                }
                $offset[$file] = $length
                foreach ($line in $text.Split([char]10)) {
                    $clean = $line.TrimEnd([char]13)
                    if ($clean -ne '') { Write-Host ('[' + $targets[$file] + '] ' + $clean) }
                }
            }
            Start-Sleep -Milliseconds 500
        }
    } finally {
        Write-Host ''
        Write-Host '  已停止跟踪日志，服务仍在后台运行。' -ForegroundColor DarkGray
    }
}

exit 0
