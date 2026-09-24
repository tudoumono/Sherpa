<#
.SYNOPSIS
  Sherpa Office COM ワーカー（W1・2026-07-08-旧Office変換2系統.md / INGEST-MD §5.6「実装境界」）。

.DESCRIPTION
  Windows 側で単体起動する HTTP ワーカー。WSL コア（sherpa.ingest.arms.legacy_convert の
  office_com バックエンド）が HTTP で呼び、旧形式 Office（.doc/.xls/.ppt）を本物の Office COM で
  新形式（.docx/.xlsx/.pptx）へ忠実変換して返す。COM interop に触れるのはこのワーカー1つだけ
  （＝interop の唯一の境界。WSL/Linux コアは COM を直接叩かない）。

  設計上の制約（INGEST-MD の決定に従う）:
    - 既定 127.0.0.1 のみ bind（LAN へは出さない・非 loopback を明示指定したら起動時に警告）。
      共有シークレット（X-Sherpa-Token・大文字小文字を区別して比較）必須。
    - 変換は直列（1件ずつ）。COM は並列不可のため。
    - 1件タイムアウト（既定120s・超過で子プロセスと「この変換が作った」Office プロセスだけを kill）。
      kill 対象は New-Object 直後に記録した PID＋CreationDate で識別し、キル直前に再検証する
      （PID 再利用ガード・要求開始前から存在した＝アタッチなら絶対に殺さない）。
      ⚠ PowerPoint は単一インスタンス性を持ち、ユーザーが既に開いている場合 COM がそこへアタッチする
      ことがある。その場合は差分が空になり**絶対に kill しない**（未保存の作業を守る・ハングは子プロセス
      kill のみで妥協＝ワーカー再起動が必要になることがある。40-運用.md に運用上の注意を記載）。
    - パス検証はホワイトリスト方式（絶対のローカルドライブ/UNC のみ・device/extended-length・ADS・
      変則 UNC サーバ名は拒否・GetFullPath 正規化後に再検証）。
    - マクロ強制無効（AutomationSecurity=ForceDisable）・DisplayAlerts 無効・ReadOnly で開く。
    - ワーカーは原本を読むだけ（書かない）。一時出力は %TEMP% に作り応答後に削除。
    - 変換1件ごとに自分自身を子プロセス（-ConvertOnce）として起動し、Office を処理毎に Quit
      （リーク防止・堅牢性優先）。ハングした場合も子プロセスごと確実に殺せる。

  OFFICE-WIN-001（2026-07-20-調査型RAG詳細修正計画.html §6.5「Windows Officeワーカー」）: 既存の
  path 方式（/convert・/render・Windows から見える絶対パス/UNC を JSON で受け取る）はそのまま維持し、
  共有ストレージが無い独立 Linux サーバー向けに **ファイル本体を受け取る** upload 系エンドポイントを
  追加する（/convert-upload・/render-upload・multipart/form-data）。file・target（convert のみ）・
  source_hash（原本の sha256 hex・受信バイトと突合し不一致は 400）を受け取り、`MaxFileBytes` 超過は
  413（Content-Length 事前チェック＋ストリーム読取中の打ち切りの二重）。拡張子ホワイトリストは既存の
  `$script:ExtMap`/`$script:RenderExtMap` を再利用（新たな許可表を作らない）。アップロードされた原本は
  ローカル一時ファイル（既定 `%TEMP%`・`-TempDir`/env `SHERPA_OFFICE_COM_TEMP_DIR`/office-worker.json
  `temp_dir` で変更可）へ書き、既存の `Invoke-OfficeJob`（直列・タイムアウト・「この変換が作った」Office
  だけを kill）へそのまま渡す＝COM 処理経路は path 方式と完全に共通。処理後は必ず削除する（原本を
  Windows 側へ残さない）。共有シークレット（X-Sherpa-Token）は既存の全ルート共通チェックがそのまま
  upload 系にも掛かる（listener 起動そのものが Token 必須＝upload だけを緩めることはしない）。

  OFFICE-WIN-001 ⑤（実装順⑤・PowerPoint 補助構造抽出の試作）: `/extract-structure-upload`
  （multipart: file=.ppt/.pptx・source_hash）を追加する。PowerPoint COM でスライドを開き、
  スライド番号・タイトル・本文テキスト・発表者ノート・非表示フラグ・図形（名前・種類・z-order・
  テキスト・可視性）を JSON で返す（COM から安定して取れる範囲のみ・意味判断はしない）。受信・検証・
  一時ファイル・削除は /convert-upload・/render-upload と全く同じ流儀（`Get-MultipartFile`・
  `Get-Sha256Hex`・トークン検査を再利用）。COM 呼び出しは既存の隔離子プロセス方式
  （`Invoke-OfficeJob` ＋新設 `-ExtractStructureOnce` 子プロセス）で行うため、タイムアウト時の
  「この抽出が起こした PowerPoint だけを kill する」安全策もそのまま効く。**試作段階**＝Sherpa 側
  （取り込みパイプライン・office_md.py 統合）へは未配線（呼び出し側の配線は将来スライス）。

  起動は `deploy/start-office-worker.ps1`（`office-worker.json` を読み込み Office 検出ログを出してから
  このスクリプトを起動する一本化ランチャー）からが既定の運用（直接このスクリプトを起動する従来手順も
  そのまま使える＝後方互換）。

  Windows 追加依存ゼロ（PowerShell 5.1 標準の System.Net.HttpListener と Office COM のみ）。

.PARAMETER Port
  Listen ポート（既定: env SHERPA_OFFICE_COM_PORT ＞ 8091）。

.PARAMETER BindAddress
  bind アドレス（既定 127.0.0.1・ローカルのみ）。

.PARAMETER Token
  共有シークレット（既定: env SHERPA_OFFICE_COM_TOKEN）。未設定だと起動を拒否（fail-closed）。

.PARAMETER TimeoutSec
  1件あたりの変換タイムアウト秒（既定: env SHERPA_OFFICE_COM_TIMEOUT ＞ 120）。

.PARAMETER MaxFileBytes
  upload 系エンドポイント（/convert-upload・/render-upload）が受け付けるファイル本体の上限バイト数
  （既定: env SHERPA_OFFICE_COM_MAX_FILE_BYTES ＞ 524288000＝500MiB）。超過は 413。path 方式
  （/convert・/render）には適用しない（従来どおりローカル/UNC パスを直接開くだけで本体転送が無い）。

.PARAMETER TempDir
  upload 系エンドポイントが受信したファイル本体を一時的に書き出すディレクトリ（既定: env
  SHERPA_OFFICE_COM_TEMP_DIR ＞ %TEMP%）。処理後（成功/失敗どちらでも）必ず削除する。

.EXAMPLE
  # PowerShell（Windows 側）で:
  $env:SHERPA_OFFICE_COM_TOKEN = "任意の長い秘密の文字列"
  powershell -NoProfile -ExecutionPolicy Bypass -STA -File .\deploy\office-com-worker.ps1

  # WSL 側の疎通確認:
  curl -s -H "X-Sherpa-Token: $SHERPA_OFFICE_COM_TOKEN" http://127.0.0.1:8091/healthz
#>
[CmdletBinding()]
param(
    [int]$Port = 0,
    [string]$BindAddress = "127.0.0.1",
    [string]$Token = "",
    [int]$TimeoutSec = 0,
    # OFFICE-WIN-001: upload 系エンドポイントのファイルサイズ上限／一時ファイル置き場（§6.5「設定を簡単にする」
    # office-worker.json の max_file_bytes/temp_dir に対応）。path 方式には影響しない。
    [long]$MaxFileBytes = 0,
    [string]$TempDir = "",
    # 内部用: 自分自身を子プロセスとして起動し1件だけ変換する隔離モード（listener は起動しない）。
    # 入出力パス/ターゲットは環境変数（SHERPA_OC_INPUT/TARGET/OUT/ERR）で受け渡す（引用符の罠を回避）。
    [switch]$ConvertOnce,
    # 内部用: -ConvertOnce と同じ隔離モードだが SaveAs でなく PDF レンダ（ExportAsFixedFormat/SaveAs PDF）を行う
    # （Officeのas-displayed忠実レンダ・W2'）。入出力は -ConvertOnce と同じ env 経由。
    [switch]$RenderPdf,
    # 内部用: -ConvertOnce と同じ隔離モードだが PowerPoint 補助構造（スライド/図形の JSON）を抽出する
    # （OFFICE-WIN-001 ⑤・試作）。入出力は -ConvertOnce と同じ env 経由（SHERPA_OC_OUT には JSON テキストを書く）。
    [switch]$ExtractStructureOnce,
    # 内部用: Excel Range.Text / DisplayFormat.NumberFormat を対象セルだけ抽出してJSONへ書く。
    [switch]$ExtractExcelDisplayOnce,
    # W2'（office_com 直接呼び出しモード・feedback-batch-2026-07-08 ⑥）: WSL の interop
    # （/mnt/c/.../powershell.exe）から one-shot でこのスクリプトを叩き、COM 検出（word/excel/powerpoint の
    # 可否とバージョン）を JSON で **stdout** に出す（常駐ワーカー・URL・トークン不要）。COM は起動しない
    # （レジストリの ProgID CurVer を読む軽量判定）。
    [switch]$Healthz,
    # W2'（office_com 直接呼び出しモード）: WSL の interop から one-shot で変換/レンダ1件を実行する外側ジョブ。
    # 内部で -ConvertOnce/-RenderPdf 子プロセスを起こし、タイムアウト時に「この変換が作った Office」だけを
    # kill する Windows 側の監視（Stop-CandidateProcesses）を効かせる（HTTP ワーカーの listener と同じ堅牢性）。
    # 入出力は env でなく引数で受け取る（WSL→Windows interop の env 透過に依存しない・-InPath/-OutPath/-ErrPath/
    # -Job(convert|render)/-Target/-JobTimeoutSec）。結果バイトは -OutPath（WSL が \\wsl.localhost の UNC で渡す
    # 一時ファイル）へ書き、失敗理由は -ErrPath へ書く。
    [switch]$DirectJob,
    [string]$InPath = "",
    [string]$OutPath = "",
    [string]$ErrPath = "",
    [string]$Job = "convert",
    [string]$Target = "",
    [int]$JobTimeoutSec = 0,
    # DirectJob(display) がセル一覧JSONを渡すためのWindows絶対path。HTTP uploadでは内部temp pathを使う。
    [string]$OptionsPath = ""
)

$ErrorActionPreference = "Stop"

# 旧形式拡張子 → 新形式ターゲット/アプリ の対応（これ以外は /convert で 400）。
$script:ExtMap = @{
    ".doc" = @{ Target = "docx"; App = "word" }
    ".xls" = @{ Target = "xlsx"; App = "excel" }
    ".ppt" = @{ Target = "pptx"; App = "powerpoint" }
}

# PDF レンダ（-RenderPdf・/render）が受け付ける拡張子 → アプリ の対応。変換（旧→新）と違い、
# **新形式（.docx/.xlsx/.pptx）も受ける**（as-displayed の忠実レンダは modern 形式でも意味がある＝
# Officeのas-displayed忠実レンダ）。旧形式（.doc/.xls/.ppt）も同じアプリで開ける。
$script:RenderExtMap = @{
    ".doc"  = @{ App = "word" };       ".docx" = @{ App = "word" }
    ".xls"  = @{ App = "excel" };      ".xlsx" = @{ App = "excel" }
    ".ppt"  = @{ App = "powerpoint" }; ".pptx" = @{ App = "powerpoint" }
}

# 補助構造抽出（-ExtractStructureOnce・/extract-structure-upload）が受け付ける拡張子（OFFICE-WIN-001 ⑤・
# PowerPoint 限定の試作。旧/新どちらの形式も COM で開けるため両方許可する＝RenderExtMap の powerpoint 分と同じ集合）。
$script:ExtractStructureExtMap = @{
    ".ppt" = @{ App = "powerpoint" }; ".pptx" = @{ App = "powerpoint" }
}

# Excel表示値補完（-ExtractExcelDisplayOnce・/extract-excel-display-upload）。旧/新の両方を
# Microsoft Excelでread-onlyに開けるため、XLS/XLSXだけを明示allowlistする。
$script:ExcelDisplayExtMap = @{
    ".xls" = @{ App = "excel" }; ".xlsx" = @{ App = "excel" }
}

# ---- COM 変換（隔離子プロセス側で実行される）----

