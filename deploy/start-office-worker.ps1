<#
.SYNOPSIS
  Sherpa Office ワーカーの起動一本化ランチャー（OFFICE-WIN-001・
  2026-07-20-調査型RAG詳細修正計画.html §6.5「設定を簡単にする」）。

.DESCRIPTION
  同ディレクトリ（既定）の `office-worker.json` を読み込み、bind/port/token/max_file_bytes/
  timeout_seconds/temp_dir を解決して `office-com-worker.ps1` を起動する。起動前に Word/Excel/
  PowerPoint の検出状態（レジストリの ProgID CurVer 判定・COM は起動しない＝軽量）をログへ出す。

  設定ファイルは必須ではない（無ければ office-com-worker.ps1 自身の既定・env にフォールバックする）。
  このスクリプトは「設定を一か所にまとめる」ための任意の入口であり、従来どおり
  `office-com-worker.ps1` を直接起動する手順（env 変数のみ）もそのまま使える（後方互換）。

  Office 検出・設定読込・ログの3点をまとめる以外の新しいロジックは持たない（実処理・エンドポイントは
  すべて office-com-worker.ps1 側・このスクリプトは薄いランチャー）。

.PARAMETER ConfigPath
  office-worker.json のパス（既定: このスクリプトと同じディレクトリの office-worker.json）。

.PARAMETER LogFile
  ログの追記先ファイル（省略可・省略時は標準出力のみ）。

.EXAMPLE
  # office-worker.json を deploy/ に置いてから:
  powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\start-office-worker.ps1
#>
[CmdletBinding()]
param(
    [string]$ConfigPath = "",
    [string]$LogFile = ""
)

$ErrorActionPreference = "Stop"

$script:ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $ConfigPath) { $ConfigPath = Join-Path $script:ScriptDir "office-worker.json" }

function Write-Log([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    if ($LogFile) {
        try { Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8 } catch {}
    }
}

# ---- 設定読込（office-worker.json・任意）----

$cfg = @{}
if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
    try {
        $raw = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8
        $parsed = $raw | ConvertFrom-Json
        foreach ($prop in $parsed.PSObject.Properties) { $cfg[$prop.Name] = $prop.Value }
        Write-Log ("設定ファイルを読み込みました: {0}" -f $ConfigPath)
    } catch {
        Write-Log ("設定ファイルの読み込みに失敗しました（既定値/env にフォールバックします）: {0}（{1}）" -f $ConfigPath, $_.Exception.Message)
    }
} else {
    Write-Log ("設定ファイルが見つかりません（既定値/env にフォールバックします）: {0}" -f $ConfigPath)
}

function Get-CfgValue([string]$Key) {
    if ($cfg.ContainsKey($Key) -and $null -ne $cfg[$Key] -and $cfg[$Key] -ne "") { return $cfg[$Key] }
    return $null
}

$bindAddress = Get-CfgValue "bind"
$port = Get-CfgValue "port"
$token = Get-CfgValue "token"
$maxFileBytes = Get-CfgValue "max_file_bytes"
$timeoutSeconds = Get-CfgValue "timeout_seconds"
$tempDir = Get-CfgValue "temp_dir"

# ---- Office 検出（COM を起動しないレジストリ判定・office-com-worker.ps1 -Healthz と同じ軽量ロジック）----

function Get-OneOfficeVersionQuick([string]$ProgId) {
    try {
        $curver = (Get-ItemProperty -Path ("Registry::HKEY_CLASSES_ROOT\{0}\CurVer" -f $ProgId) -ErrorAction Stop).'(default)'
    } catch {
        return $false
    }
    if ($curver -match '\.(\d+)$') { return ($matches[1] + '.0') }
    return $true
}

Write-Log "Office 検出（レジストリ判定・COM は起動しません）:"
foreach ($app in @(
    @{ Name = "Word"; ProgId = "Word.Application" },
    @{ Name = "Excel"; ProgId = "Excel.Application" },
    @{ Name = "PowerPoint"; ProgId = "PowerPoint.Application" })) {
    $v = Get-OneOfficeVersionQuick $app.ProgId
    if ($v -eq $false) {
        Write-Log ("  {0}: 未検出" -f $app.Name)
    } elseif ($v -eq $true) {
        Write-Log ("  {0}: 検出（バージョン不明）" -f $app.Name)
    } else {
        Write-Log ("  {0}: 検出（バージョン {1}）" -f $app.Name, $v)
    }
}

if (-not $token -and -not $env:SHERPA_OFFICE_COM_TOKEN) {
    Write-Log "警告: token が office-worker.json にも env SHERPA_OFFICE_COM_TOKEN にも設定されていません。office-com-worker.ps1 は共有シークレット必須のため起動できません。"
}

# ---- office-com-worker.ps1 起動（同ディレクトリ）----

$workerPath = Join-Path $script:ScriptDir "office-com-worker.ps1"
if (-not (Test-Path -LiteralPath $workerPath -PathType Leaf)) {
    Write-Error ("office-com-worker.ps1 が見つかりません: {0}" -f $workerPath)
    exit 2
}

$workerArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-STA", "-File", $workerPath)
if ($bindAddress) { $workerArgs += @("-BindAddress", [string]$bindAddress) }
if ($port) { $workerArgs += @("-Port", [int]$port) }
if ($token) { $workerArgs += @("-Token", [string]$token) }
if ($timeoutSeconds) { $workerArgs += @("-TimeoutSec", [int]$timeoutSeconds) }
if ($maxFileBytes) { $workerArgs += @("-MaxFileBytes", [long]$maxFileBytes) }
if ($tempDir) { $workerArgs += @("-TempDir", [string]$tempDir) }

Write-Log ("office-com-worker.ps1 を起動します: bind={0} port={1} timeout_seconds={2} max_file_bytes={3} temp_dir={4}" -f `
    ($(if ($bindAddress) { $bindAddress } else { "既定(127.0.0.1)" })), `
    ($(if ($port) { $port } else { "既定(env/8091)" })), `
    ($(if ($timeoutSeconds) { $timeoutSeconds } else { "既定(env/120)" })), `
    ($(if ($maxFileBytes) { $maxFileBytes } else { "既定(env/500MiB)" })), `
    ($(if ($tempDir) { $tempDir } else { "既定(env/%TEMP%)" })))

& powershell.exe @workerArgs
exit $LASTEXITCODE
