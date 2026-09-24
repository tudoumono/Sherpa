#!/usr/bin/env python3
"""S0: 構文分布スクリプト（docs/proposals/2026-09-05-アナライザ拡張.md §9・A5）。

実コーパス（閉域・実環境）で1回走らせ、COBOL/JCL/Java/設定ファイル/SQL/C/C#の構文分布を集計する。
S1〜S7（同提案 §9 スライス表）の着手順を決める根拠を出すためだけのツール——**判定・順序付けは
一切行わない**（集計のみ）。読み取り専用・LLM 不使用・world（台帳/ES/Neo4j/DB）への書込み一切なし
（ファイルシステムのソースを読むだけ）。標準ライブラリのみで動く（`.venv` 不要）。

使い方:
    python3 scripts/syntax_profile.py <dir> [<dir> ...]           # 人が読む表
    python3 scripts/syntax_profile.py <dir> --json                # JSON

設計メモ（判断が必要だった点はここに書く。実装の詳細はコードのコメントを見よ）:

* **エンコーディング判定**: ファイル全体を bytes で読み、UTF-8→失敗時 CP932→失敗時 latin-1 の順で
  丸ごと decode を試す（latin-1 は任意バイト列を必ず decode できるため最終フォールバック）。
  8MiB 超のファイルは decode を試みず skip 数に計上する（`MAX_FILE_BYTES`）。
* **「ストリーミング」の解釈**: 1ファイルは高々 8MiB（`MAX_FILE_BYTES`）に収まる decode 済み文字列
  として一度だけメモリに載せ、その文字列に対して行走査・正規表現走査を行う。複数ファイル分の内容や
  行リストを同時に保持することはない（ファイルを1本処理し終えたら即座に破棄して次へ進む）ため、
  数万ファイル規模でも常駐メモリは高々「1ファイル分」に収まる。
* **COBOL 固定/自由形式判定**: `sherpa/ingest/static_analysis.py::_is_free_format` と同じ判定式
  （`>>SOURCE FORMAT IS FREE` 指示文がファイル中に現れれば自由形式・無ければ固定形式）を、
  そのモジュールをインポートせず本ファイル内に再実装する（`.venv` 不要の縛りを守るため——
  実際には `static_analysis.py` は標準ライブラリのみで import 可能だが、本ツールは意図的に
  `sherpa` パッケージへの依存を持たない完全独立ツールとして作る）。
* **COBOL 採番領域の除去（delevel）**: 行の1〜6桁が数字なら、その6桁＋7桁目（indicator）を
  取り除いてから COPY/CALL/レベル項目の判定を行う（採番の有無に関わらず同じ判定式が効くように
  するため）。採番あり率・継続行の集計自体は元の生の行に対して行う（採番の実際の出現率を見たい
  ため、delevel 後ではなく生の行を見る）。
* **EXEC SQL/EXEC CICS ブロック**: `EXEC SQL ... END-EXEC` / `EXEC CICS ... END-EXEC` は複数行に
  またがるため、行単位ではなくファイル全文に対する `re.DOTALL` の正規表現で一括抽出する
  （1ファイル分の文字列は既にメモリにあるため追加のコストはない）。
* **YAML の読み方**: 本物の YAML パーサ（PyYAML 等）は標準ライブラリに無いため、`key: value` 形式の
  行を正規表現で拾う簡易パーサ（ネスト・複数ドキュメント・ブロックスカラー等は非対応）。分布の
  目安を得る目的には十分という判断。
* **JCL PROC 実体解決**: PROC 名 Top20 それぞれについて、走査済み全ファイルのファイル名ステム
  （拡張子を除いた部分・大小無視）と一致するものが world 内に存在するかを見る（1回の走査中に
  全ファイルのステム集合を作っておき、Top20 確定後に集合参照で判定する）。

設計 RV 指摘（2026-09-05・追加3項目）:

* **SQL: CREATE TABLE 件数の分布＋DML専用ファイル**: 1ファイルあたりの `CREATE TABLE` 出現数を
  数え、1件＝`single`／2件以上＝`multiple` に分類する（0件のファイルはどちらにも数えない）。
  「DDL（CREATE TABLE/VIEW/PROCEDURE/FUNCTION・ALTER TABLE のいずれか）を1つも含まず、DML
  （SELECT/INSERT INTO/UPDATE/DELETE FROM のいずれか）だけを含む」ファイル数も別途数える
  （EXEC SQL 埋め込みでなく `.sql` ファイル単体としてテーブル定義を持たない＝データ投入/参照
  専用スクリプトの比率を見るための目安）。
* **XML: 設定 XML と非設定 XML の別**: ルート要素名が `beans`/`mapper`/`struts`/`web-app`
  （大小無視）なら「設定 XML」、それ以外（ルートが取れなかったものを含む）なら「非設定 XML」に
  数える。ルート要素 Top20 とは別に、まず「設定/非設定のどちらが多いか」を見るための粗い二分。
* **COBOL: 継続行が COPY/CALL/EXEC をまたぐ件数**: 継続行（7桁目 `-`）ごとに、直前の物理行または
  継続行自身に `COPY`/`CALL`/`EXEC SQL`/`EXEC CICS` のいずれかが現れるかを見る（粗い判定でよいという
  指示どおり——本物の論理行結合はしない・「直前行 or 継続行自身にキーワードが見える」というヒューリ
  スティックのみ）。S1（論理行正規化）が実際にどれだけ必要かの目安（この件数が多いほど、現行の
  行単位マッチが継続行をまたぐ COPY/CALL/EXEC 文を取りこぼしている可能性が高い）。

追加指標（コード内設定キー参照・S3' 着手判断用・2026-09-05）:

* **キー集合の作り方**: properties は `key=value` の `key` をそのまま、YAML はインデントに応じた
  親キーのスタックを積み、`.` 連結の完全キー（例 `tax.rate`）を作る（両方とも既存の
  `_process_properties`/`_process_yaml` が読んでいる行から同時に集める・追加の読み込みはしない）。
* **2パス化の理由**: Java/C# の文字列リテラルをキー集合と突き合わせる（辞書突合ベース）には
  キー集合が完成している必要がある。そのためファイル一覧の収集は1回のまま、処理順序だけを
  「properties/yaml を全部処理 → 残りのファイル（Java/C# 含む）」の2パスに変える
  （`scan()` の `config_entries`/`code_entries` 分割）。ファイルの読み込みは変わらず1ファイル1回。
* **構文ベース vs 辞書突合ベース**: 構文ベースは `getProperty(...)`/`@Value("${...}")`/
  `getString(...)`/`ConfigurationManager.AppSettings[...]`/`Configuration[...]`・`config[...]`/
  `GetValue<T>(...)` の形に一致した文字列リテラルだけを見る（キー集合との突合はしない＝構文だけで
  「設定キー参照らしき呼び出し」を検出）。辞書突合ベースはそれとは独立に、ファイル中の**すべての**
  文字列リテラルをキー集合と完全一致で突き合わせる（構文ベースで見つかったものを除外しない＝
  重複してよい・「構文の外で見つかった分も含む」という指示どおり）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# 対象拡張子（registry.py の登録済み分＋提案書 §4 の追加候補。大小無視で扱う）
# ---------------------------------------------------------------------------
COBOL_EXT = {".cbl", ".cob", ".cobol"}
COPYBOOK_EXT = {".cpy", ".copybook"}
JCL_EXT = {".jcl"}
JAVA_EXT = {".java"}
C_EXT = {".c", ".h"}
CPP_EXT = {".cpp", ".hpp"}
CS_EXT = {".cs"}
SQL_EXT = {".sql"}
PROPERTIES_EXT = {".properties"}
YAML_EXT = {".yaml", ".yml"}
XML_EXT = {".xml"}

ALL_EXT = (COBOL_EXT | COPYBOOK_EXT | JCL_EXT | JAVA_EXT | C_EXT | CPP_EXT
           | CS_EXT | SQL_EXT | PROPERTIES_EXT | YAML_EXT | XML_EXT)

# ext -> 集計グループ名
_GROUP_OF_EXT: dict = {}
for _e in COBOL_EXT | COPYBOOK_EXT:
    _GROUP_OF_EXT[_e] = "cobol"
for _e in JCL_EXT:
    _GROUP_OF_EXT[_e] = "jcl"
for _e in JAVA_EXT:
    _GROUP_OF_EXT[_e] = "java"
for _e in C_EXT | CPP_EXT:
    _GROUP_OF_EXT[_e] = "c_cpp"
for _e in CS_EXT:
    _GROUP_OF_EXT[_e] = "csharp"
for _e in SQL_EXT:
    _GROUP_OF_EXT[_e] = "sql"
for _e in PROPERTIES_EXT:
    _GROUP_OF_EXT[_e] = "properties"
for _e in YAML_EXT:
    _GROUP_OF_EXT[_e] = "yaml"
for _e in XML_EXT:
    _GROUP_OF_EXT[_e] = "xml"

MAX_FILE_BYTES = 8 * 1024 * 1024  # 8 MiB
PROGRESS_EVERY = 1000
TOP_N = 20

# ---------------------------------------------------------------------------
# COBOL/JCL 正規表現（sherpa/ingest/static_analysis.py と同じ判定式を再実装・コピーはしない）
# ---------------------------------------------------------------------------
_SOURCE_FORMAT_DIRECTIVE = re.compile(r">>SOURCE\s+(?:FORMAT\s+)?(FREE|FIXED)\b", re.I)
_PROGRAM_ID = re.compile(r"PROGRAM-ID\s*\.\s*([A-Z0-9#@$-]+)", re.I)
_COPY = re.compile(r"(?<![A-Z0-9#@$-])COPY\s+([A-Z0-9#@$-]+)", re.I)
_CALL_LITERAL = re.compile(r"(?<![A-Z0-9#@$-])CALL\s+['\"]([^'\"]+)['\"]", re.I)
_DYNAMIC_CALL = re.compile(r"(?<![A-Z0-9#@$-])CALL\s+([A-Z][A-Z0-9#@$-]*)\b", re.I)
_LEVEL_ITEM = re.compile(r"^\s*(\d{2})\s+([A-Za-z0-9#@$_-]+)")
_EXEC_SQL_BLOCK = re.compile(r"EXEC\s+SQL(.*?)END-EXEC", re.I | re.S)
_EXEC_CICS_BLOCK = re.compile(r"EXEC\s+CICS\s+(\S+)(.*?)END-EXEC", re.I | re.S)
_SQL_TABLE_REF = re.compile(
    r"\b(FROM|INTO|UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+([A-Za-z_][A-Za-z0-9_.$#@-]*)", re.I)
_JCL_EXEC = re.compile(r"^//\S+\s+EXEC\s+(\S+)", re.I)
_JCL_INCLUDE = re.compile(r"^//\S*\s*INCLUDE\s+MEMBER\s*=\s*(\S+)", re.I)
# 継続行が COPY/CALL/EXEC をまたぐかの粗い判定用（設計RV指摘・本物の論理行結合はしない）。
_CONT_SPAN_KEYWORD = re.compile(r"\bCOPY\b|\bCALL\b|\bEXEC\s+SQL\b|\bEXEC\s+CICS\b", re.I)

# ---------------------------------------------------------------------------
# Java 正規表現
# ---------------------------------------------------------------------------
_JAVA_ANNOTATIONS = ("Component", "Service", "Repository", "Controller", "RestController",
                      "Autowired", "Inject", "Resource", "Transactional")
_JAVA_ANNOTATION_RE = {a: re.compile(r"@" + a + r"\b") for a in _JAVA_ANNOTATIONS}
_MYBATIS_ANNOTATIONS = ("Mapper", "Select", "Insert", "Update", "Delete")
_MYBATIS_ANNOTATION_RE = {a: re.compile(r"@" + a + r"\b") for a in _MYBATIS_ANNOTATIONS}
_JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.*]+)\s*;", re.M)
_CLASS_FOR_NAME = re.compile(r"\bClass\s*\.\s*forName\s*\(")
_GET_BEAN = re.compile(r"\bgetBean\s*\(")

# ---------------------------------------------------------------------------
# XML 正規表現
# ---------------------------------------------------------------------------
_XML_ROOT = re.compile(r"<([A-Za-z_][\w:.-]*)")
_XML_DECL_OR_COMMENT_OR_DOCTYPE = re.compile(r"^\s*(<\?xml|<!--|<!DOCTYPE)", re.I)
_XML_CLASS_ATTR = re.compile(r'\bclass\s*=\s*"([^"]+)"|\bclass\s*=\s*\'([^\']+)\'')
_XML_NAMESPACE_ATTR = re.compile(r'\bnamespace\s*=\s*"([^"]*)"|\bnamespace\s*=\s*\'([^\']*)\'')
_XML_ACTION_ELEM = re.compile(r"<action\b", re.I)
_STRUTS_XML_NAME = re.compile(r"^struts(-.*)?\.xml$", re.I)
_MAPPER_XML_NAME = re.compile(r"Mapper\.xml$", re.I)
# 「設定 XML」とみなすルート要素名（大小無視・設計RV指摘）。この集合に無いルートは「非設定 XML」。
_CONFIG_XML_ROOTS = {"beans", "mapper", "struts", "web-app"}

# ---------------------------------------------------------------------------
# properties/yaml 正規表現
# ---------------------------------------------------------------------------
_PROPERTIES_KV = re.compile(r"^\s*([^#!=:\s][^=:]*?)\s*[:=]\s*(.*)$")
_YAML_KV = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)\s*:\s*(.*)$")
_FQCN_LIKE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.$-]*$")
_PLACEHOLDER = re.compile(r"\$\{[^}]*\}")

# ---------------------------------------------------------------------------
# SQL 正規表現
# ---------------------------------------------------------------------------
_CREATE_TABLE = re.compile(r"\bCREATE\s+TABLE\s+([A-Za-z_][\w.$#-]*)", re.I)
_CREATE_VIEW = re.compile(r"\bCREATE\s+VIEW\s+([A-Za-z_][\w.$#-]*)", re.I)
_CREATE_PROC_FUNC = re.compile(r"\bCREATE\s+(?:PROCEDURE|FUNCTION)\s+([A-Za-z_][\w.$#-]*)", re.I)
_ALTER_TABLE = re.compile(r"\bALTER\s+TABLE\s+([A-Za-z_][\w.$#-]*)", re.I)
_SQL_DML = re.compile(r"\bSELECT\b|\bINSERT\s+INTO\b|\bUPDATE\b|\bDELETE\s+FROM\b", re.I)

# ---------------------------------------------------------------------------
# C/C++ 正規表現（粗さは意図的・提案書 §4(a) の粗い判定でよいという指示どおり）
# ---------------------------------------------------------------------------
_C_INCLUDE_LOCAL = re.compile(r'^\s*#\s*include\s*"([^"]+)"')
_C_INCLUDE_EXTERNAL = re.compile(r"^\s*#\s*include\s*<([^>]+)>")
# 粗いトップレベル関数定義判定（提案書の式そのまま）: 戻り値型＋関数名＋引数リストで終わる行。
_C_FUNC_DEF = re.compile(r"^[A-Za-z_][\w\s*]*\s+\**\s*[A-Za-z_]\w*\s*\([^;]*\)\s*\{?\s*$")
_C_FUNC_PTR_CALL = re.compile(r"\(\*\w+\)\s*\(")

# ---------------------------------------------------------------------------
# C# 正規表現
# ---------------------------------------------------------------------------
_CS_TYPE_DECL = re.compile(r"\b(class|interface|struct|record|enum)\s+[A-Za-z_]\w*")
_CS_INHERITANCE = re.compile(
    r"\b(?:class|interface|struct|record)\s+[A-Za-z_]\w*(?:<[^>]*>)?\s*:\s*[A-Za-z_]")
_CS_ASPNET_ATTR = re.compile(r"\[\s*(Route|HttpGet|HttpPost|HttpPut|HttpDelete|HttpPatch)\b", re.I)
_CS_DBSET = re.compile(r"\bDbSet\s*<")

# ---------------------------------------------------------------------------
# コード内設定キー参照（Java/C# の文字列リテラル・構文ベース＋辞書突合ベース）
# ---------------------------------------------------------------------------
# 任意の文字列リテラル（バックスラッシュエスケープ対応・粗い判定でよい――C# 逐語的文字列 @"..." の
# `""` エスケープは非対応・提案書の粗さの方針を踏襲）。辞書突合ベースはこれで拾った全リテラルを
# キー集合と完全一致で照合する。
_STRING_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')
_CFG_GETPROPERTY = re.compile(r'\bgetProperty\s*\(\s*"((?:[^"\\]|\\.)*)"')  # env.getProperty(...) も含む
_CFG_VALUE_ANN = re.compile(r'@Value\s*\(\s*"\$\{\s*([^}:"]+?)\s*(?::[^}"]*)?\}"')  # 既定値部分は捨てる
_CFG_GETSTRING = re.compile(r'\bgetString\s*\(\s*"((?:[^"\\]|\\.)*)"')
_CFG_APPSETTINGS = re.compile(
    r'\bConfigurationManager\.AppSettings\s*\[\s*"((?:[^"\\]|\\.)*)"\s*\]')
_CFG_CONFIG_IDX = re.compile(r'\b(?:Configuration|config)\s*\[\s*"((?:[^"\\]|\\.)*)"\s*\]')
_CFG_GETVALUE = re.compile(r'\bGetValue\s*<[^>]*>\s*\(\s*"((?:[^"\\]|\\.)*)"')
_CFG_SYNTAX_PATTERNS = (
    _CFG_GETPROPERTY, _CFG_VALUE_ANN, _CFG_GETSTRING,
    _CFG_APPSETTINGS, _CFG_CONFIG_IDX, _CFG_GETVALUE,
)


def _read_text(data: bytes) -> tuple:
    """bytes を UTF-8→CP932→latin-1 の順で decode する（latin-1 は必ず成功する最終フォールバック）。"""
    for enc in ("utf-8", "cp932"):
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


def _is_comment(line: str) -> bool:
    """COBOL/JCL のコメント行（固定形式 桁7の `*`/`/`、行頭 `*`、JCL `//*`）。static_analysis と同じ式。"""
    s = line.lstrip()
    return s.startswith("*") or s.startswith("//*") or (len(line) > 6 and line[6] in "*/")


def _is_free_format(text: str) -> bool:
    """ファイル中で最初に現れる `>>SOURCE FORMAT` 指示文で判定（`_is_free_format` と同じ判定式）。"""
    m = _SOURCE_FORMAT_DIRECTIVE.search(text)
    return bool(m) and m.group(1).upper() == "FREE"


def _delevel(line: str) -> str:
    """行頭1〜6桁が数字（採番領域）なら、その6桁＋7桁目（indicator）を取り除く。"""
    if len(line) >= 7 and line[:6].isdigit():
        return line[7:]
    if len(line) == 6 and line[:6].isdigit():
        return ""
    return line


def _strip_inline_comment(line: str) -> str:
    idx = line.find("*>")
    return line[:idx] if idx >= 0 else line


def _strip_quoted(s: str) -> str:
    """引用文字列（'...'/"..."）の中身を空白に置換する（雑な簡易版・複数行にまたがる引用は非対応）。"""
    return re.sub(r"'[^']*'|\"[^\"]*\"", lambda m: " " * len(m.group(0)), s)


class Metric:
    """{files, lines} の集計単位（このファイルで少なくとも1回出現＝files+1・出現行数＝lines）。"""

    __slots__ = ("files", "lines")

    def __init__(self):
        self.files = 0
        self.lines = 0

    def to_dict(self) -> dict:
        return {"files": self.files, "lines": self.lines}


class MetricSet(defaultdict):
    """キーごとの `Metric`。`hit()` で行ヒットを記録し、`close_file()` でファイル単位を確定する。"""

    def __init__(self):
        super().__init__(Metric)

    def hit(self, key: str, file_hits: set) -> None:
        self[key].lines += 1
        file_hits.add(key)

    def close_file(self, file_hits: set) -> None:
        for key in file_hits:
            self[key].files += 1

    def to_dict(self) -> dict:
        return {k: v.to_dict() for k, v in self.items()}


class Stats:
    """全集計状態。1回の `scan()` の間だけ生存する。"""

    def __init__(self):
        self.files_scanned = 0
        self.skipped_too_large = 0
        self.skipped_unreadable = 0
        self.encoding_counts: Counter = Counter()
        self.by_ext: MetricSet = MetricSet()
        self.by_gen: dict = defaultdict(MetricSet)
        self.all_stems: set = set()

        # cobol
        self.cobol_files_total = 0
        self.cobol_files_fixed = 0
        self.cobol_files_free = 0
        self.cobol_lines_total = 0
        self.cobol_lines_numbered = 0
        self.cobol_continuation_lines = 0
        self.cobol_counts: MetricSet = MetricSet()  # copy / call_literal / call_dynamic / level_item
        self.cobol_exec_sql_tables: Counter = Counter()
        self.cobol_exec_sql_refkw: Counter = Counter()
        self.cobol_exec_cics_kind: Counter = Counter()  # xctl/link/other

        # jcl
        self.jcl_files_total = 0
        self.jcl_counts: MetricSet = MetricSet()  # exec_pgm / exec_proc_or_named / include_member
        self.jcl_proc_names: Counter = Counter()

        # java
        self.java_files_total = 0
        self.java_counts: MetricSet = MetricSet()  # per annotation + mybatis + terasoluna_import etc
        self.java_mapper_xml_files = 0
        self.java_struts_xml_files = 0
        self.java_struts_action_elements = 0
        self.java_terasoluna_xml_files = 0

        # xml
        self.xml_files_total = 0
        self.xml_root_elements: Counter = Counter()
        self.xml_class_attr_fqcn = Metric()
        self.xml_namespace_attr = Metric()
        self.xml_config_root_files = 0
        self.xml_non_config_root_files = 0

        # properties / yaml
        self.properties_files = 0
        self.properties_keys = 0
        self.properties_fqcn_values = 0
        self.properties_placeholder_values = 0
        self.yaml_files = 0
        self.yaml_keys = 0
        self.yaml_fqcn_values = 0
        self.yaml_placeholder_values = 0

        # sql
        self.sql_files_total = 0
        self.sql_counts: MetricSet = MetricSet()  # create_table/create_view/create_proc_func/alter_table
        self.sql_tables: Counter = Counter()
        self.sql_create_table_per_file_single = 0   # CREATE TABLE がちょうど1件のファイル数
        self.sql_create_table_per_file_multi = 0    # CREATE TABLE が2件以上のファイル数
        self.sql_dml_only_files = 0                 # DDL 皆無・DML のみのファイル数

        # c/c++
        self.c_cpp_files_total = 0
        self.c_cpp_counts: MetricSet = MetricSet()  # include_local/include_external/func_def/func_ptr_call

        # c#
        self.cs_files_total = 0
        self.cs_type_decl: Counter = Counter()  # class/interface/struct/record/enum
        self.cs_counts: MetricSet = MetricSet()  # inheritance/aspnet_attr/dbset

        # コード内設定キー参照（properties/yaml から作るキー集合＋Java/C# 側の突合）
        self.config_keys: set = set()
        self.cfg_syntax_metric = Metric()
        self.cfg_syntax_keys: Counter = Counter()
        self.cfg_dict_metric = Metric()
        self.cfg_dict_keys: Counter = Counter()


def _generation_key(root: Path, dirpath: str) -> str:
    """`<dir>` 直下1階層のフォルダ名（世代キー）。直下ファイル自体は `(root)` とする。"""
    rel = Path(dirpath).relative_to(root)
    parts = rel.parts
    return parts[0] if parts else "(root)"


def _iter_files(roots: list) -> "iter":
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # symlink のディレクトリは辿らない（os.walk(followlinks=False) は展開しないが、
            # 念のため dirnames からも明示的に除外して二重に防ぐ）。
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            gen = _generation_key(root, dirpath)
            for name in filenames:
                full = Path(dirpath) / name
                if full.is_symlink():
                    continue
                yield root, gen, full


def _process_cobol(text: str, stats: Stats) -> None:
    stats.cobol_files_total += 1
    free_format = _is_free_format(text)
    if free_format:
        stats.cobol_files_free += 1
    else:
        stats.cobol_files_fixed += 1

    file_hits: set = set()
    lines = text.splitlines()
    stats.cobol_lines_total += len(lines)
    for i, raw_line in enumerate(lines):
        # 採番あり率・継続行は「全行」が母数（コメント行も含む・採番領域はコメント/コードを問わず
        # 物理行すべてに付く）。COPY/CALL/レベル項目の判定だけをコメント行の対象外にする。
        if len(raw_line) >= 6 and raw_line[:6].isdigit():
            stats.cobol_lines_numbered += 1
        if len(raw_line) > 6 and raw_line[6] == "-":
            stats.cobol_continuation_lines += 1
            # 継続行が COPY/CALL/EXEC をまたぐか（粗い判定・設計RV指摘）: 直前の物理行か継続行
            # 自身のどちらかにキーワードが見えれば「またぐ」とみなす（本物の論理行結合はしない）。
            prev_raw_line = lines[i - 1] if i > 0 else ""
            if _CONT_SPAN_KEYWORD.search(prev_raw_line) or _CONT_SPAN_KEYWORD.search(raw_line):
                stats.cobol_counts.hit("continuation_spans_copy_call_exec", file_hits)
        if _is_comment(raw_line):
            continue

        code = _delevel(raw_line)
        code = _strip_inline_comment(code)

        for _m in _COPY.finditer(_strip_quoted(code)):
            stats.cobol_counts.hit("copy", file_hits)
        for _m in _CALL_LITERAL.finditer(code):
            stats.cobol_counts.hit("call_literal", file_hits)
        for _m in _DYNAMIC_CALL.finditer(_strip_quoted(code)):
            stats.cobol_counts.hit("call_dynamic", file_hits)
        if _LEVEL_ITEM.match(code):
            stats.cobol_counts.hit("level_item", file_hits)

    # EXEC SQL / EXEC CICS は複数行ブロックのためファイル全文へ DOTALL 正規表現を掛ける。
    for m in _EXEC_SQL_BLOCK.finditer(text):
        stats.cobol_counts.hit("exec_sql", file_hits)
        body = m.group(1)
        for kw, name in _SQL_TABLE_REF.findall(body):
            kw_norm = re.sub(r"\s+", "_", kw.strip().upper())
            stats.cobol_exec_sql_refkw[kw_norm] += 1
            stats.cobol_exec_sql_tables[name.upper()] += 1
    for m in _EXEC_CICS_BLOCK.finditer(text):
        stats.cobol_counts.hit("exec_cics", file_hits)
        cmd = m.group(1).upper()
        if cmd == "XCTL":
            stats.cobol_exec_cics_kind["xctl"] += 1
        elif cmd == "LINK":
            stats.cobol_exec_cics_kind["link"] += 1
        else:
            stats.cobol_exec_cics_kind["other"] += 1

    stats.cobol_counts.close_file(file_hits)


def _process_jcl(text: str, stats: Stats) -> None:
    stats.jcl_files_total += 1
    file_hits: set = set()
    for raw_line in text.splitlines():
        m = _JCL_EXEC.match(raw_line)
        if m:
            target = m.group(1).split(",", 1)[0]
            if target.upper().startswith("PGM="):
                stats.jcl_counts.hit("exec_pgm", file_hits)
            else:
                stats.jcl_counts.hit("exec_proc_or_named", file_hits)
                name = target.split("=", 1)[1] if "=" in target else target
                if name:
                    stats.jcl_proc_names[name.upper()] += 1
        if _JCL_INCLUDE.match(raw_line):
            stats.jcl_counts.hit("include_member", file_hits)
    stats.jcl_counts.close_file(file_hits)


def _process_config_key_refs(text: str, stats: Stats) -> None:
    """Java/C# ソースのコード内設定キー参照（構文ベース＋辞書突合ベース）。

    properties/yaml のキー集合（`stats.config_keys`）が完成済みであることが前提
    （`scan()` の2パス化＝properties/yaml を全部処理してから呼ばれる）。
    """
    file_hit_syntax = False
    for pat in _CFG_SYNTAX_PATTERNS:
        for m in pat.finditer(text):
            key = m.group(1).strip()
            if not key:
                continue
            stats.cfg_syntax_metric.lines += 1
            stats.cfg_syntax_keys[key] += 1
            file_hit_syntax = True
    if file_hit_syntax:
        stats.cfg_syntax_metric.files += 1

    if not stats.config_keys:
        return  # 計測不能（properties/yaml が1件も無い）
    file_hit_dict = False
    for m in _STRING_LITERAL.finditer(text):
        literal = m.group(1)
        if literal in stats.config_keys:
            stats.cfg_dict_metric.lines += 1
            stats.cfg_dict_keys[literal] += 1
            file_hit_dict = True
    if file_hit_dict:
        stats.cfg_dict_metric.files += 1


def _process_java(text: str, stats: Stats) -> None:
    stats.java_files_total += 1
    file_hits: set = set()
    lines = text.splitlines()
    for raw_line in lines:
        for name, rx in _JAVA_ANNOTATION_RE.items():
            if rx.search(raw_line):
                stats.java_counts.hit(name, file_hits)
        for name, rx in _MYBATIS_ANNOTATION_RE.items():
            if rx.search(raw_line):
                stats.java_counts.hit("mybatis_" + name, file_hits)
        if _CLASS_FOR_NAME.search(raw_line):
            stats.java_counts.hit("class_for_name", file_hits)
        if _GET_BEAN.search(raw_line):
            stats.java_counts.hit("get_bean", file_hits)
    for imp in _JAVA_IMPORT.finditer(text):
        name = imp.group(1)
        if name.startswith("org.terasoluna."):
            stats.java_counts.hit("terasoluna_import", file_hits)
        if name.startswith("org.apache.struts"):
            stats.java_counts.hit("struts_import", file_hits)
    stats.java_counts.close_file(file_hits)
    _process_config_key_refs(text, stats)


def _process_xml(filename: str, text: str, stats: Stats) -> None:
    stats.xml_files_total += 1
    lines = text.splitlines()
    root_found = False
    file_hits_class = False
    file_hits_ns = False
    has_terasoluna_ns = False
    is_struts_file = bool(_STRUTS_XML_NAME.match(filename))
    is_mapper_file = bool(_MAPPER_XML_NAME.search(filename))
    if is_mapper_file:
        stats.java_mapper_xml_files += 1
    if is_struts_file:
        stats.java_struts_xml_files += 1

    for raw_line in lines:
        if not root_found and not _XML_DECL_OR_COMMENT_OR_DOCTYPE.match(raw_line):
            m = _XML_ROOT.search(raw_line)
            if m:
                stats.xml_root_elements[m.group(1)] += 1
                root_found = True
                # 設定 XML（beans/mapper/struts/web-app ルート）と非設定 XML の別（設計RV指摘）。
                if m.group(1).lower() in _CONFIG_XML_ROOTS:
                    stats.xml_config_root_files += 1
                else:
                    stats.xml_non_config_root_files += 1
        for m in _XML_CLASS_ATTR.finditer(raw_line):
            val = m.group(1) or m.group(2) or ""
            if "." in val:
                stats.xml_class_attr_fqcn.lines += 1
                file_hits_class = True
        for m in _XML_NAMESPACE_ATTR.finditer(raw_line):
            stats.xml_namespace_attr.lines += 1
            file_hits_ns = True
        if is_struts_file and _XML_ACTION_ELEM.search(raw_line):
            stats.java_struts_action_elements += 1
        if not has_terasoluna_ns and "terasoluna" in raw_line.lower():
            has_terasoluna_ns = True
    if has_terasoluna_ns:
        stats.java_terasoluna_xml_files += 1
    if file_hits_class:
        stats.xml_class_attr_fqcn.files += 1
    if file_hits_ns:
        stats.xml_namespace_attr.files += 1


def _process_properties(text: str, stats: Stats) -> None:
    stats.properties_files += 1
    for raw_line in text.splitlines():
        m = _PROPERTIES_KV.match(raw_line)
        if not m:
            continue
        stats.properties_keys += 1
        key = m.group(1).strip()
        stats.config_keys.add(key)  # コード内設定キー参照の突合用キー集合
        value = m.group(2).strip()
        if _FQCN_LIKE.match(value) and "." in value:
            stats.properties_fqcn_values += 1
        if _PLACEHOLDER.search(value):
            stats.properties_placeholder_values += 1


def _process_yaml(text: str, stats: Stats) -> None:
    stats.yaml_files += 1
    # インデントに応じた親キーのスタック（コード内設定キー参照の完全キー組み立て用・このファイル限定）。
    key_stack: list = []  # [(indent, key), ...]
    for raw_line in text.splitlines():
        s = raw_line.strip()
        if not s or s.startswith("#") or s.startswith("- "):
            continue
        m = _YAML_KV.match(raw_line)
        if not m:
            continue
        stats.yaml_keys += 1
        indent = len(m.group(1))
        while key_stack and key_stack[-1][0] >= indent:
            key_stack.pop()
        key_stack.append((indent, m.group(2)))
        stats.config_keys.add(".".join(k for _, k in key_stack))
        value = m.group(3).strip().strip("'\"")
        if not value:
            continue
        if _FQCN_LIKE.match(value) and "." in value:
            stats.yaml_fqcn_values += 1
        if _PLACEHOLDER.search(value):
            stats.yaml_placeholder_values += 1


def _process_sql(text: str, stats: Stats) -> None:
    stats.sql_files_total += 1
    file_hits: set = set()
    create_table_count = 0  # このファイル内の CREATE TABLE 出現数（単一/複数の分布用）
    any_ddl = False
    any_dml = False
    lines = text.splitlines()
    for raw_line in lines:
        for m in _CREATE_TABLE.finditer(raw_line):
            stats.sql_counts.hit("create_table", file_hits)
            stats.sql_tables[m.group(1).upper()] += 1
            create_table_count += 1
            any_ddl = True
        if _CREATE_VIEW.search(raw_line):
            stats.sql_counts.hit("create_view", file_hits)
            any_ddl = True
        if _CREATE_PROC_FUNC.search(raw_line):
            stats.sql_counts.hit("create_procedure_or_function", file_hits)
            any_ddl = True
        m = _ALTER_TABLE.search(raw_line)
        if m:
            stats.sql_counts.hit("alter_table", file_hits)
            stats.sql_tables[m.group(1).upper()] += 1
            any_ddl = True
        if _SQL_DML.search(raw_line):
            any_dml = True
    stats.sql_counts.close_file(file_hits)

    # CREATE TABLE 件数の分布（設計RV指摘）。0件のファイルはどちらにも数えない。
    if create_table_count == 1:
        stats.sql_create_table_per_file_single += 1
    elif create_table_count >= 2:
        stats.sql_create_table_per_file_multi += 1
    # DDL 皆無・DML のみのファイル（設計RV指摘）。
    if not any_ddl and any_dml:
        stats.sql_dml_only_files += 1


def _process_c_cpp(text: str, stats: Stats) -> None:
    stats.c_cpp_files_total += 1
    file_hits: set = set()
    for raw_line in text.splitlines():
        if _C_INCLUDE_LOCAL.match(raw_line):
            stats.c_cpp_counts.hit("include_local", file_hits)
        elif _C_INCLUDE_EXTERNAL.match(raw_line):
            stats.c_cpp_counts.hit("include_external", file_hits)
        if _C_FUNC_DEF.match(raw_line):
            stats.c_cpp_counts.hit("function_def_lines", file_hits)
        if _C_FUNC_PTR_CALL.search(raw_line):
            stats.c_cpp_counts.hit("function_pointer_call_lines", file_hits)
    stats.c_cpp_counts.close_file(file_hits)


def _process_csharp(text: str, stats: Stats) -> None:
    stats.cs_files_total += 1
    file_hits: set = set()
    for raw_line in text.splitlines():
        for m in _CS_TYPE_DECL.finditer(raw_line):
            stats.cs_type_decl[m.group(1)] += 1
        if _CS_INHERITANCE.search(raw_line):
            stats.cs_counts.hit("inheritance_lines", file_hits)
        if _CS_ASPNET_ATTR.search(raw_line):
            stats.cs_counts.hit("aspnet_attributes", file_hits)
        if _CS_DBSET.search(raw_line):
            stats.cs_counts.hit("dbset_usages", file_hits)
    stats.cs_counts.close_file(file_hits)
    _process_config_key_refs(text, stats)


_PROCESSORS = {
    "cobol": _process_cobol,
    "jcl": _process_jcl,
    "java": _process_java,
    "sql": _process_sql,
    "c_cpp": _process_c_cpp,
    "csharp": _process_csharp,
}


def _process_one_file(gen: str, full: Path, ext: str, stats: Stats) -> None:
    stats.files_scanned += 1
    if stats.files_scanned % PROGRESS_EVERY == 0:
        print(f"...{stats.files_scanned} files processed...", file=sys.stderr, flush=True)
    try:
        size = full.stat().st_size
    except OSError:
        stats.skipped_unreadable += 1
        return
    if size > MAX_FILE_BYTES:
        stats.skipped_too_large += 1
        return
    try:
        data = full.read_bytes()
    except OSError:
        stats.skipped_unreadable += 1
        return
    text, enc = _read_text(data)
    stats.encoding_counts[enc] += 1
    nlines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)

    group = _GROUP_OF_EXT[ext]
    if group == "xml":
        _process_xml(full.name, text, stats)
    elif group == "properties":
        _process_properties(text, stats)
    elif group == "yaml":
        _process_yaml(text, stats)
    else:
        _PROCESSORS[group](text, stats)

    stats.by_ext[ext].lines += nlines
    stats.by_ext[ext].files += 1
    gen_metrics = stats.by_gen[gen]
    gen_metrics[ext].lines += nlines
    gen_metrics[ext].files += 1


def scan(roots: list) -> Stats:
    stats = Stats()
    resolved_roots = [Path(r).resolve() for r in roots]

    # ファイル一覧の収集は1回だけ（os.walk 自体は _iter_files 内で1回）。
    entries: list = []
    for root, gen, full in _iter_files(resolved_roots):
        stats.all_stems.add(full.stem.upper())
        ext = full.suffix.lower()
        if ext not in ALL_EXT:
            continue
        entries.append((gen, full, ext))

    # 2パス（設計メモ参照）: properties/yaml を先に全部処理してキー集合を完成させ、
    # その後に残り（Java/C# を含む）を処理する。各ファイルの読み込みは1回のまま
    # （パスをまたいで同じファイルを2度読むことはない）。
    config_entries = [e for e in entries if _GROUP_OF_EXT[e[2]] in ("properties", "yaml")]
    code_entries = [e for e in entries if _GROUP_OF_EXT[e[2]] not in ("properties", "yaml")]
    for gen, full, ext in config_entries:
        _process_one_file(gen, full, ext, stats)
    for gen, full, ext in code_entries:
        _process_one_file(gen, full, ext, stats)
    return stats


def _top_n(counter: Counter, n: int = TOP_N) -> list:
    return [[name, count] for name, count in counter.most_common(n)]


def build_report(stats: Stats) -> dict:
    numbered_ratio_pct = (
        round(100.0 * stats.cobol_lines_numbered / stats.cobol_lines_total, 2)
        if stats.cobol_lines_total else 0.0
    )

    proc_top20 = stats.jcl_proc_names.most_common(TOP_N)
    proc_resolved = sum(1 for name, _cnt in proc_top20 if name.upper() in stats.all_stems)

    cc = stats.cobol_counts.to_dict()
    jc = stats.jcl_counts.to_dict()
    javac = stats.java_counts.to_dict()
    sqlc = stats.sql_counts.to_dict()
    ccpp = stats.c_cpp_counts.to_dict()
    csc = stats.cs_counts.to_dict()

    return {
        "meta": {
            "read_only": True,
            "llm_used": False,
            "world_writes": False,
        },
        "totals": {
            "files_scanned": stats.files_scanned,
            "files_skipped_too_large": stats.skipped_too_large,
            "files_skipped_unreadable": stats.skipped_unreadable,
            "encoding_counts": dict(stats.encoding_counts),
        },
        "by_extension": stats.by_ext.to_dict(),
        "by_generation": {gen: ms.to_dict() for gen, ms in stats.by_gen.items()},
        "cobol": {
            "files_total": stats.cobol_files_total,
            "files_fixed_format": stats.cobol_files_fixed,
            "files_free_format": stats.cobol_files_free,
            "lines_total": stats.cobol_lines_total,
            "lines_numbered": stats.cobol_lines_numbered,
            "numbered_ratio_pct": numbered_ratio_pct,
            "continuation_lines": stats.cobol_continuation_lines,
            "copy": cc.get("copy", {"files": 0, "lines": 0}),
            "call_literal": cc.get("call_literal", {"files": 0, "lines": 0}),
            "call_dynamic": cc.get("call_dynamic", {"files": 0, "lines": 0}),
            "level_item": cc.get("level_item", {"files": 0, "lines": 0}),
            "exec_sql": cc.get("exec_sql", {"files": 0, "lines": 0}),
            "exec_sql_tables_top20": _top_n(stats.cobol_exec_sql_tables),
            "exec_sql_ref_keywords": dict(stats.cobol_exec_sql_refkw),
            "exec_cics": cc.get("exec_cics", {"files": 0, "lines": 0}),
            "exec_cics_kind": dict(stats.cobol_exec_cics_kind),
            "continuation_spans_copy_call_exec": cc.get(
                "continuation_spans_copy_call_exec", {"files": 0, "lines": 0}),
        },
        "jcl": {
            "files_total": stats.jcl_files_total,
            "exec_pgm": jc.get("exec_pgm", {"files": 0, "lines": 0}),
            "exec_proc_or_named": jc.get("exec_proc_or_named", {"files": 0, "lines": 0}),
            "include_member": jc.get("include_member", {"files": 0, "lines": 0}),
            "proc_names_top20": [[name, cnt] for name, cnt in proc_top20],
            "proc_names_top20_resolved_in_world": proc_resolved,
        },
        "java": {
            "files_total": stats.java_files_total,
            "annotations": {a: javac.get(a, {"files": 0, "lines": 0}) for a in _JAVA_ANNOTATIONS},
            "mybatis": {
                "annotations": {a: javac.get("mybatis_" + a, {"files": 0, "lines": 0})
                                for a in _MYBATIS_ANNOTATIONS},
                "mapper_xml_files": stats.java_mapper_xml_files,
            },
            "terasoluna": {
                "import_count": javac.get("terasoluna_import", {"files": 0, "lines": 0}),
                "xml_namespace_files": stats.java_terasoluna_xml_files,
            },
            "struts": {
                "struts_xml_files": stats.java_struts_xml_files,
                "action_elements": stats.java_struts_action_elements,
                "import_count": javac.get("struts_import", {"files": 0, "lines": 0}),
            },
            "reflection": {
                "class_for_name": javac.get("class_for_name", {"files": 0, "lines": 0}),
                "get_bean": javac.get("get_bean", {"files": 0, "lines": 0}),
            },
        },
        "xml": {
            "files_total": stats.xml_files_total,
            "root_elements_top20": _top_n(stats.xml_root_elements),
            "class_attr_fqcn": stats.xml_class_attr_fqcn.to_dict(),
            "namespace_attr": stats.xml_namespace_attr.to_dict(),
            "config_root_files": stats.xml_config_root_files,
            "non_config_root_files": stats.xml_non_config_root_files,
        },
        "properties": {
            "files": stats.properties_files,
            "keys": stats.properties_keys,
            "fqcn_like_values": stats.properties_fqcn_values,
            "placeholder_values": stats.properties_placeholder_values,
        },
        "yaml": {
            "files": stats.yaml_files,
            "keys": stats.yaml_keys,
            "fqcn_like_values": stats.yaml_fqcn_values,
            "placeholder_values": stats.yaml_placeholder_values,
        },
        "sql": {
            "files_total": stats.sql_files_total,
            "create_table": sqlc.get("create_table", {"files": 0, "lines": 0}),
            "create_view": sqlc.get("create_view", {"files": 0, "lines": 0}),
            "create_procedure_or_function": sqlc.get("create_procedure_or_function", {"files": 0, "lines": 0}),
            "alter_table": sqlc.get("alter_table", {"files": 0, "lines": 0}),
            "tables_top20": _top_n(stats.sql_tables),
            "create_table_per_file": {
                "single": stats.sql_create_table_per_file_single,
                "multiple": stats.sql_create_table_per_file_multi,
            },
            "dml_only_files": stats.sql_dml_only_files,
        },
        "c_cpp": {
            "files_total": stats.c_cpp_files_total,
            "include_local": ccpp.get("include_local", {"files": 0, "lines": 0}),
            "include_external": ccpp.get("include_external", {"files": 0, "lines": 0}),
            "function_def_lines": ccpp.get("function_def_lines", {"files": 0, "lines": 0}),
            "function_pointer_call_lines": ccpp.get("function_pointer_call_lines", {"files": 0, "lines": 0}),
        },
        "csharp": {
            "files_total": stats.cs_files_total,
            "type_decls": dict(stats.cs_type_decl),
            "inheritance_lines": csc.get("inheritance_lines", {"files": 0, "lines": 0}),
            "aspnet_attributes": csc.get("aspnet_attributes", {"files": 0, "lines": 0}),
            "dbset_usages": csc.get("dbset_usages", {"files": 0, "lines": 0}),
        },
        "config_key_refs": {
            "keys_total": len(stats.config_keys),
            "syntax": {
                **stats.cfg_syntax_metric.to_dict(),
                "top": _top_n(stats.cfg_syntax_keys),
            },
            "dict_match": (
                {
                    **stats.cfg_dict_metric.to_dict(),
                    "top": _top_n(stats.cfg_dict_keys),
                }
                if stats.config_keys else "計測不能（キー集合なし）"
            ),
        },
    }


def _fmt_metric(m: dict) -> str:
    return f"files={m['files']} lines={m['lines']}"


def print_table(report: dict) -> None:
    print("この集計は読み取り専用・LLM 不使用・world への書込みなし。")
    print()
    t = report["totals"]
    print(f"[全体] 走査対象ファイル数={t['files_scanned']} "
          f"skip(too_large)={t['files_skipped_too_large']} "
          f"skip(unreadable)={t['files_skipped_unreadable']}")
    print(f"  文字コード内訳: {t['encoding_counts']}")
    print()
    print("[拡張子別] files / lines")
    for ext, m in sorted(report["by_extension"].items()):
        print(f"  {ext:12s} {_fmt_metric(m)}")
    print()
    print("[トップフォルダ（世代）別]")
    for gen, exts in sorted(report["by_generation"].items()):
        print(f"  {gen}:")
        for ext, m in sorted(exts.items()):
            print(f"    {ext:12s} {_fmt_metric(m)}")
    print()

    c = report["cobol"]
    print("[COBOL]")
    print(f"  files_total={c['files_total']} fixed={c['files_fixed_format']} free={c['files_free_format']}")
    print(f"  lines_total={c['lines_total']} numbered_ratio={c['numbered_ratio_pct']}% "
          f"continuation_lines={c['continuation_lines']}")
    print(f"  COPY: {_fmt_metric(c['copy'])}")
    print(f"  CALL(literal): {_fmt_metric(c['call_literal'])}  CALL(dynamic): {_fmt_metric(c['call_dynamic'])}")
    print(f"  レベル項目行: {_fmt_metric(c['level_item'])}")
    print(f"  EXEC SQL: {_fmt_metric(c['exec_sql'])}  参照キーワード内訳={c['exec_sql_ref_keywords']}")
    print(f"    テーブル名 Top20: {c['exec_sql_tables_top20']}")
    print(f"  EXEC CICS: {_fmt_metric(c['exec_cics'])}  内訳={c['exec_cics_kind']}")
    print(f"  継続行が COPY/CALL/EXEC をまたぐ件数（粗い判定）: "
          f"{_fmt_metric(c['continuation_spans_copy_call_exec'])}")
    print()

    j = report["jcl"]
    print("[JCL]")
    print(f"  files_total={j['files_total']}")
    print(f"  EXEC PGM=: {_fmt_metric(j['exec_pgm'])}")
    print(f"  EXEC PROC=/EXEC <name>: {_fmt_metric(j['exec_proc_or_named'])}")
    print(f"  INCLUDE MEMBER=: {_fmt_metric(j['include_member'])}")
    print(f"  PROC名 Top20: {j['proc_names_top20']}")
    print(f"  うち world 内に同名ファイルあり: {j['proc_names_top20_resolved_in_world']}")
    print()

    jv = report["java"]
    print("[Java]")
    print(f"  files_total={jv['files_total']}")
    for name, m in jv["annotations"].items():
        print(f"  @{name}: {_fmt_metric(m)}")
    print(f"  MyBatis annotations: {jv['mybatis']['annotations']}  mapper_xml_files={jv['mybatis']['mapper_xml_files']}")
    print(f"  TERASOLUNA: import={_fmt_metric(jv['terasoluna']['import_count'])} "
          f"xml_namespace_files={jv['terasoluna']['xml_namespace_files']}")
    print(f"  Struts: struts_xml_files={jv['struts']['struts_xml_files']} "
          f"action_elements={jv['struts']['action_elements']} import={_fmt_metric(jv['struts']['import_count'])}")
    print(f"  Reflection: Class.forName={_fmt_metric(jv['reflection']['class_for_name'])} "
          f"getBean={_fmt_metric(jv['reflection']['get_bean'])}")
    print()

    x = report["xml"]
    print("[XML]")
    print(f"  files_total={x['files_total']}")
    print(f"  ルート要素 Top20: {x['root_elements_top20']}")
    print(f"  class=属性(FQCN風): {_fmt_metric(x['class_attr_fqcn'])}")
    print(f"  namespace=属性: {_fmt_metric(x['namespace_attr'])}")
    print(f"  設定XML(beans/mapper/struts/web-appルート)={x['config_root_files']} "
          f"非設定XML={x['non_config_root_files']}")
    print()

    p = report["properties"]
    y = report["yaml"]
    print("[properties/yaml]")
    print(f"  .properties: files={p['files']} keys={p['keys']} "
          f"fqcn_like_values={p['fqcn_like_values']} placeholder_values={p['placeholder_values']}")
    print(f"  .yaml/.yml : files={y['files']} keys={y['keys']} "
          f"fqcn_like_values={y['fqcn_like_values']} placeholder_values={y['placeholder_values']}")
    print()

    s = report["sql"]
    print("[SQL]")
    print(f"  files_total={s['files_total']}")
    print(f"  CREATE TABLE: {_fmt_metric(s['create_table'])}  CREATE VIEW: {_fmt_metric(s['create_view'])}")
    print(f"  CREATE PROCEDURE/FUNCTION: {_fmt_metric(s['create_procedure_or_function'])}  "
          f"ALTER TABLE: {_fmt_metric(s['alter_table'])}")
    print(f"  テーブル名 Top20: {s['tables_top20']}")
    print(f"  CREATE TABLE 件数の分布: 単一={s['create_table_per_file']['single']} "
          f"複数={s['create_table_per_file']['multiple']}")
    print(f"  DDL皆無・DMLのみのファイル数: {s['dml_only_files']}")
    print()

    cc_ = report["c_cpp"]
    print("[C/C++]")
    print(f"  files_total={cc_['files_total']}")
    print(f"  #include \"local\": {_fmt_metric(cc_['include_local'])}  #include <ext>: {_fmt_metric(cc_['include_external'])}")
    print(f"  トップレベル関数定義らしき行: {_fmt_metric(cc_['function_def_lines'])}")
    print(f"  関数ポインタ呼び出しらしき行: {_fmt_metric(cc_['function_pointer_call_lines'])}")
    print()

    cs = report["csharp"]
    print("[C#]")
    print(f"  files_total={cs['files_total']}")
    print(f"  型宣言: {cs['type_decls']}")
    print(f"  継承行: {_fmt_metric(cs['inheritance_lines'])}")
    print(f"  ASP.NET属性: {_fmt_metric(cs['aspnet_attributes'])}")
    print(f"  DbSet<...>: {_fmt_metric(cs['dbset_usages'])}")
    print()

    ck = report["config_key_refs"]
    print("[コード内設定キー参照]")
    print(f"  properties/yaml キー総数(unique)={ck['keys_total']}")
    print(f"  構文ベース(getProperty/@Value/getString/AppSettings/Configuration[]/GetValue<T>): "
          f"{_fmt_metric(ck['syntax'])}")
    print(f"    Top20: {ck['syntax']['top']}")
    if isinstance(ck["dict_match"], str):
        print(f"  辞書突合ベース: {ck['dict_match']}")
    else:
        print(f"  辞書突合ベース: {_fmt_metric(ck['dict_match'])}")
        print(f"    Top20: {ck['dict_match']['top']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="構文分布スクリプト（S0・読み取り専用・LLM不使用・world書込みなし）")
    parser.add_argument("dirs", nargs="+", help="走査するディレクトリ（複数可）")
    parser.add_argument("--json", action="store_true", help="JSON で出力する（既定は人が読む表）")
    args = parser.parse_args(argv)

    for d in args.dirs:
        if not Path(d).is_dir():
            print(f"エラー: ディレクトリではありません: {d}", file=sys.stderr)
            return 2

    stats = scan(args.dirs)
    report = build_report(stats)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