function Get-ProcessSnapshot($imageName) {
    # RV High（2026-07-08）: タイムアウト時に kill してよい Office プロセスは「この変換が**作った**インスタンス」
    # だけ（ユーザーが手動で開いている文書を巻き込まない）。判定を「タイムアウト時点の広い前後差分」から
    # 「New-Object 直前後のミリ秒級の差分」へ狭めるため、CIM（WMI Win32_Process）で PID＋CreationDate を取る
    # （CreationDate は PID 再利用ガードにも使う）。CIM 取得に失敗したら空配列（fail-safe＝候補ゼロ＝kill しない）。
    try {
        return @(Get-CimInstance -ClassName Win32_Process -Filter ("Name='{0}.exe'" -f $imageName) -ErrorAction Stop |
            Select-Object -Property @{n='Id';e={$_.ProcessId}}, CreationDate)
    } catch {
        return @()
    }
}

function ConvertTo-CandidateJson($candidates) {
    # 実 Windows PowerShell 5.1 の検証で判明: `ConvertTo-Json -NoEnumerate` は PowerShell 6+（Core）専用の
    # パラメーターで、**Windows PowerShell 5.1 には存在しない**（本スクリプトの対象そのもの）。
    # 気づかず使うと Write-CandidatePidFile が毎回例外→ catch で握りつぶされ pidfile が一切書かれず、
    # タイムアウト時に「この変換が作った」Office プロセスを一切 kill できなくなる（RV High 修正が無効化される）
    # サイレント regression になる。ConvertTo-Json のバージョン依存を避けるため、候補の JSON を手組みする
    # （フィールドは pid:int・name/creation_date:string のみの単純な形なので手組みで十分）。
    if (-not $candidates -or $candidates.Count -eq 0) { return "[]" }
    $items = foreach ($c in $candidates) {
        $name = ([string]$c.name) -replace '\\', '\\\\' -replace '"', '\"'
        $date = ([string]$c.creation_date) -replace '\\', '\\\\' -replace '"', '\"'
        ('{{"pid":{0},"name":"{1}","creation_date":"{2}"}}' -f [int]$c.pid, $name, $date)
    }
    return "[" + ($items -join ",") + "]"
}

function Write-CandidatePidFile($pidFile, $imageName, $before, $after) {
    # New-Object 直前後の差分＝この変換が新規に起こしたインスタンス候補。差分が空＝既存インスタンスへ
    # アタッチ（例: PowerPoint の単一インスタンス性）＝**候補ゼロで書く**（タイムアウトしても絶対に kill しない）。
    if (-not $pidFile) { return }
    $beforeIds = @($before | ForEach-Object { $_.Id })
    $candidates = @($after | Where-Object { $beforeIds -notcontains $_.Id } | ForEach-Object {
        @{ pid = $_.Id; name = $imageName; creation_date = $_.CreationDate.ToString("o") }
    })
    try {
        Set-Content -LiteralPath $pidFile -Value (ConvertTo-CandidateJson $candidates) -Encoding UTF8
    } catch {}
}

function Convert-Word($inPath, $outPath, $pidFile, $target) {
    # $target: "docx"（旧→新 SaveAs）｜"pdf"（ExportAsFixedFormat＝as-displayed 忠実レンダ・W2'）。
    $before = Get-ProcessSnapshot "WINWORD"
    $app = New-Object -ComObject Word.Application
    Write-CandidatePidFile $pidFile "WINWORD" $before (Get-ProcessSnapshot "WINWORD")
    try {
        $app.Visible = $false
        try { $app.DisplayAlerts = 0 } catch {}          # wdAlertsNone
        try { $app.AutomationSecurity = 3 } catch {}      # msoAutomationSecurityForceDisable（マクロ強制無効・Open 前に設定）
        # Documents.Open(FileName, ConfirmConversions, ReadOnly, AddToRecentFiles)
        $doc = $app.Documents.Open($inPath, $false, $true, $false)
        try {
            if ($target -eq "pdf") {
                $doc.ExportAsFixedFormat($outPath, 17)    # wdExportFormatPDF（見た目どおりのレンダ）
            } else {
                # 実機検証（2026-07-08・実 Office 16.0）: `SaveAs([ref]$out,[ref]$fmt)` は遅延バインドで
                # "psobject を Object に変換できません" と即エラーになる（[ref] 包みが COM の VARIANT 引数に
                # 変換できない）。[ref] を使わない SaveAs2（Excel/PowerPoint の SaveAs と同じ無 ref 形）に統一する。
                $doc.SaveAs2($outPath, 16)                # wdFormatDocumentDefault（.docx）
            }
        } finally {
            $doc.Close($false)                            # wdDoNotSaveChanges
        }
    } finally {
        $app.Quit()
        [System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) | Out-Null
    }
}

function Convert-Excel($inPath, $outPath, $pidFile, $target) {
    # $target: "xlsx"（旧→新 SaveAs）｜"pdf"（ExportAsFixedFormat xlTypePDF）。
    $before = Get-ProcessSnapshot "EXCEL"
    $app = New-Object -ComObject Excel.Application
    Write-CandidatePidFile $pidFile "EXCEL" $before (Get-ProcessSnapshot "EXCEL")
    $guardWb = $null
    try {
        $app.Visible = $false
        try { $app.DisplayAlerts = $false } catch {}
        try { $app.AutomationSecurity = 3 } catch {}      # msoAutomationSecurityForceDisable
        # Workbooks.Open(FileName, UpdateLinks, ReadOnly)
        $wb = $app.Workbooks.Open($inPath, 0, $true)
        try {
            if ($target -eq "pdf") {
                $wb.ExportAsFixedFormat(0, $outPath)      # xlTypePDF=0（Type, Filename）
            } else {
                $fmt = 51                                 # xlOpenXMLWorkbook（.xlsx）
                $wb.SaveAs($outPath, $fmt)
            }
        } finally {
            $wb.Close($false)
        }
    } finally {
        $app.Quit()
        [System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) | Out-Null
    }
}

function Convert-PowerPoint($inPath, $outPath, $pidFile, $target) {
    # $target: "pptx"（旧→新 SaveAs）｜"pdf"（SaveAs ppSaveAsPDF）。
    # ⚠ PowerPoint は単一インスタンス性を持ち、ユーザーが既に PowerPoint を開いている場合 COM が
    # **既存インスタンスへアタッチ**することがある（新規プロセスを作らない）。その場合 Get-ProcessSnapshot の
    # 差分は空になり Write-CandidatePidFile が空配列を書く＝タイムアウトしてもこの既存インスタンスは
    # 絶対に kill しない（ユーザーの未保存プレゼンを守る・ハングは子プロセス kill のみで妥協＝ワーカー再起動が
    # 必要になることがある。40-運用.md に明記）。
    $before = Get-ProcessSnapshot "POWERPNT"
    $app = New-Object -ComObject PowerPoint.Application
    Write-CandidatePidFile $pidFile "POWERPNT" $before (Get-ProcessSnapshot "POWERPNT")
    try {
        try { $app.DisplayAlerts = 1 } catch {}           # ppAlertsNone
        try { $app.AutomationSecurity = 3 } catch {}      # msoAutomationSecurityForceDisable
        # Presentations.Open(FileName, ReadOnly, Untitled, WithWindow)
        #   ReadOnly=msoTrue(-1) / WithWindow=msoFalse(0)（ヘッドレス）
        $pres = $app.Presentations.Open($inPath, -1, 0, 0)
        try {
            if ($target -eq "pdf") {
                $pres.SaveAs($outPath, 32)                # ppSaveAsPDF=32
            } else {
                $fmt = 24                                 # ppSaveAsOpenXMLPresentation（.pptx）
                $pres.SaveAs($outPath, $fmt)
            }
        } finally {
            $pres.Close()
        }
    } finally {
        $app.Quit()
        [System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) | Out-Null
    }
}

# ---- Excel表示値抽出（E15・隔離子プロセス側で実行される）----

$script:ExcelDisplayMaxCells = 50000
$script:ExcelDisplayMaxJsonBytes = 67108864   # 64MiB。超過時は補完全体を失敗させLinux基本経路へ倒す。

function Test-ExcelCellReference([string]$cell) {
    if (-not $cell -or $cell -cnotmatch '^([A-Z]{1,3})([1-9][0-9]{0,6})$') { return $false }
    $letters = $matches[1]
    $row = [int]$matches[2]
    $column = 0
    foreach ($ch in $letters.ToCharArray()) {
        $column = ($column * 26) + ([int][char]$ch - [int][char]'A' + 1)
    }
    return ($column -le 16384 -and $row -le 1048576)
}

function Get-FileSha256Hex([string]$path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($path)
    try {
        $hash = $sha.ComputeHash($stream)
        return -join ($hash | ForEach-Object { $_.ToString("x2") })
    } finally {
        $stream.Dispose()
        $sha.Dispose()
    }
}

function Disable-WorkbookExternalConnections($wb) {
    # UpdateLinks=0でopenした上で、既存connection/query tableの以後のrefreshも止める。原本はread-onlyで
    # 開き、保存しないため、これらの変更はメモリ上だけで破棄される。
    foreach ($connection in @($wb.Connections)) {
        try { $connection.RefreshWithRefreshAll = $false } catch {}
        try { $connection.OLEDBConnection.EnableRefresh = $false } catch {}
        try { $connection.OLEDBConnection.BackgroundQuery = $false } catch {}
        try { $connection.ODBCConnection.EnableRefresh = $false } catch {}
        try { $connection.ODBCConnection.BackgroundQuery = $false } catch {}
    }
    foreach ($sheet in @($wb.Worksheets)) {
        try {
            foreach ($query in @($sheet.QueryTables)) {
                try { $query.EnableRefresh = $false } catch {}
                try { $query.RefreshOnFileOpen = $false } catch {}
                try { $query.BackgroundQuery = $false } catch {}
            }
            foreach ($table in @($sheet.ListObjects)) {
                try { $table.QueryTable.EnableRefresh = $false } catch {}
                try { $table.QueryTable.RefreshOnFileOpen = $false } catch {}
                try { $table.QueryTable.BackgroundQuery = $false } catch {}
            }
        } finally {
            try { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($sheet) | Out-Null } catch {}
        }
    }
}

function Get-ExcelDisplayValues($inPath, $optionsPath, $pidFile) {
    if (-not $optionsPath -or -not (Test-Path -LiteralPath $optionsPath -PathType Leaf)) {
        throw "excel display options file is required"
    }
    $optionsRaw = [System.IO.File]::ReadAllText($optionsPath, [System.Text.Encoding]::UTF8)
    try { $options = $optionsRaw | ConvertFrom-Json } catch { throw "invalid excel display options json" }
    if ($null -eq $options -or $options.schema -ne "sherpa-excel-display-v1") {
        throw "invalid excel display options schema"
    }
    $targets = @($options.cells)
    if ($targets.Count -le 0 -or $targets.Count -gt $script:ExcelDisplayMaxCells) {
        throw ("excel display cell count must be 1..{0}" -f $script:ExcelDisplayMaxCells)
    }
    $seen = @{}
    foreach ($target in $targets) {
        $sheetName = [string]$target.sheet
        $cell = ([string]$target.cell).ToUpperInvariant()
        if (-not $sheetName -or $sheetName.Length -gt 31 -or -not (Test-ExcelCellReference $cell)) {
            throw "invalid excel display cell target"
        }
        $key = $sheetName + "`n" + $cell
        if ($seen.ContainsKey($key)) { throw "duplicate excel display cell target" }
        $seen[$key] = $true
    }

    $before = Get-ProcessSnapshot "EXCEL"
    $app = New-Object -ComObject Excel.Application
    Write-CandidatePidFile $pidFile "EXCEL" $before (Get-ProcessSnapshot "EXCEL")
    try {
        # いずれかの安全設定を適用できなければ補完を失敗させる。安全profileを偽って成功させない。
        $app.Visible = $false
        $app.DisplayAlerts = $false
        $app.AutomationSecurity = 3                 # msoAutomationSecurityForceDisable
        $app.AskToUpdateLinks = $false
        $app.EnableEvents = $false
        # Excelはworkbookが0件だとCalculation設定を0x800A03ECで拒否する版がある。空のguard workbookを
        # 先に作ってmanualへ固定し、その後で初めて対象原本を開く（対象を自動計算状態で開く窓を作らない）。
        $guardWb = $app.Workbooks.Add()
        $app.Calculation = -4135                    # xlCalculationManual
        $app.CalculateBeforeSave = $false
        # Workbooks.Open(FileName, UpdateLinks=0, ReadOnly=true)。再計算/refresh/saveは一度も呼ばない。
        $wb = $app.Workbooks.Open($inPath, 0, $true)
        try {
            Disable-WorkbookExternalConnections $wb
            $cells = @()
            $missing = @()
            foreach ($target in $targets) {
                $sheetName = [string]$target.sheet
                $cell = ([string]$target.cell).ToUpperInvariant()
                $sheet = $null; $range = $null; $displayFormat = $null
                $baseFont = $null; $baseInterior = $null; $displayFont = $null; $displayInterior = $null
                try {
                    $sheet = $wb.Worksheets.Item($sheetName)
                    $range = $sheet.Range($cell)
                    $baseFormat = [string]$range.NumberFormat
                    $baseFont = $range.Font
                    $baseInterior = $range.Interior
                    $baseFontColor = [long]$baseFont.Color
                    $baseFillColor = [long]$baseInterior.Color
                    $effectiveFormat = $baseFormat
                    $effectiveLocal = [string]$range.NumberFormatLocal
                    $effectiveFontColor = $baseFontColor
                    $effectiveFillColor = $baseFillColor
                    $formatSource = "Range"
                    try {
                        $displayFormat = $range.DisplayFormat
                        $effectiveFormat = [string]$displayFormat.NumberFormat
                        $effectiveLocal = [string]$displayFormat.NumberFormatLocal
                        $displayFont = $displayFormat.Font
                        $displayInterior = $displayFormat.Interior
                        $effectiveFontColor = [long]$displayFont.Color
                        $effectiveFillColor = [long]$displayInterior.Color
                        $formatSource = "DisplayFormat"
                    } catch {
                        # DisplayFormat非対応時もRange自身の書式は実値なので、近似せずsourceを明示して返す。
                    }
                    $cells += ,@{
                        sheet               = $sheetName
                        cell                = $cell
                        text                = [string]$range.Text
                        number_format       = $effectiveFormat
                        base_number_format  = $baseFormat
                        number_format_local = $effectiveLocal
                        number_format_source = $formatSource
                        base_font_color     = $baseFontColor
                        base_fill_color     = $baseFillColor
                        display_font_color  = $effectiveFontColor
                        display_fill_color  = $effectiveFillColor
                    }
                } catch {
                    $missing += ,@{ sheet = $sheetName; cell = $cell; reason = "cell_read_failed" }
                } finally {
                    if ($null -ne $displayInterior) { try { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($displayInterior) | Out-Null } catch {} }
                    if ($null -ne $displayFont) { try { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($displayFont) | Out-Null } catch {} }
                    if ($null -ne $baseInterior) { try { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($baseInterior) | Out-Null } catch {} }
                    if ($null -ne $baseFont) { try { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($baseFont) | Out-Null } catch {} }
                    if ($null -ne $displayFormat) { try { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($displayFormat) | Out-Null } catch {} }
                    if ($null -ne $range) { try { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($range) | Out-Null } catch {} }
                    if ($null -ne $sheet) { try { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($sheet) | Out-Null } catch {} }
                }
            }
            return @{
                schema          = "sherpa-excel-display-v1"
                source_hash     = (Get-FileSha256Hex $inPath)
                worker_version  = "1.3"
                office_app      = "excel"
                office_version  = [string]$app.Version
                worker_profile  = @{
                    read_only                    = $true
                    macros_disabled              = $true
                    update_links                 = 0
                    # Workbooks.Open(UpdateLinks=0)に加え、open後のconnection/query refreshを無効化した。
                    # open瞬間のnetwork 0はCOMだけでは証明できないため、worker hostのoutbound firewallを別Gateにする。
                    post_open_external_refresh_disabled = $true
                    network_isolation                    = "deployment_required"
                    calculation                  = "manual_no_recalculate"
                }
                cells            = $cells
                missing          = $missing
            }
        } finally {
            $wb.Close($false)
        }
    } finally {
        if ($null -ne $guardWb) { try { $guardWb.Close($false) } catch {} }
        $app.Quit()
        [System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) | Out-Null
    }
}

function Invoke-ExtractExcelDisplayOnce {
    $inPath = $env:SHERPA_OC_INPUT
    $outFile = $env:SHERPA_OC_OUT
    $errFile = $env:SHERPA_OC_ERR
    $pidFile = $env:SHERPA_OC_PIDFILE
    $optionsPath = $env:SHERPA_OC_OPTIONS
    try {
        $ext = [System.IO.Path]::GetExtension($inPath).ToLowerInvariant()
        if (-not $script:ExcelDisplayExtMap[$ext]) { throw "unsupported extension for excel display: $ext" }
        $result = Get-ExcelDisplayValues $inPath $optionsPath $pidFile
        $jsonBytes = [System.Text.Encoding]::UTF8.GetBytes(($result | ConvertTo-Json -Compress -Depth 8))
        if ($jsonBytes.Length -gt $script:ExcelDisplayMaxJsonBytes) {
            throw "excel display json exceeds limit"
        }
        [System.IO.File]::WriteAllBytes($outFile, $jsonBytes)
        exit 0
    } catch {
        try { Set-Content -LiteralPath $errFile -Value $_.Exception.Message -Encoding UTF8 } catch {}
        exit 1
    }
}

# ---- PowerPoint 補助構造抽出（OFFICE-WIN-001 ⑤・試作・隔離子プロセス側で実行される）----

function Get-ShapeTypeName([int]$type) {
    # MsoShapeType の主要値だけ平文名へ（未知値は数値のまま・意味判断はしない＝Sherpa 側の決定ルールへ委ねる）。
    switch ($type) {
        1  { return "AutoShape" }
        3  { return "Chart" }
        6  { return "Group" }
        7  { return "Comment" }
        9  { return "EmbeddedOLEObject" }
        12 { return "OLEControlObject" }
        13 { return "Picture" }
        14 { return "Placeholder" }
        17 { return "TextBox" }
        19 { return "Table" }
        24 { return "Diagram" }
        default { return ("Shape{0}" -f $type) }
    }
}

# Med是正（レビュー指摘・2026-07-22・追いRV）: 展開後の大量スライド/ノート/図形テキストによるワーカー側
# メモリ枯渇・巨大応答を防ぐ多段の上限。(a) フィールド単位の文字数上限を**蓄積ループの内側**で適用する
# （Read-ShapeTextClamped/Add-BudgetedText・段落単位の増分読み取り＋予算到達で即中断＝一括 `.Text` 取得＋
# 事後切り詰めをやめる）。(b) スライド数・スライドあたり図形数のハード上限（超過分は COM から読みにも
# 行かない＝ループを打ち切る）。(c) スライド単位の蓄積後に総文字数を粗い予算（JSON バイト上限を保守的に
# 文字数換算したもの）と照合し、超過を検出したら**直列化を待たずに** STRUCTURE_TOO_LARGE を投げる。
# (d)（旧 Med-2・変更なし）直列化後の総バイト数検査は最終防衛として残す（Invoke-ExtractStructureOnce 側）。
#
# 既定値の根拠: 32,768 文字/フィールド＝実務的な1スライド分のテキスト量を十分に超える上限。500 スライド・
# 1000 図形/スライド＝実務のプレゼンで通常到達しない規模（数百枚のデッキでも数百止まりが大半）でありながら、
# 極端な水増しファイルを打ち切れる水準。32MiB/応答＝HTTP クライアント・JSON パーサの一般的な既定バッファ
# 規模を大きく超えないための後段の安全弁。(c) の粗予算は 32MiB を **4（UTF-8 の1文字最大バイト数）で割った
# 文字数**とする（保守側＝実際にはここまで悪くない文字種がほとんどだが、安全側に倒して早期に打ち切る）。
#
# CLI パラメータ化しない理由: これらの関数は `-ExtractStructureOnce` の隔離子プロセス（Invoke-OfficeJob が
# spawn する子プロセス）側で実行される。子プロセスは listener が解決した CLI パラメータを引数として
# 受け取らない（`Invoke-OfficeJob` は `SHERPA_OC_INPUT`/`TARGET`/`OUT`/`ERR`/`PIDFILE` の5つの env だけを
# 明示的に渡す設計）。子プロセスへ確実に伝わるのは**OS 環境変数の継承**のみ（`Invoke-DirectJob` が
# `$JobTimeoutSec` の既定値を `$env:SHERPA_OFFICE_COM_TIMEOUT` から直接解決しているのと同じ理由・同じ
# パターン）。運用側で上限を変えたい場合は listener 起動前に env を設定する（`SHERPA_OFFICE_COM_TOKEN` と
# 同じ設定方法）。
$script:DefaultMaxStructureFieldChars = 32768
$script:DefaultMaxStructureJsonBytes = 33554432        # 32MiB
$script:DefaultMaxStructureSlides = 500
$script:DefaultMaxStructureShapesPerSlide = 1000

# 追いMed是正 (2): テキストが空の図形・スライドでも、JSON 化すれば name/type/z_order/visible/*_truncated
# 等のキーと配列保持自体がメモリ・応答サイズを消費する。$totalChars（粗予算）が実テキスト長だけを見ていると、
# 「500 スライド × 1,000 図形（すべてテキスト無し）」＝50万 hashtable が実テキスト予算を一切消費しないまま
# 完全直列化まで進んでしまう。図形/スライド1件ごとに構造オーバーヘッドの保守見積り文字数を予算へ計上し、
# 図形・スライド数そのものが予算超過の引き金になるようにする。
#   - 256 文字/図形: name（数十文字級）+ type 文字列 + z_order/visible/text_truncated 等の固定キー＋
#     JSON の中括弧・カンマ・クォート・配列要素のオーバーヘッドを保守的に見積もった値。
#   - 512 文字/スライド: スライド自体の固定キー（slide_number/title_truncated/body_truncated/
#     notes_truncated/hidden/shapes_truncated 等）＋配列（$slides への追加）のオーバーヘッド。
$script:StructureOverheadCharsPerShape = 256
$script:StructureOverheadCharsPerSlide = 512

function Resolve-StructureLimitEnv([string]$envName, [long]$defaultValue) {
    # Med是正: env 値は「正の整数として parse 可能」のみ受理する。`-1`（Substring(0,-1) 例外→汎用500化）・
    # `0`（全フィールド空文字・JSON 上限0で常時413）・`abc`（parse 不能）はすべて既定値へフォールバックし、
    # 警告ログ（Write-Warning）を残す（無効値を握って未定義動作にしない・fail-safe）。
    $raw = [Environment]::GetEnvironmentVariable($envName)
    if ([string]::IsNullOrWhiteSpace($raw)) { return $defaultValue }
    $parsed = 0L
    if (-not [long]::TryParse($raw.Trim(), [ref]$parsed) -or $parsed -le 0) {
        Write-Warning ("{0} の値が不正です（'{1}'）。既定値 {2} を使用します（正の整数のみ有効）。" -f $envName, $raw, $defaultValue)
        return $defaultValue
    }
    return $parsed
}

function Get-MaxStructureFieldChars {
    return [int][Math]::Min((Resolve-StructureLimitEnv "SHERPA_OFFICE_COM_MAX_STRUCTURE_FIELD_CHARS" $script:DefaultMaxStructureFieldChars), [int]::MaxValue)
}

function Get-MaxStructureJsonBytes {
    return (Resolve-StructureLimitEnv "SHERPA_OFFICE_COM_MAX_STRUCTURE_JSON_BYTES" $script:DefaultMaxStructureJsonBytes)
}

function Get-MaxStructureSlides {
    return [int][Math]::Min((Resolve-StructureLimitEnv "SHERPA_OFFICE_COM_MAX_STRUCTURE_SLIDES" $script:DefaultMaxStructureSlides), [int]::MaxValue)
}

function Get-MaxStructureShapesPerSlide {
    return [int][Math]::Min((Resolve-StructureLimitEnv "SHERPA_OFFICE_COM_MAX_STRUCTURE_SHAPES_PER_SLIDE" $script:DefaultMaxStructureShapesPerSlide), [int]::MaxValue)
}


# 追いMed是正 (1): 1回の Characters() 呼び出しで読む最大文字数。段落単体が病的に巨大（例: 数百 MiB の
# 単一段落）でも、一度の COM 読み取りでこのチャンク分しかメモリへ展開しない。$script:DefaultMaxStructure
# FieldChars（既定 32,768）より十分小さく、かつ COM 呼び出し回数が過大にならない実務的なバランス値
# （8,192 文字＝32,768 の 1/4・普通のテキストなら 1〜数回の呼び出しで1段落を読み切れる）。
$script:StructureReadChunkChars = 8192

function Read-ParagraphChunked($para, [int]$maxRead) {
    # 追いMed是正 (1): 段落テキストを `Characters(Start, Length)`（1-based）でチャンク単位に読み、
    # 最大 $maxRead 文字まで蓄積する。`.Text` の一括取得（1段落全体を一度にメモリへ展開する）は使わない。
    # `.Length`／`.Characters()` が使えない場合も「ここまで読めた分」だけを返し、一括 `.Text` へは
    # 絶対にフォールバックしない（Read-ShapeTextClamped と同じ安全側の一貫方針）。
    $sb = New-Object System.Text.StringBuilder
    $paraLen = 0
    try { $paraLen = [int]$para.Length } catch { return @{ Text = ""; Truncated = $true } }
    $pos = 1
    $truncated = $false
    while ($pos -le $paraLen) {
        if ($sb.Length -ge $maxRead) { $truncated = $true; break }
        $chunkLen = [Math]::Min($script:StructureReadChunkChars, $paraLen - $pos + 1)
        $chunkLen = [Math]::Min($chunkLen, $maxRead - $sb.Length)
        if ($chunkLen -le 0) { $truncated = $true; break }
        try {
            $chunkText = $para.Characters($pos, $chunkLen).Text
        } catch {
            $truncated = $true
            break
        }
        [void]$sb.Append($chunkText)
        $pos += $chunkLen
    }
    if ($pos -le $paraLen) { $truncated = $true }
    return @{ Text = $sb.ToString(); Truncated = $truncated }
}

function Read-ShapeTextClamped($shape, [int]$maxChars) {
    # Med是正 (a): 図形のテキストを**段落単位で増分的に**読み取り、上限に達した時点でそれ以上 COM から
    # 読み取らない（`.TextFrame.TextRange.Text` を一括取得してから切り詰める旧実装は、1つの図形が病的に
    # 巨大なテキストを持つ場合にその全体を一度メモリへ展開してしまう）。`Paragraphs()` が使えない図形
    # （一部の OLE/表セル等）は例外を投げるが、その場合は「ここまで読めた分」を返すに留め、決して一括
    # 取得へフォールバックしない（安全側＝読み取れる範囲が狭まる代わりに上限は必ず守られる）。
    #
    # 追いMed是正 (1): 段落**単体**が病的に巨大な場合（1段落で数百 MiB 等）に備え、各段落の読み取り自体も
    # `Read-ParagraphChunked`（`Characters(start,len)` によるチャンク読み）を使う。`$para.Text` の一括取得
    # はもう行わない＝1回の COM 読み取りで確保するメモリは常に $script:StructureReadChunkChars 文字が上限。
    try {
        if (-not $shape.HasTextFrame) { return @{ Text = ""; Truncated = $false } }
    } catch { return @{ Text = ""; Truncated = $false } }
    $tf = $shape.TextFrame
    try {
        if (-not $tf.HasText) { return @{ Text = ""; Truncated = $false } }
    } catch { return @{ Text = ""; Truncated = $false } }
    $sb = New-Object System.Text.StringBuilder
    $truncated = $false
    try {
        foreach ($para in $tf.TextRange.Paragraphs()) {
            if ($sb.Length -ge $maxChars) { $truncated = $true; break }
            if ($sb.Length -gt 0) {
                if ($sb.Length -ge $maxChars) { $truncated = $true; break }
                [void]$sb.Append("`r")
            }
            $remaining = $maxChars - $sb.Length
            if ($remaining -le 0) { $truncated = $true; break }
            $chunked = Read-ParagraphChunked $para $remaining
            [void]$sb.Append($chunked.Text)
            if ($chunked.Truncated) { $truncated = $true; break }
        }
    } catch {
        # Paragraphs() 自体が例外を投げる図形（安全側＝ここまで読めた分だけを返す・一括取得はしない）。
        return @{ Text = $sb.ToString(); Truncated = $true }
    }
    return @{ Text = $sb.ToString(); Truncated = $truncated }
}

function Add-BudgetedText([System.Text.StringBuilder]$sb, [string]$piece, [int]$maxChars, [string]$separator) {
    # Med是正 (a): body_text（スライド内複数図形の集約）の蓄積を予算付きにする。既に予算に達していれば
    # 何も追加しない（それ以上の図形テキストを StringBuilder へ足さない＝メモリを追加消費しない）。
    # 戻り値 $true＝この呼び出しで予算に達した/超えた（呼び出し側が truncated フラグへ反映する）。
    if ([string]::IsNullOrEmpty($piece)) { return ($sb.Length -ge $maxChars) }
    if ($sb.Length -ge $maxChars) { return $true }
    if ($sb.Length -gt 0 -and $separator) {
        if (($sb.Length + $separator.Length) -ge $maxChars) { return $true }
        [void]$sb.Append($separator)
    }
    $remaining = $maxChars - $sb.Length
    if ($remaining -le 0) { return $true }
    if ($piece.Length -gt $remaining) {
        [void]$sb.Append($piece.Substring(0, $remaining))
        return $true
    }
    [void]$sb.Append($piece)
    return $false
}

function Get-SlideNotesTextClamped($slide, [int]$maxChars) {
    # 発表者ノート本文（ppPlaceholderBody=2 のプレースホルダ）を段落単位で増分読み取り。ノートページ自体が
    # 無い/取得失敗は空文字（Truncated=$false）。
    try {
        foreach ($nshape in $slide.NotesPage.Shapes.Placeholders) {
            try {
                if ([int]$nshape.PlaceholderFormat.Type -eq 2) {
                    return Read-ShapeTextClamped $nshape $maxChars
                }
            } catch { continue }
        }
    } catch {}
    return @{ Text = ""; Truncated = $false }
}

function Get-PowerPointStructure($inPath, $pidFile) {
    # PowerPoint COM でスライドを開き、スライド単位の補助構造（タイトル・本文テキスト・発表者ノート・
    # 非表示フラグ・図形の名前/種類/z-order/テキスト/可視性）を抽出する。COM から安定して取れる範囲のみ
    # （隠し図形による上書き判定等の**意味づけ**は Sherpa 側の決定ルールが行う・OFFICE-WIN-001「構造抽出の責務」）。
    # Convert-PowerPoint と同じ「この呼び出しが起こしたインスタンスだけを記録して kill 対象にする」パターン
    # （PowerPoint の単一インスタンス性＝既存インスタンスへアタッチした場合は候補ゼロで絶対に kill しない）。
    $before = Get-ProcessSnapshot "POWERPNT"
    $app = New-Object -ComObject PowerPoint.Application
    Write-CandidatePidFile $pidFile "POWERPNT" $before (Get-ProcessSnapshot "POWERPNT")
    try {
        try { $app.DisplayAlerts = 1 } catch {}           # ppAlertsNone
        try { $app.AutomationSecurity = 3 } catch {}      # msoAutomationSecurityForceDisable
        # Presentations.Open(FileName, ReadOnly, Untitled, WithWindow)
        $pres = $app.Presentations.Open($inPath, -1, 0, 0)
        try {
            $maxChars = Get-MaxStructureFieldChars
            $maxSlides = Get-MaxStructureSlides
            $maxShapesPerSlide = Get-MaxStructureShapesPerSlide
            # Med是正 (c): 直列化前の粗い総量予算（32MiB 相当を UTF-8 最悪バイト数 4 で割った文字数・保守側）。
            # フィールド単位の上限だけではスライド数そのものが多い場合の総量を抑えられないため、蓄積の
            # 都度ここと照合し、完全な ConvertTo-Json を待たずに打ち切る（直列化後の実バイト検査は
            # Invoke-ExtractStructureOnce に最終防衛として残す）。
            $charBudget = [Math]::Max(1L, [long](Get-MaxStructureJsonBytes) / 4L)
            $totalChars = 0L
            $slides = @()
            $slidesTruncated = $false
            $slideIdx = 0
            foreach ($slide in $pres.Slides) {
                $slideIdx++
                if ($slideIdx -gt $maxSlides) { $slidesTruncated = $true; break }
                $titleShapeName = $null
                $clampedTitle = @{ Text = ""; Truncated = $false }
                try {
                    if ($slide.Shapes.HasTitle) {
                        $titleShapeName = [string]$slide.Shapes.Title.Name
                        $clampedTitle = Read-ShapeTextClamped $slide.Shapes.Title $maxChars
                    }
                } catch {}
                $shapes = @()
                $bodySb = New-Object System.Text.StringBuilder
                $bodyTruncated = $false
                $shapesTruncated = $false
                $shapeIdx = 0
                foreach ($shape in $slide.Shapes) {
                    $shapeIdx++
                    if ($shapeIdx -gt $maxShapesPerSlide) { $shapesTruncated = $true; break }
                    $visible = $true
                    try { $visible = ([int]$shape.Visible -ne 0) } catch {}
                    $zorder = 0
                    try { $zorder = [int]$shape.ZOrderPosition } catch {}
                    $clampedShapeText = Read-ShapeTextClamped $shape $maxChars
                    $shapes += @{
                        name             = [string]$shape.Name
                        type             = (Get-ShapeTypeName ([int]$shape.Type))
                        z_order          = $zorder
                        text             = $clampedShapeText.Text
                        text_truncated   = $clampedShapeText.Truncated
                        visible          = $visible
                    }
                    if ($clampedShapeText.Text -and $shape.Name -ne $titleShapeName) {
                        if (Add-BudgetedText $bodySb $clampedShapeText.Text $maxChars "`n") { $bodyTruncated = $true }
                    }
                    if ($clampedShapeText.Truncated) { $bodyTruncated = $true }
                }
                $hidden = $false
                try { $hidden = ([int]$slide.SlideShowTransition.Hidden -eq -1) } catch {}
                $clampedNotes = Get-SlideNotesTextClamped $slide $maxChars
                $bodyText = $bodySb.ToString()
                $slides += @{
                    slide_number      = [int]$slide.SlideNumber
                    title             = $clampedTitle.Text
                    title_truncated   = $clampedTitle.Truncated
                    body_text         = $bodyText
                    body_truncated    = $bodyTruncated
                    notes             = $clampedNotes.Text
                    notes_truncated   = $clampedNotes.Truncated
                    hidden            = $hidden
                    shapes            = $shapes
                    shapes_truncated  = $shapesTruncated
                }
                # 追いMed是正 (2): 実テキスト長に加えて、図形/スライドの構造オーバーヘッド見積りも予算へ計上する
                # （テキストがほぼ空でも、図形/スライド数そのものが多ければ予算超過の引き金になるようにする）。
                $totalChars += $clampedTitle.Text.Length + $bodyText.Length + $clampedNotes.Text.Length + $script:StructureOverheadCharsPerSlide
                foreach ($s in $shapes) { $totalChars += $s.text.Length + $script:StructureOverheadCharsPerShape }
                if ($totalChars -gt $charBudget) {
                    throw ("STRUCTURE_TOO_LARGE: accumulated structure text exceeds conservative char budget ({0} chars > {1} limit derived from max_structure_json_bytes)" -f $totalChars, $charBudget)
                }
            }
            $verVal = (Get-OfficeVersionsCached).powerpoint
            if ($verVal -is [bool]) { $verVal = $null }    # 登録はあるがバージョン不明（Get-OneOfficeVersion 参照）
            return @{
                worker_version   = "1.0"
                office_app       = "powerpoint"
                office_version   = $verVal
                slide_count      = [int]$pres.Slides.Count
                slides_truncated = $slidesTruncated
                slides           = $slides
            }
        } finally {
            $pres.Close()
        }
    } finally {
        $app.Quit()
        [System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) | Out-Null
    }
}

function Invoke-ExtractStructureOnce {
    # -ExtractStructureOnce: 隔離子プロセス本体（Invoke-OfficeJobOnce と同型）。PowerPoint 補助構造を JSON で
    # $env:SHERPA_OC_OUT へ書く（成功で exit 0）。失敗はエラー文言を $env:SHERPA_OC_ERR へ書き exit 1。
    # `[System.Text.Encoding]::UTF8.GetBytes(...)` で書く（`Set-Content -Encoding UTF8` は既定で BOM を
    # 付けてしまう・Write-JsonResponse と同じ理由で BOM 無しに揃える）。
    #
    # Med是正: フィールド単位の増分切り詰め・スライド/図形数の上限・蓄積中の粗予算検査は Get-PowerPointStructure
    # 側で既に行っている（メモリ確保そのものを抑える一次防衛）。ここでの直列化後の総バイト数検査は
    # **最終防衛**（JSON の構造的オーバーヘッドや粗予算の見積もり誤差を吸収する最後の砦）。超過は成功扱いに
    # しない（"STRUCTURE_TOO_LARGE:" プレフィクス付きのエラーで throw ＝ Handle-ExtractStructureUpload 側が
    # この印を見て 413 相当へ変換する）。
    $inPath = $env:SHERPA_OC_INPUT
    $outFile = $env:SHERPA_OC_OUT
    $errFile = $env:SHERPA_OC_ERR
    $pidFile = $env:SHERPA_OC_PIDFILE
    try {
        $ext = [System.IO.Path]::GetExtension($inPath).ToLowerInvariant()
        if (-not $script:ExtractStructureExtMap[$ext]) { throw "unsupported extension for extract-structure: $ext" }
        $structure = Get-PowerPointStructure $inPath $pidFile
        $json = ($structure | ConvertTo-Json -Compress -Depth 10)
        $jsonBytes = [System.Text.Encoding]::UTF8.GetBytes($json)
        $maxJsonBytes = Get-MaxStructureJsonBytes
        if ($jsonBytes.Length -gt $maxJsonBytes) {
            throw ("STRUCTURE_TOO_LARGE: extracted structure json exceeds max_structure_json_bytes ({0} bytes > {1} limit)" -f $jsonBytes.Length, $maxJsonBytes)
        }
        [System.IO.File]::WriteAllBytes($outFile, $jsonBytes)
        exit 0
    } catch {
        try { Set-Content -LiteralPath $errFile -Value $_.Exception.Message -Encoding UTF8 } catch {}
        exit 1
    }
}

function Invoke-OfficeJobOnce([bool]$asPdf) {
    # -ConvertOnce / -RenderPdf 共通の隔離子プロセス本体。環境変数から入出力を受け取り、成功で $OUT に
    # ファイル（OOXML or PDF）を書き exit 0、失敗でエラー文言を $ERR に書き exit 1（親がステータス/本文へ
    # 変換する）。$SHERPA_OC_PIDFILE には Convert-* が「この変換が起こした Office プロセス候補」を書く
    # （親のタイムアウト kill が読む・RV High）。出力先 $OUT は親（listener の %TEMP% or DirectJob の %TEMP%）が
    # 指定するローカル NTFS パス＝Office は常にローカルへ書く（ネットワーク直書きの不安定を避ける）。
    $inPath = $env:SHERPA_OC_INPUT
    $outFile = $env:SHERPA_OC_OUT
    $errFile = $env:SHERPA_OC_ERR
    $pidFile = $env:SHERPA_OC_PIDFILE
    try {
        $ext = [System.IO.Path]::GetExtension($inPath).ToLowerInvariant()
        if ($asPdf) {
            $entry = $script:RenderExtMap[$ext]
            if (-not $entry) { throw "unsupported extension for render: $ext" }
            $appName = $entry.App; $target = "pdf"
        } else {
            $map = $script:ExtMap[$ext]
            if (-not $map) { throw "unsupported extension: $ext" }
            $appName = $map.App; $target = $map.Target
        }
        switch ($appName) {
            "word"       { Convert-Word $inPath $outFile $pidFile $target }
            "excel"      { Convert-Excel $inPath $outFile $pidFile $target }
            "powerpoint" { Convert-PowerPoint $inPath $outFile $pidFile $target }
            default      { throw "no converter for extension: $ext" }
        }
        if (-not (Test-Path -LiteralPath $outFile)) { throw "conversion produced no output" }
        exit 0
    } catch {
        try { Set-Content -LiteralPath $errFile -Value $_.Exception.Message -Encoding UTF8 } catch {}
        exit 1
    }
}

# ---- listener 側ヘルパ ----

$script:VersionCache = $null

function Get-OneOfficeVersion($progId) {
    # 検出可否/バージョンをレジストリ（ProgID の CurVer）から**軽量に**判定する。COM は起動しない
    # （healthz を軽くする＝WSL 側の短い到達タイムアウトに収める／未導入環境で COM 活性化がハングするのを避ける）。
    # 例: HKCR\Word.Application\CurVer = "Word.Application.16" → "16.0"。未登録（＝未導入）は $false。
    try {
        $curver = (Get-ItemProperty -Path ("Registry::HKEY_CLASSES_ROOT\{0}\CurVer" -f $progId) -ErrorAction Stop).'(default)'
    } catch {
        return $false
    }
    if ($curver -match '\.(\d+)$') { return ($matches[1] + '.0') }
    return $true                                          # 登録はあるがバージョン不明
}

function Get-OfficeVersionsCached {
    # healthz は軽量に＝バージョン取得（レジストリ参照）は初回のみ（プロセス内キャッシュ）。
    if ($null -ne $script:VersionCache) { return $script:VersionCache }
    $script:VersionCache = @{
        word       = (Get-OneOfficeVersion "Word.Application")
        excel      = (Get-OneOfficeVersion "Excel.Application")
        powerpoint = (Get-OneOfficeVersion "PowerPoint.Application")
    }
    return $script:VersionCache
}

function Stop-CandidateProcesses($pidFile, [datetime]$requestStart) {
    # RV High（2026-07-08）: pidfile の候補だけを、キル直前に**再検証してから** kill する。
    # 全条件を満たすものだけ Stop-Process:
    #   (1) PID がまだ存命（既に居ない＝安全側で何もしない）
    #   (2) イメージ名が記録と一致（無関係プロセスへの誤爆を防ぐ）
    #   (3) CreationDate が記録と厳密一致（PID 再利用ガード＝元プロセスが死に別プロセスが同じ PID を
    #       引き継いでいた場合、CreationDate が変わるので弾ける）
    #   (4) CreationDate が要求開始時刻以降（要求開始前から存在＝アタッチ＝絶対に殺さない防御の重ね掛け）
    if (-not $pidFile -or -not (Test-Path -LiteralPath $pidFile)) { return }
    try {
        $raw = Get-Content -LiteralPath $pidFile -Raw -ErrorAction Stop
        $candidates = $raw | ConvertFrom-Json
    } catch {
        return
    }
    foreach ($c in @($candidates)) {
        try {
            $proc = Get-Process -Id $c.pid -ErrorAction SilentlyContinue
            if (-not $proc -or $proc.ProcessName -ne $c.name) { continue }
            $now = Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId={0}" -f $c.pid) -ErrorAction SilentlyContinue
            if (-not $now) { continue }
            if ($now.CreationDate.ToString("o") -ne $c.creation_date) { continue }
            if ([datetime]$now.CreationDate -lt $requestStart) { continue }
            Stop-Process -Id $c.pid -Force -ErrorAction SilentlyContinue
        } catch { continue }
    }
}

function Test-ConvertPathShape($p) {
    # RV Med（2026-07-08）: device/extended-length（\\.\, \\?\）・ADS（`...:stream`）・変則 UNC サーバ名
    # （先頭が . や ?）を拒否する明示ホワイトリスト。GetFullPath 正規化の前後どちらでも同じ形を要求する
    # （呼び出し元 Test-ConvertPath が正規化前後の2回この関数を呼ぶ）。
    if (-not [System.IO.Path]::IsPathRooted($p)) { return $false }
    if ($p.StartsWith('\\.\') -or $p.StartsWith('\\?\')) { return $false }   # device/extended-length 拒否
    if ($p -match '^[A-Za-z]:\\') {
        # ドライブ以外の位置にコロンを含まない（`C:\x\a.doc:stream` のような ADS を拒否）。
        return -not ($p.Substring(2).Contains(':'))
    }
    # UNC: `\\server\...`。server は英数字/_.$- のみ・先頭が `.`（`?` は文字クラス外で既に除外済み）でない。
    if ($p -match '^\\\\(?!\.)[A-Za-z0-9_.$-]+\\') { return $true }
    return $false
}

function Test-ConvertPath($p) {
    # 絶対のローカルドライブ（C:\...）または UNC（\\host\...）のみ許可。相対・空・その他は拒否。
    # GetFullPath で正規化した後にも同じ形を再検証する（".." 等の畳み込みで形が変わっていないことの確認）。
    if (-not $p) { return $false }
    if (-not (Test-ConvertPathShape $p)) { return $false }
    $full = $null
    try { $full = [System.IO.Path]::GetFullPath($p) } catch { return $false }
    return Test-ConvertPathShape $full
}

function Invoke-OfficeJob($inPath, $outExt, $childSwitch, $timeoutSec, $optionsPath = "") {
    # 変換/レンダ1件を隔離子プロセス（-ConvertOnce or -RenderPdf・STA）で実行し、タイムアウトで子プロセスを
    # kill する。Office プロセスは「この変換が New-Object 直後に記録した候補」（pidfile）だけを、キル直前に
    # 再検証してから kill する（RV High・Stop-CandidateProcesses 参照）。子は結果をローカル %TEMP% の $tmpOut
    # に書き、ここでバイトを読み取って返す（listener は HTTP 応答へ、DirectJob は WSL の出力ファイルへ書く）。
    # $childSwitch = "-ConvertOnce"（OOXML）｜"-RenderPdf"（PDF）。$outExt = 出力拡張子（docx/xlsx/pptx/pdf）。
    $tmpOut = Join-Path $env:TEMP ("sherpa-com-" + [guid]::NewGuid().ToString("N") + "." + $outExt)
    $tmpErr = Join-Path $env:TEMP ("sherpa-com-" + [guid]::NewGuid().ToString("N") + ".err")
    $tmpPid = Join-Path $env:TEMP ("sherpa-com-" + [guid]::NewGuid().ToString("N") + ".pid.json")
    $requestStart = Get-Date
    $psExe = (Get-Process -Id $PID).Path
    if (-not $psExe) { $psExe = "powershell.exe" }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $psExe
    # 入出力パスは env で渡す（引用符の取り回しを避ける）。スクリプトパスのみ引用（空白対応）。
    $psi.Arguments = ('-NoProfile -NonInteractive -STA -ExecutionPolicy Bypass -File "{0}" {1}' -f $PSCommandPath, $childSwitch)
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.EnvironmentVariables["SHERPA_OC_INPUT"] = $inPath
    $psi.EnvironmentVariables["SHERPA_OC_TARGET"] = $outExt
    $psi.EnvironmentVariables["SHERPA_OC_OUT"] = $tmpOut
    $psi.EnvironmentVariables["SHERPA_OC_ERR"] = $tmpErr
    $psi.EnvironmentVariables["SHERPA_OC_PIDFILE"] = $tmpPid
    if ($optionsPath) { $psi.EnvironmentVariables["SHERPA_OC_OPTIONS"] = $optionsPath }

    $proc = [System.Diagnostics.Process]::Start($psi)
    $exited = $proc.WaitForExit($timeoutSec * 1000)
    if (-not $exited) {
        # タイムアウト: 子プロセスをまず停止し、続いて pidfile の候補（この変換が作った Office インスタンス
        # だけ）を再検証の上で停止する。候補が空（＝既存インスタンスへアタッチ）なら何も殺さない。
        try { $proc.Kill() } catch {}
        Start-Sleep -Milliseconds 200
        Stop-CandidateProcesses $tmpPid $requestStart
        Remove-Item -LiteralPath $tmpOut, $tmpErr, $tmpPid -ErrorAction SilentlyContinue
        return @{ Ok = $false; Status = 504; Error = ("conversion timed out after {0}s" -f $timeoutSec) }
    }
    try {
        if ($proc.ExitCode -eq 0 -and (Test-Path -LiteralPath $tmpOut)) {
            return @{ Ok = $true; Bytes = [System.IO.File]::ReadAllBytes($tmpOut) }
        }
        $msg = "conversion failed"
        if (Test-Path -LiteralPath $tmpErr) {
            $raw = (Get-Content -LiteralPath $tmpErr -Raw -ErrorAction SilentlyContinue)
            if ($raw) { $msg = $raw.Trim() }
        }
        return @{ Ok = $false; Status = 500; Error = $msg }
    } finally {
        Remove-Item -LiteralPath $tmpOut, $tmpErr, $tmpPid -ErrorAction SilentlyContinue
    }
}

function Write-ResponseBytes($resp, [int]$status, [byte[]]$bytes, [string]$contentType) {
    $resp.StatusCode = $status
    $resp.ContentType = $contentType
    $resp.ContentLength64 = $bytes.Length
    $resp.OutputStream.Write($bytes, 0, $bytes.Length)
    $resp.OutputStream.Close()
}

function Write-JsonResponse($resp, [int]$status, $obj) {
    $json = ($obj | ConvertTo-Json -Compress -Depth 6)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    Write-ResponseBytes $resp $status $bytes "application/json; charset=utf-8"
}

function Handle-Convert($req, $resp) {
    $enc = $req.ContentEncoding
    if (-not $enc) { $enc = [System.Text.Encoding]::UTF8 }
    $reader = New-Object System.IO.StreamReader($req.InputStream, $enc)
    try { $body = $reader.ReadToEnd() } finally { $reader.Close() }

    $data = $null
    try { $data = $body | ConvertFrom-Json } catch { $data = $null }
    if ($null -eq $data) { Write-JsonResponse $resp 400 @{ error = "invalid JSON body" }; return }

    $path = [string]$data.path
    $target = [string]$data.target
    if (-not (Test-ConvertPath $path)) {
        Write-JsonResponse $resp 400 @{ error = "path must be an absolute local or UNC path" }; return
    }
    $ext = [System.IO.Path]::GetExtension($path).ToLowerInvariant()
    $map = $script:ExtMap[$ext]
    if (-not $map) {
        Write-JsonResponse $resp 400 @{ error = ("unsupported extension: {0}" -f $ext) }; return
    }
    if ($target -and $target -ne $map.Target) {
        Write-JsonResponse $resp 400 @{ error = ("target mismatch: expected {0} for {1}" -f $map.Target, $ext) }; return
    }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Write-JsonResponse $resp 404 @{ error = "file not found" }; return
    }

    $r = Invoke-OfficeJob $path $map.Target "-ConvertOnce" $TimeoutSec
    if ($r.Ok) {
        Write-ResponseBytes $resp 200 $r.Bytes "application/octet-stream"
    } else {
        Write-JsonResponse $resp $r.Status @{ error = $r.Error }
    }
}

function Handle-Render($req, $resp) {
    # POST /render: 入力（旧/新 Office 形式）を PDF（as-displayed 忠実レンダ）にして octet-stream で返す
    # （Officeのas-displayed忠実レンダ・W2'）。/convert と同じパス検証＋直列＋タイムアウト＋Office kill を使う。
    $enc = $req.ContentEncoding
    if (-not $enc) { $enc = [System.Text.Encoding]::UTF8 }
    $reader = New-Object System.IO.StreamReader($req.InputStream, $enc)
    try { $body = $reader.ReadToEnd() } finally { $reader.Close() }

    $data = $null
    try { $data = $body | ConvertFrom-Json } catch { $data = $null }
    if ($null -eq $data) { Write-JsonResponse $resp 400 @{ error = "invalid JSON body" }; return }

    $path = [string]$data.path
    if (-not (Test-ConvertPath $path)) {
        Write-JsonResponse $resp 400 @{ error = "path must be an absolute local or UNC path" }; return
    }
    $ext = [System.IO.Path]::GetExtension($path).ToLowerInvariant()
    if (-not $script:RenderExtMap[$ext]) {
        Write-JsonResponse $resp 400 @{ error = ("unsupported extension for render: {0}" -f $ext) }; return
    }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Write-JsonResponse $resp 404 @{ error = "file not found" }; return
    }

    $r = Invoke-OfficeJob $path "pdf" "-RenderPdf" $TimeoutSec
    if ($r.Ok) {
        Write-ResponseBytes $resp 200 $r.Bytes "application/pdf"
    } else {
        Write-JsonResponse $resp $r.Status @{ error = $r.Error }
    }
}

# ---- upload 系（OFFICE-WIN-001・共有ストレージ無しの独立 Linux サーバー向け・§6.5）----
#
# multipart/form-data を PowerShell 5.1 標準のみで手組みする（追加モジュール依存ゼロ）。
#
# レビュー是正（High/Med-1）: 旧実装は全 body を ISO-8859-1 で string 化してから String.Split で
# パート分割していた。.NET の string は内部 UTF-16（1文字2バイト）のため、body を丸ごと文字列化すると
# **元バイト数の約2倍**のメモリを追加消費し（500MiB のファイルで +1GB 級）、さらに Split() がセグメント毎に
# 新しい string を複製してそこから再度 GetBytes するためピークではさらに積み上がる（worker OOM リスク＝
# High）。加えて素朴な Split は裸の "--boundary" 文字列に一致する箇所ならファイル本体の中身でも区切りとして
# 誤認識しうる（Med-1・RFC 2046 の区切りは "CRLF--boundary" のみ・先頭パートに限り例外で body 先頭の
# "--boundary" も可）。
#
# 是正: body を文字列化せず**バイト列のまま**扱う。delimiter（CRLF+"--"+boundary）の探索は
# `Find-ByteSequence`（[Array]::IndexOf の高速パスで先頭バイト候補を絞り、一致箇所だけ残りバイトを検証）で
# 行い、ヘッダ部分（数十〜数百バイト・ASCII 前提）だけを都度小さく ISO-8859-1 デコードする。ファイルパートの
# バイトは `[Array]::Copy` で1回だけ切り出す。ピークメモリは概ね「受信 body 1個分＋ファイルパート1個分」
# （文字列化コピーが無いため旧実装の数分の1）。ファイル名の非 ASCII 部分はヘッダのこの小デコードでも正しい
# 文字には戻らないが、ここで使うのは拡張子（ASCII 部分）だけなので実害はない。

# multipart の非ファイル部分（boundary 行・ヘッダ・target/source_hash フィールド値）のおおよその上乗せ許容量。
# $MaxFileBytes は契約上「ファイル本体」の上限だが、実際のリクエストボディはそれに boundary/ヘッダ/他
# フィールドが上乗せされるため、読み取りの打ち切り閾値には少し余裕を持たせる（64KiB＝複数パートのヘッダと
# target/source_hash として十分すぎる余裕）。ファイル本体そのものの上限は Get-MultipartFile がパース後に
# 厳密に再検査する（Read-BoundedBytes の閾値はあくまで「読み取りを打ち切る目安」）。
# Excel表示対象（最大5万cell）のUTF-8 JSONも非file partとして同じmultipartへ載る。file本体の上限は
# パース後に別途厳密検査するため、ここではoptionsを含むbody上乗せを8MiBまで許可する。
$script:MultipartOverheadBytes = 8454144   # 8MiB + 従来の64KiB

function Find-ByteSequence([byte[]]$hay, [byte[]]$needle, [int]$start) {
    # High 是正の要: body を文字列化せず delimiter をバイト列のまま探す。先頭バイトの一致候補だけ
    # [Array]::IndexOf（BCL のネイティブ実装・PowerShell の逐次比較ループより大幅に速い）で絞り込み、
    # 候補ごとに残りバイトだけを比較する（愚直な全バイト逐次比較よりずっと速い）。PowerShell 5.1 /
    # .NET Framework 4.x の範囲の API のみ使用（Span/Memory は使わない）。
    $hayLen = $hay.Length
    $needleLen = $needle.Length
    if ($needleLen -eq 0 -or $start -lt 0 -or $start -ge $hayLen) { return -1 }
    $first = [byte]$needle[0]
    $pos = $start
    while ($true) {
        $idx = [Array]::IndexOf($hay, $first, $pos)
        if ($idx -lt 0 -or ($idx + $needleLen) -gt $hayLen) { return -1 }
        $matched = $true
        for ($j = 1; $j -lt $needleLen; $j++) {
            if ($hay[$idx + $j] -ne $needle[$j]) { $matched = $false; break }
        }
        if ($matched) { return $idx }
        $pos = $idx + 1
    }
}

function Find-BoundaryMarker([byte[]]$bytes, [byte[]]$needle, [int]$start) {
    # Med 是正（RV 2巡目）: `Find-ByteSequence` は $needle（"--boundary" or CRLF+"--boundary"）に一致した
    # 位置を返すだけで、その**直後**が本当に区切りとして正しい形かは見ていなかった。RFC 2046 では
    # dash-boundary/delimiter の直後は transport-padding（SP/HTAB）を挟んで CRLF（次パートへ続く）または
    # "--"（終端 close-delimiter）が来る。ここでは自クライアント（`_build_multipart`）が transport-padding を
    # 送らないことを踏まえ、「直後が CRLF か `--` のときだけ本物の区切りとして採用する」という厳格側で判定する
    # （padding を送ってくる相手には対応しない・意図的な単純化）。
    #
    # Med 是正（RV 3巡目）: 上の判定のうち "--"（close-delimiter 候補）は、それを見た時点で即採用していたため、
    # 本文に "\r\n--<boundary>--X"（X は区切りでない任意バイト）が含まれる正当なファイルを誤って
    # close-delimiter と誤認識しうる（"--" もまた boundary token の接頭辞衝突と同じ穴を持つ）。close-delimiter
    # として採用してよいのは、"--" の**さらに直後**が CRLF（末尾に epilogue が続く形）か EOF（バッファ終端＝
    # ちょうど body の末尾で終わる形）のときだけとする（厳格側＝transport-padding や余分バイトは許さない・
    # CRLF/EOF 以外は次の候補位置から探索を継続する）。
    #
    # 合致しなければ、ファイル本体に boundary token の接頭辞と同じバイト列がたまたま含まれているだけ
    # （例: 実 boundary が "ABC" で本文中に "\r\n--ABCDEF" や "\r\n--ABC--XYZ" のような継続バイト列がある）と
    # みなし、次の候補位置から探索を継続する（見つけた位置を境界として切り詰めない＝本体を壊さない）。
    $len = $bytes.Length
    $pos = $start
    while ($true) {
        $idx = Find-ByteSequence $bytes $needle $pos
        if ($idx -lt 0) { return -1 }
        $after = $idx + $needle.Length
        $isCrlf = (($after + 1) -lt $len -and $bytes[$after] -eq 0x0D -and $bytes[$after + 1] -eq 0x0A)
        if ($isCrlf) { return $idx }
        $isCloseCandidate = (($after + 1) -lt $len -and $bytes[$after] -eq 0x2D -and $bytes[$after + 1] -eq 0x2D)
        if ($isCloseCandidate) {
            # "--" 自体は候補にすぎない。その直後が CRLF か EOF のときだけ本物の close-delimiter とみなす。
            $afterClose = $after + 2
            $closeIsCrlf = (($afterClose + 1) -lt $len -and $bytes[$afterClose] -eq 0x0D -and $bytes[$afterClose + 1] -eq 0x0A)
            $closeIsEof = ($afterClose -eq $len)
            if ($closeIsCrlf -or $closeIsEof) { return $idx }
        }
        $pos = $idx + 1
    }
}

function Get-Sha256Hex([byte[]]$bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash($bytes)
        return -join ($hash | ForEach-Object { $_.ToString("x2") })
    } finally {
        $sha.Dispose()
    }
}

function Get-MultipartBoundary($req) {
    $ctype = $req.ContentType
    if (-not $ctype) { return $null }
    if ($ctype -notmatch 'multipart/form-data') { return $null }
    if ($ctype -notmatch 'boundary="?([^";]+)"?') { return $null }
    return $matches[1]
}

function Read-BoundedBytes($stream, [long]$readCeiling) {
    # RV（OFFICE-WIN-001）: Content-Length が無い/信頼できない場合に備え、読み取り中も上限
    # （呼び出し元が $script:MultipartOverheadBytes を上乗せ済みの $readCeiling）を超えたら即座に打ち切る
    # （413）。413 判定用に専用の例外型で throw する。ここでのコピーは MemoryStream への書き込み＋最後の
    # ToArray() の1回だけ（body を文字列化しない・上のコメント参照）。
    $ms = New-Object System.IO.MemoryStream
    $buf = New-Object byte[] 65536
    $total = 0L
    while ($true) {
        $n = $stream.Read($buf, 0, $buf.Length)
        if ($n -le 0) { break }
        $total += $n
        if ($total -gt $readCeiling) {
            throw (New-Object System.IO.InvalidDataException("body exceeds max_file_bytes"))
        }
        $ms.Write($buf, 0, $n)
    }
    return $ms.ToArray()
}

function Parse-MultipartParts([byte[]]$bytes, [string]$boundary) {
    # 戻り値: @{ Name=<form名>; FileName=(ファイルパートのみ); Bytes=[byte[]] } の配列。
    #
    # RFC 2046 §5.1.1 のとおり厳密に解釈する: パート区切りは "CRLF--boundary"（先頭パートのみ body 先頭の
    # "--boundary" も可＝以下では最初の1回だけ CRLF 無しの dash-boundary で探す）・終端は
    # "CRLF--boundary--"。2 パート目以降は必ず CRLF プレフィックス付きの delimiter だけを区切りとして扱う
    # ため、ファイル本体に "--boundary" と同じバイト列が単独で（CRLF 無しで）含まれていても誤って区切りと
    # 認識しない（Med-1）。
    #
    # body を文字列化しない（High 是正）: delimiter 探索はバイト列のまま `Find-ByteSequence` で行い、
    # ヘッダだけを都度小さく ISO-8859-1 デコードし、ファイルパートのバイトは `[Array]::Copy` で1回だけ
    # 切り出す。ピークメモリは概ね「body 1個分＋file part 1個分」。
    #
    # Med 是正（RV 2巡目・RV 3巡目）: `Find-ByteSequence` は「一致した位置」しか返さず、その直後が本当に
    # 区切りの形かまでは見ていなかった。ファイル本文に "\r\n--<boundary>X"（X は区切りでない任意のバイト＝
    # boundary token の接頭辞が偶然一致しただけ）や "\r\n--<boundary>--X"（"--" 自体も同じ接頭辞衝突の穴を
    # 持つ）が含まれる正当な multipart では、これを delimiter/close-delimiter と誤認して本文を切り詰めて
    # しまう。`Find-BoundaryMarker` で「一致位置の直後が CRLF」または「`--` の場合はさらにその直後が CRLF か
    # EOF」まで検証してから採用する（先頭パートの dash-boundary 探索・2パート目以降の delimiter 探索の
    # 両方に適用・判定の詳細は `Find-BoundaryMarker` 本体のコメント参照）。
    $enc = [System.Text.Encoding]::GetEncoding(28591)   # ISO-8859-1（Latin1）: ヘッダの小デコード専用
    $ascii = [System.Text.Encoding]::ASCII
    $dashBoundary = $ascii.GetBytes("--" + $boundary)
    $delim = $ascii.GetBytes("`r`n--" + $boundary)
    $crlfcrlf = $ascii.GetBytes("`r`n`r`n")

    $out = @()
    $len = $bytes.Length
    # 先頭パート: 通常は body 先頭の "--boundary"（CRLF 無し）から始まる。前文があっても最初に見つかる
    # dash-boundary（直後が CRLF か "--" のもの）から始める（大半のクライアントは前文を送らない）。
    $pos = Find-BoundaryMarker $bytes $dashBoundary 0
    while ($pos -ge 0) {
        $lineStart = $pos + $dashBoundary.Length
        if (($lineStart + 1) -lt $len -and $bytes[$lineStart] -eq 0x2D -and $bytes[$lineStart + 1] -eq 0x2D) {
            break   # "--boundary--"（終端デリミタ）＝ここで打ち切り
        }
        $headerStart = $lineStart
        if (($headerStart + 1) -lt $len -and $bytes[$headerStart] -eq 0x0D -and $bytes[$headerStart + 1] -eq 0x0A) {
            $headerStart += 2   # boundary 行の CRLF を飛ばしてヘッダ開始位置へ
        }
        $sepIdx = Find-ByteSequence $bytes $crlfcrlf $headerStart
        if ($sepIdx -lt 0) { break }   # ヘッダ終端（空行）が見つからない＝不正な multipart
        $bodyStart = $sepIdx + $crlfcrlf.Length
        $nextDelim = Find-BoundaryMarker $bytes $delim $bodyStart
        $bodyEnd = if ($nextDelim -ge 0) { $nextDelim } else { $len }

        $headerLen = $sepIdx - $headerStart
        $headerText = $enc.GetString($bytes, $headerStart, $headerLen)   # ヘッダだけ小さくデコード
        $name = $null; $filename = $null
        if ($headerText -match 'name="([^"]*)"') { $name = $matches[1] }
        if ($headerText -match 'filename="([^"]*)"') { $filename = $matches[1] }

        $partLen = $bodyEnd - $bodyStart
        $partBytes = New-Object byte[] $partLen
        if ($partLen -gt 0) { [Array]::Copy($bytes, $bodyStart, $partBytes, 0, $partLen) }   # 1回だけコピー
        $out += , @{ Name = $name; FileName = $filename; Bytes = $partBytes }

        if ($nextDelim -lt 0) { break }
        $pos = $nextDelim + 2   # delim（CRLF+"--"+boundary）の CRLF 分だけ進めて次の dash-boundary 先頭へ揃える
    }
    return $out
}

function Get-MultipartFile($req, $resp, [long]$maxBytes) {
    # 共通の受信＋パース。413/400 は自前で応答済みで $null を返す（呼び出し元は $null なら return するだけ）。
    #
    # $maxBytes は契約どおり「ファイル本体」の上限。実際のリクエストボディは boundary・ヘッダ・
    # target/source_hash フィールド値の分だけ上乗せされるため、読み取りの打ち切り閾値
    # （$readCeiling）には $script:MultipartOverheadBytes の余裕を持たせ、パース後にファイルパートそのものの
    # 厳密なサイズを再検査する（413 の二重判定: 早期の Content-Length 判定＝読む前・ストリーム打ち切り＝
    # 読み取り中・最終ファイルサイズ検査＝パース後）。
    $boundary = Get-MultipartBoundary $req
    if (-not $boundary) {
        Write-JsonResponse $resp 400 @{ error = "multipart/form-data（boundary 付き）が必要です" }
        return $null
    }
    $readCeiling = $maxBytes + $script:MultipartOverheadBytes
    if ($req.ContentLength64 -gt 0 -and $req.ContentLength64 -gt $readCeiling) {
        Write-JsonResponse $resp 413 @{ error = "ファイルサイズが上限（max_file_bytes）を超えています" }
        return $null
    }
    try {
        $bytes = Read-BoundedBytes $req.InputStream $readCeiling
    } catch [System.IO.InvalidDataException] {
        Write-JsonResponse $resp 413 @{ error = "ファイルサイズが上限（max_file_bytes）を超えています" }
        return $null
    }
    $parts = Parse-MultipartParts $bytes $boundary
    foreach ($p in $parts) {
        if ($p.FileName -and $p.Bytes.Length -gt $maxBytes) {
            # readCeiling は通ったが、multipart 全体でなく「ファイル本体」そのものが上限を超えている
            # （overhead 余裕分を食い潰した／複数ファイルパートが送られた等）＝契約どおり 413。
            Write-JsonResponse $resp 413 @{ error = "ファイルサイズが上限（max_file_bytes）を超えています" }
            return $null
        }
    }
    return $parts
}

function Handle-ConvertUpload($req, $resp) {
    # POST /convert-upload: multipart(file, target=docx|xlsx|pptx, source_hash) → 変換済み OOXML バイト列。
    $parts = Get-MultipartFile $req $resp $script:MaxFileBytes
    if ($null -eq $parts) { return }
    $filePart = $parts | Where-Object { $_.FileName } | Select-Object -First 1
    $targetPart = $parts | Where-Object { $_.Name -eq "target" } | Select-Object -First 1
    $hashPart = $parts | Where-Object { $_.Name -eq "source_hash" } | Select-Object -First 1
    if (-not $filePart -or -not $targetPart -or -not $hashPart) {
        Write-JsonResponse $resp 400 @{ error = "file・target・source_hash が必要です" }; return
    }
    $textEnc = [System.Text.Encoding]::GetEncoding(28591)
    $target = ($textEnc.GetString($targetPart.Bytes)).Trim()
    $sourceHash = ($textEnc.GetString($hashPart.Bytes)).Trim()
    $ext = [System.IO.Path]::GetExtension($filePart.FileName).ToLowerInvariant()
    $map = $script:ExtMap[$ext]                          # 既存の拡張子ホワイトリストを再利用（path 方式と共通）
    if (-not $map) {
        Write-JsonResponse $resp 400 @{ error = ("unsupported extension: {0}" -f $ext) }; return
    }
    # Low 是正: target は非空かつ docx|xlsx|pptx（拡張子から導かれる期待値）のみ許可。旧実装は
    # `$target -and ...`（真偽判定）だったため空文字列 "" は偽＝この検査を素通りしていた（空値を暗黙に
    # 受理してしまう抜け穴）。空・未知値のどちらも一律 400 にする。
    if ($target -ne $map.Target) {
        Write-JsonResponse $resp 400 @{ error = ("target mismatch: expected {0} for {1}" -f $map.Target, $ext) }; return
    }
    $actualHash = Get-Sha256Hex $filePart.Bytes
    if ($actualHash -ne $sourceHash.ToLowerInvariant()) {
        Write-JsonResponse $resp 400 @{ error = "source_hash mismatch" }; return
    }
    $tmpIn = Join-Path $script:UploadTempDir ("sherpa-com-upload-" + [guid]::NewGuid().ToString("N") + $ext)
    try {
        [System.IO.File]::WriteAllBytes($tmpIn, $filePart.Bytes)
        $r = Invoke-OfficeJob $tmpIn $map.Target "-ConvertOnce" $TimeoutSec
        if ($r.Ok) {
            Write-ResponseBytes $resp 200 $r.Bytes "application/octet-stream"
        } else {
            Write-JsonResponse $resp $r.Status @{ error = $r.Error }
        }
    } finally {
        Remove-Item -LiteralPath $tmpIn -ErrorAction SilentlyContinue   # 原本は処理後に必ず削除（Windows側に残さない）
    }
}

function Handle-RenderUpload($req, $resp) {
    # POST /render-upload: multipart(file, source_hash) → PDF（as-displayed 忠実レンダ）バイト列。
    $parts = Get-MultipartFile $req $resp $script:MaxFileBytes
    if ($null -eq $parts) { return }
    $filePart = $parts | Where-Object { $_.FileName } | Select-Object -First 1
    $hashPart = $parts | Where-Object { $_.Name -eq "source_hash" } | Select-Object -First 1
    if (-not $filePart -or -not $hashPart) {
        Write-JsonResponse $resp 400 @{ error = "file・source_hash が必要です" }; return
    }
    $textEnc = [System.Text.Encoding]::GetEncoding(28591)
    $sourceHash = ($textEnc.GetString($hashPart.Bytes)).Trim()
    $ext = [System.IO.Path]::GetExtension($filePart.FileName).ToLowerInvariant()
    if (-not $script:RenderExtMap[$ext]) {                # 既存の render 対応拡張子（旧/新両方）を再利用
        Write-JsonResponse $resp 400 @{ error = ("unsupported extension for render: {0}" -f $ext) }; return
    }
    $actualHash = Get-Sha256Hex $filePart.Bytes
    if ($actualHash -ne $sourceHash.ToLowerInvariant()) {
        Write-JsonResponse $resp 400 @{ error = "source_hash mismatch" }; return
    }
    $tmpIn = Join-Path $script:UploadTempDir ("sherpa-com-upload-" + [guid]::NewGuid().ToString("N") + $ext)
    try {
        [System.IO.File]::WriteAllBytes($tmpIn, $filePart.Bytes)
        $r = Invoke-OfficeJob $tmpIn "pdf" "-RenderPdf" $TimeoutSec
        if ($r.Ok) {
            Write-ResponseBytes $resp 200 $r.Bytes "application/pdf"
        } else {
            Write-JsonResponse $resp $r.Status @{ error = $r.Error }
        }
    } finally {
        Remove-Item -LiteralPath $tmpIn -ErrorAction SilentlyContinue
    }
}

function Handle-ExtractStructureUpload($req, $resp) {
    # POST /extract-structure-upload: multipart(file=.ppt/.pptx, source_hash[, options]) → PowerPoint 補助構造
    # JSON（OFFICE-WIN-001 ⑤・試作）。受信・検証・一時ファイル・削除は Handle-ConvertUpload/Handle-RenderUpload
    # と同じ流儀（既存の Get-MultipartFile・Get-Sha256Hex・トークン検査を再利用）。`options` パートは現状
    # 未使用（将来の抽出オプション向けの予約・送られても無視するだけで拒否はしない）。
    $parts = Get-MultipartFile $req $resp $script:MaxFileBytes
    if ($null -eq $parts) { return }
    $filePart = $parts | Where-Object { $_.FileName } | Select-Object -First 1
    $hashPart = $parts | Where-Object { $_.Name -eq "source_hash" } | Select-Object -First 1
    if (-not $filePart -or -not $hashPart) {
        Write-JsonResponse $resp 400 @{ error = "file・source_hash が必要です" }; return
    }
    $textEnc = [System.Text.Encoding]::GetEncoding(28591)
    $sourceHash = ($textEnc.GetString($hashPart.Bytes)).Trim()
    $ext = [System.IO.Path]::GetExtension($filePart.FileName).ToLowerInvariant()
    if (-not $script:ExtractStructureExtMap[$ext]) {       # PowerPoint 限定（.ppt/.pptx のみ・試作）
        Write-JsonResponse $resp 400 @{ error = ("unsupported extension for extract-structure: {0}" -f $ext) }; return
    }
    $actualHash = Get-Sha256Hex $filePart.Bytes
    if ($actualHash -ne $sourceHash.ToLowerInvariant()) {
        Write-JsonResponse $resp 400 @{ error = "source_hash mismatch" }; return
    }
    $tmpIn = Join-Path $script:UploadTempDir ("sherpa-com-upload-" + [guid]::NewGuid().ToString("N") + $ext)
    try {
        [System.IO.File]::WriteAllBytes($tmpIn, $filePart.Bytes)
        $r = Invoke-OfficeJob $tmpIn "json" "-ExtractStructureOnce" $TimeoutSec
        if ($r.Ok) {
            Write-ResponseBytes $resp 200 $r.Bytes "application/json; charset=utf-8"
        } elseif ($r.Error -and $r.Error.StartsWith("STRUCTURE_TOO_LARGE:")) {
            # Med是正: フィールド上限を適用してもなお応答全体が上限を超えた場合（例: スライド数が極端に
            # 多い）は、部分結果を成功に見せず 413 相当のエラーとして扱う（Invoke-ExtractStructureOnce 参照）。
            Write-JsonResponse $resp 413 @{ error = $r.Error }
        } else {
            Write-JsonResponse $resp $r.Status @{ error = $r.Error }
        }
    } finally {
        Remove-Item -LiteralPath $tmpIn -ErrorAction SilentlyContinue   # 原本は処理後に必ず削除（Windows側に残さない）
    }
}

function Handle-ExtractExcelDisplayUpload($req, $resp) {
    # POST /extract-excel-display-upload: multipart(file=.xls/.xlsx, source_hash, cells_json) →
    # Range.Text/DisplayFormat.NumberFormat JSON。受信原本・optionsはいずれも処理後必ず削除する。
    $parts = Get-MultipartFile $req $resp $script:MaxFileBytes
    if ($null -eq $parts) { return }
    $filePart = $parts | Where-Object { $_.FileName } | Select-Object -First 1
    $hashPart = $parts | Where-Object { $_.Name -eq "source_hash" } | Select-Object -First 1
    $cellsPart = $parts | Where-Object { $_.Name -eq "cells_json" } | Select-Object -First 1
    if (-not $filePart -or -not $hashPart -or -not $cellsPart) {
        Write-JsonResponse $resp 400 @{ error = "file・source_hash・cells_json が必要です" }; return
    }
    if ($cellsPart.Bytes.Length -gt 8388608) {
        Write-JsonResponse $resp 413 @{ error = "cells_json が上限を超えています" }; return
    }
    $latin1 = [System.Text.Encoding]::GetEncoding(28591)
    $sourceHash = ($latin1.GetString($hashPart.Bytes)).Trim()
    $ext = [System.IO.Path]::GetExtension($filePart.FileName).ToLowerInvariant()
    if (-not $script:ExcelDisplayExtMap[$ext]) {
        Write-JsonResponse $resp 400 @{ error = ("unsupported extension for excel display: {0}" -f $ext) }; return
    }
    $actualHash = Get-Sha256Hex $filePart.Bytes
    if ($actualHash -ne $sourceHash.ToLowerInvariant()) {
        Write-JsonResponse $resp 400 @{ error = "source_hash mismatch" }; return
    }
    # UTF-8のsheet名をそのまま保持する。既存target/source_hashのLatin1 decodingとは意図的に分離する。
    try {
        $optionsText = [System.Text.Encoding]::UTF8.GetString($cellsPart.Bytes)
        $parsedOptions = $optionsText | ConvertFrom-Json
        if ($null -eq $parsedOptions -or $parsedOptions.schema -ne "sherpa-excel-display-v1") { throw "schema" }
    } catch {
        Write-JsonResponse $resp 400 @{ error = "invalid cells_json" }; return
    }
    $tmpIn = Join-Path $script:UploadTempDir ("sherpa-com-upload-" + [guid]::NewGuid().ToString("N") + $ext)
    $tmpOptions = Join-Path $script:UploadTempDir ("sherpa-com-options-" + [guid]::NewGuid().ToString("N") + ".json")
    try {
        [System.IO.File]::WriteAllBytes($tmpIn, $filePart.Bytes)
        [System.IO.File]::WriteAllBytes($tmpOptions, $cellsPart.Bytes)
        $r = Invoke-OfficeJob $tmpIn "json" "-ExtractExcelDisplayOnce" $TimeoutSec $tmpOptions
        if ($r.Ok) {
            Write-ResponseBytes $resp 200 $r.Bytes "application/json; charset=utf-8"
        } else {
            Write-JsonResponse $resp $r.Status @{ error = $r.Error }
        }
    } finally {
        Remove-Item -LiteralPath $tmpIn, $tmpOptions -ErrorAction SilentlyContinue
    }
}

# ---- W2' 直接呼び出しモード（one-shot・listener なし・URL/トークン不要）----

function Write-HealthzStdout {
    # -Healthz: COM 検出（word/excel/powerpoint の可否とバージョン）を JSON で stdout に出す。
    # HTTP /healthz と同形（{ok,versions,worker}）。WSL 側が stdout を読み到達性・拡張子ゲートに使う。
    # 情報行（Write-Host 等）は一切出さない＝stdout は純粋な JSON のみ（呼び出し側の json パースを汚さない）。
    $payload = @{ ok = $true; versions = (Get-OfficeVersionsCached); worker = "direct" }
    [Console]::Out.Write(($payload | ConvertTo-Json -Compress -Depth 6))
}

function Invoke-DirectJob {
    # -DirectJob: WSL interop から one-shot で変換/レンダ1件を実行する外側ジョブ。内部で Invoke-OfficeJob が
    # -ConvertOnce/-RenderPdf 子プロセスを起こす＝タイムアウト時に「この変換が作った Office」だけを kill する
    # Windows 側の監視（Stop-CandidateProcesses）が HTTP ワーカーと同じく効く。結果バイトは -OutPath へ書き、
    # 失敗理由は -ErrPath へ書く（WSL は OutPath を読めば bytes、無ければ ErrPath を読む）。exit code で成否も返す。
    $tsec = $JobTimeoutSec
    if ($tsec -le 0) {
        $tsec = if ($env:SHERPA_OFFICE_COM_TIMEOUT) { [int]$env:SHERPA_OFFICE_COM_TIMEOUT } else { 120 }
    }
    try {
        if (-not (Test-ConvertPath $InPath)) { throw "path must be an absolute local or UNC path" }
        if (-not (Test-Path -LiteralPath $InPath -PathType Leaf)) { throw "file not found" }
        $ext = [System.IO.Path]::GetExtension($InPath).ToLowerInvariant()
        if ($Job -eq "excel_display") {
            if (-not $script:ExcelDisplayExtMap[$ext]) { throw "unsupported extension for excel display: $ext" }
            if (-not (Test-ConvertPath $OptionsPath) -or -not (Test-Path -LiteralPath $OptionsPath -PathType Leaf)) {
                throw "excel display options path must be an existing absolute local or UNC path"
            }
            $r = Invoke-OfficeJob $InPath "json" "-ExtractExcelDisplayOnce" $tsec $OptionsPath
        } elseif ($Job -eq "render") {
            if (-not $script:RenderExtMap[$ext]) { throw "unsupported extension for render: $ext" }
            $r = Invoke-OfficeJob $InPath "pdf" "-RenderPdf" $tsec
        } elseif ($Job -eq "convert") {
            $map = $script:ExtMap[$ext]
            if (-not $map) { throw "unsupported extension: $ext" }
            $r = Invoke-OfficeJob $InPath $map.Target "-ConvertOnce" $tsec
        } else {
            throw "unsupported direct job: $Job"
        }
        if ($r.Ok) {
            [System.IO.File]::WriteAllBytes($OutPath, $r.Bytes)
            exit 0
        }
        if ($ErrPath) { try { Set-Content -LiteralPath $ErrPath -Value $r.Error -Encoding UTF8 } catch {} }
        exit 1
    } catch {
        if ($ErrPath) { try { Set-Content -LiteralPath $ErrPath -Value $_.Exception.Message -Encoding UTF8 } catch {} }
        exit 1
    }
}

# ---- エントリポイント ----

if ($ConvertOnce) {
    Invoke-OfficeJobOnce $false
    return
}
if ($RenderPdf) {
    Invoke-OfficeJobOnce $true
    return
}
if ($ExtractStructureOnce) {
    Invoke-ExtractStructureOnce
    return
}
if ($ExtractExcelDisplayOnce) {
    Invoke-ExtractExcelDisplayOnce
    return
}
if ($Healthz) {
    Write-HealthzStdout
    return
}
if ($DirectJob) {
    Invoke-DirectJob
    return
}

# 既定値の解決（引数 ＞ env ＞ ハードコード既定）。
if ($Port -le 0) {
    $Port = if ($env:SHERPA_OFFICE_COM_PORT) { [int]$env:SHERPA_OFFICE_COM_PORT } else { 8091 }
}
if (-not $Token) { $Token = $env:SHERPA_OFFICE_COM_TOKEN }
if ($TimeoutSec -le 0) {
    $TimeoutSec = if ($env:SHERPA_OFFICE_COM_TIMEOUT) { [int]$env:SHERPA_OFFICE_COM_TIMEOUT } else { 120 }
}
# OFFICE-WIN-001: upload 系の上限/一時ディレクトリ（引数 ＞ env ＞ 既定・path 方式の挙動には無関係）。
if ($MaxFileBytes -le 0) {
    $MaxFileBytes = if ($env:SHERPA_OFFICE_COM_MAX_FILE_BYTES) { [long]$env:SHERPA_OFFICE_COM_MAX_FILE_BYTES } else { 524288000 }
}
if (-not $TempDir) {
    $TempDir = if ($env:SHERPA_OFFICE_COM_TEMP_DIR) { $env:SHERPA_OFFICE_COM_TEMP_DIR } else { $env:TEMP }
}
$script:MaxFileBytes = $MaxFileBytes
$script:UploadTempDir = $TempDir
if (-not (Test-Path -LiteralPath $script:UploadTempDir)) {
    New-Item -ItemType Directory -Path $script:UploadTempDir -Force | Out-Null
}

if (-not $Token) {
    Write-Error "SHERPA_OFFICE_COM_TOKEN が未設定です。env か -Token で共有シークレットを設定してください（必須）。"
    exit 2
}

# RV Low（2026-07-08）: 既定は 127.0.0.1（LAN へ出さない）を維持。従来 NAT ネットワーキングで WSL から
# 127.0.0.1 に届かない場合など、明示的に非 loopback アドレスを指定したら起動ログで警告する
# （docs/manual/40-運用.md の NAT 節に urlacl・ファイアウォール制限の具体手順あり）。
if ($BindAddress -ne "127.0.0.1" -and $BindAddress -ne "localhost" -and $BindAddress -ne "::1") {
    $bindWarning = ("非 loopback アドレス（{0}）で待ち受けます。LAN に露出します。共有シークレットは必須のままですが、" -f $BindAddress) +
        "到達元を Windows Defender ファイアウォールで WSL のサブネットのみに絞ることを強く推奨します（詳細: docs/manual/40-運用.md）。"
    Write-Warning $bindWarning
}

$listener = New-Object System.Net.HttpListener
$prefix = "http://{0}:{1}/" -f $BindAddress, $Port
$listener.Prefixes.Add($prefix)
try {
    $listener.Start()
} catch [System.Net.HttpListenerException] {
    Write-Error ("listener を開始できませんでした（{0}）。アクセス拒否なら管理者で実行するか URL ACL を追加してください: netsh http add urlacl url={0} user={1}" -f $prefix, $env:USERNAME)
    exit 3
}
Write-Host ("office-com-worker listening on {0} (timeout={1}s)" -f $prefix, $TimeoutSec)

try {
    while ($listener.IsListening) {
        $ctx = $listener.GetContext()      # 直列: 1件処理してから次を受ける（COM は並列不可）
        try {
            $req = $ctx.Request
            $resp = $ctx.Response
            # 共有シークレット必須（両エンドポイント・不一致/欠落は 401）。
            # RV Low（2026-07-08）: -ne は大文字小文字を区別しない比較。トークンは -cne（case-sensitive）で比較する。
            if ($req.Headers["X-Sherpa-Token"] -cne $Token) {
                Write-JsonResponse $resp 401 @{ error = "invalid or missing token" }
                continue
            }
            $route = $req.Url.AbsolutePath
            if ($req.HttpMethod -eq "GET" -and $route -eq "/healthz") {
                Write-JsonResponse $resp 200 @{ ok = $true; versions = (Get-OfficeVersionsCached); worker = "1" }
                continue
            }
            if ($req.HttpMethod -eq "POST" -and $route -eq "/convert") {
                Handle-Convert $req $resp
                continue
            }
            if ($req.HttpMethod -eq "POST" -and $route -eq "/render") {
                Handle-Render $req $resp
                continue
            }
            if ($req.HttpMethod -eq "POST" -and $route -eq "/convert-upload") {
                Handle-ConvertUpload $req $resp
                continue
            }
            if ($req.HttpMethod -eq "POST" -and $route -eq "/render-upload") {
                Handle-RenderUpload $req $resp
                continue
            }
            if ($req.HttpMethod -eq "POST" -and $route -eq "/extract-structure-upload") {
                Handle-ExtractStructureUpload $req $resp
                continue
            }
            if ($req.HttpMethod -eq "POST" -and $route -eq "/extract-excel-display-upload") {
                Handle-ExtractExcelDisplayUpload $req $resp
                continue
            }
            Write-JsonResponse $resp 404 @{ error = "not found" }
        } catch {
            try { Write-JsonResponse $ctx.Response 500 @{ error = $_.Exception.Message } } catch {}
        }
    }
} finally {
    $listener.Stop()
    $listener.Close()
}
