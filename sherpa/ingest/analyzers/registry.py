"""言語アナライザの登録簿（拡張子→アナライザ解決の単一の真実源・§2.4・§7 裁定2/5/10）。

既知アナライザの列挙順＝**優先順**（同じ拡張子を複数のアナライザが要求したら上位が担当）。
CODE-1b（管理画面の有効/無効・並び順）が本モジュールを介して `_ANALYZERS` を差し替える until
それまでは固定の既定リストのみ。`registered_extensions()` が「コード」と見なす拡張子集合の
単一の真実源——`doc_kinds.CODE_EXT`・`corpus_docs._doctype_map()`（コード分）・
`scope._CONTENT_EXT`（コード分）・`agentic_search._READABLE_EXT`（コード分）・
`ext_api._UTF8_DECLARE_EXT`／`_DOC_CONTENT_TYPE`（コード分）はすべてこれを参照する（§2.4）。
`resolve_lazy()` は拡張子だけでなく `accepts()` の内容判定まで見て担当アナライザを確定する
（`corpus_docs.iter_world_documents` が使う・既定 `accepts` のみなら内容を読まない）。

`_ANALYZERS` ＝ `_UPSTREAM_ANALYZERS`（本体の固定既定リスト）＋ `discover_extension_analyzers()`
（フォーク側の `<prefix>_*.py` 拡張アナライザを名前順で末尾に足す・拡張の契約 S4・
docs/21-拡張の契約.md）。発見・命名・版署名の詳細は各関数のドキュストリング参照。
"""
from __future__ import annotations

import importlib.util
import re
import sys
import uuid
from pathlib import Path, PurePosixPath

from ._base import KNOWN_VIA, VIA_PRIORITY, Analyzer  # noqa: F401  (世界層が参照する再エクスポート)
from ._base import via_priority_rank  # noqa: F401  (同上)
from .c import CAnalyzer
from .cobol import CobolAnalyzer
from .copybook import CopybookAnalyzer
from .css import CssAnalyzer
from .csharp import CSharpAnalyzer
from .html import HtmlTemplateAnalyzer
from .java import JavaAnalyzer
from .jcl import JclAnalyzer
from .js import JsAnalyzer
from .jsp import JspAnalyzer
from .properties import PropertiesAnalyzer
from .shell import ShellBatchAnalyzer
from .sql import SqlDdlAnalyzer
from .vb import VbAnalyzer
from .xml_config import XmlConfigAnalyzer
from .yaml_config import YamlConfigAnalyzer

# 優先順＝この並び順（§7 裁定2）。JavaAnalyzer は拡張子 `.java` が他アナライザと衝突しない
# ため末尾に追加（新言語1つでの手順検証・docs/proposals/2026-08-29-コード解析層のコンポーネント化.md §4.2）。
# Properties/YamlConfig/XmlConfig（アナライザ拡張 S3b・A6/A7）・SqlDdlAnalyzer（S2・A1/A10）も
# 拡張子が他アナライザと衝突しないため末尾に追加——新規アナライザの追加自体は `config_signature()`
# の `_ANALYZERS` タプル材料が自動的に変わるため `CODE_ANALYZERS_SCHEMA_VERSION` の据え置きでよい
# （S3b 前例）。CAnalyzer（`.c`/`.h`）・CSharpAnalyzer（`.cs`）も同じ理由で末尾追加（S6/S7・A1）。
# JspAnalyzer/HtmlTemplateAnalyzer/JsAnalyzer/CssAnalyzer（波3 レーンA）・ShellBatchAnalyzer
# （波3 レーンB）・VbAnalyzer（波3 レーンC）も同じ理由（拡張子非衝突）で末尾追加。ただし `.js`/
# `.sh`/`.bash`/`.zsh`/`.bat`/`.cmd`/`.vb` は従来 `text_kind.CODE_EXT`（軽量テキスト枠）が拾って
# いた拡張子のため、本登録により `classify_document()` の判定結果（branch）が変わる——
# `CODE_ANALYZERS_SCHEMA_VERSION` を上げる理由の一つ（v9 参照）。
_UPSTREAM_ANALYZERS: tuple[Analyzer, ...] = (
    CobolAnalyzer(), CopybookAnalyzer(), JclAnalyzer(), JavaAnalyzer(),
    PropertiesAnalyzer(), YamlConfigAnalyzer(), XmlConfigAnalyzer(),
    SqlDdlAnalyzer(), CAnalyzer(), CSharpAnalyzer(),
    JspAnalyzer(), HtmlTemplateAnalyzer(), JsAnalyzer(), CssAnalyzer(),
    ShellBatchAnalyzer(), VbAnalyzer(),
)

# 上流モジュールのファイル stem（拡張アナライザ発見の対象から除外・§4）。実ファイル名基準
# （`xml_config.py`/`yaml_config.py` は登録名 "xml_config"/"yaml_config" と同じ stem）。
_UPSTREAM_MODULE_STEMS = frozenset({
    "cobol", "copybook", "jcl", "java", "c", "csharp", "sql", "js", "jsp", "html", "css",
    "vb", "shell", "xml_config", "yaml_config", "properties",
})

# フォーク側が拡張アナライザの接頭辞として使えない予約語（拡張の契約 S4・docs/21-拡張の契約.md）。
# 上流の言語名＋内部モジュール名。実ファイル名と一致しない語（"xml"/"yaml"）も含める——本体が
# 将来 `xml:*`/`yaml:*` という登録名を使う余地を残し、フォークにその名前空間を先取りさせない。
RESERVED_ANALYZER_PREFIXES = frozenset({
    "cobol", "copybook", "jcl", "java", "c", "csharp", "sql", "js", "jsp", "html", "css",
    "vb", "shell", "xml", "yaml", "properties", "base", "registry",
})

# 接頭辞の文字種（英小文字・数字・`_`）。`_` は区切りとしてだけでなく接頭辞内部でも使える
# （例: サンプル拡張の接頭辞 `sample_ext`）——禁止するのは大文字・記号・空文字のみ。
_PREFIX_CHARSET_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# 拡張子の形式（照合側 `_ext()` は `PurePosixPath.suffix` の単一区切りしか返さない）。`.` + 英小文字/
# 数字のみの単一区切り以外——大文字・単なる「.」・空文字だけでなく `.d.ts` のような多段接尾辞も
# `_ext()` の出力形とは永久に一致せず担当なしになるため、契約違反として弾く。
_EXT_FORMAT_RE = re.compile(r"\.[a-z0-9]+")


class ExtensionAnalyzerError(RuntimeError):
    """拡張アナライザの発見時契約違反（接頭辞不一致・拡張子衝突・version<1 等）。

    黙って落とさない（アナライザ増設5箇条）——発見（モジュール import）時に例外にする。
    """


# 資料（非コード）として本体が扱う拡張子。corpus_docs（`_NONCODE_DOCTYPE`／`_OFFICE_DOCTYPE`）は本モジュールを
# import するため、ここから逆向きに import できない＝定数を写し、整合は単体テストで固定する。
_DOCUMENT_EXT_FOR_COLLISION: frozenset = frozenset({
    ".md", ".markdown", ".txt",
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf",
    ".csv", ".tsv", ".rtf", ".log",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff",   # 画像（office_md.IMAGE_EXT）
})


# 秘匿ファイル（text_kind.SENSITIVE_EXT の写し・同期は単体テストで固定）。
_SENSITIVE_EXT_FOR_COLLISION: frozenset = frozenset({".key", ".pem", ".ppk", ".env"})


def _noncode_document_extensions() -> frozenset:
    """資料（非コード）として本体が扱う拡張子の集合（写し・corpus_docs／text_kind と単体テストで同期）。"""
    return _DOCUMENT_EXT_FOR_COLLISION


def discover_extension_analyzers(analyzers_dir: Path | None = None) -> tuple[Analyzer, ...]:
    """`<prefix>_*.py`（フォーク側の拡張アナライザ・docs/21-拡張の契約.md）を発見し、契約を検証して
    名前順のタプルで返す。

    対象: `analyzers_dir`（省略時は本パッケージのディレクトリ＝本番の発見対象。テストは tmp
    ディレクトリを注入して発見規約・契約違反を検証する）直下の `*.py` のうち、`_` 始まり
    （`_base.py`/`_sql_scan.py`/`__init__.py` 等の内部モジュール）・上流モジュール stem
    （`_UPSTREAM_MODULE_STEMS`）・`registry` のいずれでもないもの全てを一旦読み込む。モジュール
    属性 `ANALYZER`（`Analyzer` のインスタンス）を持たないものは拡張アナライザを名乗っていないと
    みなし黙って読み飛ばす——`ANALYZER` を持つものだけが契約検証＋登録の対象で、ファイル名に `_`
    を含まない（`<prefix>_*.py` の形でない）ものは命名規約違反として `ExtensionAnalyzerError`
    にする（`ANALYZER` を宣言している以上、黙って読み飛ばさない）。

    衝突判定の基準は上流アナライザ（`_UPSTREAM_ANALYZERS`）の拡張子集合に加え、既にこの発見処理で
    見つかった拡張アナライザの `name`／`extensions`。`overrides` の除外は上流との衝突判定だけに使う
    ——拡張アナライザ同士の衝突判定は `extensions` **全体**で行う（`overrides` に無いものだけを見ると、
    同じ上流拡張子を意図的に共有する2本が互いに素通りしてしまう）。ただし同じ上流拡張子を両方が
    `overrides` で明示的に共有していれば例外として許可する。`analyzers_dir` を注入したテストでも
    本番の上流構成に対して検証する（フォークが守るべき契約は本番の上流と同じであるべきため）。
    """
    base_dir = analyzers_dir if analyzers_dir is not None else Path(__file__).resolve().parent
    upstream_ext: frozenset = frozenset().union(*(a.extensions for a in _UPSTREAM_ANALYZERS))
    # 資料側の拡張子（.md/.txt/Office/PDF 等）も「上流の担当」に含める。ここに当たる拡張が
    # overrides 無しで登録されると、資料の分類経路（派生 MD・branch）を黙って奪う。
    upstream_ext = upstream_ext | _noncode_document_extensions()
    found: list[Analyzer] = []
    found_names: set[str] = set()
    # 拡張子 → (既に見つかった拡張アナライザの name, その拡張子を overrides で宣言していたか)。
    # 後者は「同じ上流拡張子を両方が overrides で共有」の例外判定に使う（衝突検出用）。
    found_ext_owner: dict[str, tuple[str, bool]] = {}
    for path in sorted(base_dir.glob("*.py")):
        stem = path.stem
        if stem.startswith("_") or stem in _UPSTREAM_MODULE_STEMS or stem == "registry":
            continue
        module = _load_module_from_path(stem, path)
        analyzer = getattr(module, "ANALYZER", None)
        if analyzer is None:
            continue   # ANALYZER を持たない＝拡張アナライザを名乗っていない（黙って読み飛ばす）
        if "_" not in stem:
            # ANALYZER を持つ＝拡張アナライザを名乗っている以上、命名規約違反（`<prefix>_*.py` でない）
            # を黙って読み飛ばさない（読み飛ばすのは ANALYZER を持たない無関係なファイルだけ）。
            raise ExtensionAnalyzerError(
                f"{stem}.py: ANALYZER を持つファイルは '<prefix>_*.py' の形にしてください（ファイル名に '_' がありません）")
        _validate_extension_analyzer(analyzer, stem, upstream_ext)
        if analyzer.name in found_names:
            raise ExtensionAnalyzerError(
                f"{stem}.py: 登録名 {analyzer.name!r} が既に見つかった拡張アナライザと重複しています")
        for ext in analyzer.extensions:
            prior = found_ext_owner.get(ext)
            if prior is None:
                continue
            prior_name, prior_is_override = prior
            # 例外: 同じ上流拡張子を両方が overrides で明示的に共有している場合だけ許可する
            # （overrides の除外は上流との衝突判定だけに使う——ここは extensions 全体で見る）。
            shared_upstream_override = (
                ext in upstream_ext and ext in analyzer.overrides and prior_is_override)
            if not shared_upstream_override:
                raise ExtensionAnalyzerError(
                    f"{stem}.py: 拡張子 {ext!r} が既に見つかった拡張アナライザ {prior_name!r} と"
                    "衝突しています（意図的に同じ上流拡張子を共有するなら両方で ANALYZER.overrides に"
                    "明示してください）")
        found_names.add(analyzer.name)
        for ext in analyzer.extensions:
            if ext not in found_ext_owner:
                found_ext_owner[ext] = (analyzer.name, ext in analyzer.overrides)
        found.append(analyzer)
    found.sort(key=lambda a: a.name)
    return tuple(found)


def _load_module_from_path(stem: str, path: Path):
    """`path` を独立モジュールとして読み込む（`sys.modules` は一時登録のみ・衝突を避けるため
    呼び出しごとに一意なモジュール名を使う）。"""
    mod_name = f"_sherpa_ext_analyzer__{stem}__{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ExtensionAnalyzerError(f"{path}: モジュール仕様を構築できません")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(mod_name, None)
    return module


def _validate_extension_analyzer(analyzer, filename_stem: str, upstream_ext: frozenset) -> None:
    """発見した拡張アナライザの契約検証（§4・docs/21-拡張の契約.md）。違反は `ExtensionAnalyzerError`。"""
    if not isinstance(analyzer, Analyzer):
        raise ExtensionAnalyzerError(f"{filename_stem}.py: ANALYZER は Analyzer のインスタンスではありません")
    # 型ミスは契約違反として報告する（黙って TypeError の traceback にしない・後続の集合演算・比較が
    # list/str 相手に失敗する前に検査する）。
    if not isinstance(analyzer.extensions, (set, frozenset)):
        raise ExtensionAnalyzerError(
            f"{filename_stem}.py: ANALYZER.extensions は set/frozenset にしてください"
            f"（実際の型: {type(analyzer.extensions).__name__}）")
    if not isinstance(analyzer.version, int) or isinstance(analyzer.version, bool):
        raise ExtensionAnalyzerError(
            f"{filename_stem}.py: ANALYZER.version は int にしてください"
            f"（実際の型: {type(analyzer.version).__name__}）")
    if "version" not in vars(type(analyzer)):
        # 上流クラス（JavaAnalyzer 等）を継承した拡張が version を省略すると上流の版を継承し、上流の版上げで
        # 拡張の署名要素まで変わる＝「署名の独立」が破れる。拡張自身のクラスで宣言させる。
        raise ExtensionAnalyzerError(
            f"{filename_stem}.py: ANALYZER.version は拡張自身のクラスで宣言してください"
            "（上流クラスからの継承値は署名の独立を破るため受け付けない）")
    if not isinstance(analyzer.overrides, (set, frozenset)):
        raise ExtensionAnalyzerError(
            f"{filename_stem}.py: ANALYZER.overrides は set/frozenset にしてください"
            f"（実際の型: {type(analyzer.overrides).__name__}）")
    if not isinstance(analyzer.name, str):
        raise ExtensionAnalyzerError(
            f"{filename_stem}.py: ANALYZER.name は文字列にしてください（実際の型: {type(analyzer.name).__name__}）")
    name = analyzer.name or ""
    if ":" not in name:
        raise ExtensionAnalyzerError(
            f"{filename_stem}.py: ANALYZER.name は '<prefix>:<kind>' の形にしてください（実際: {name!r}）")
    prefix, _, kind = name.partition(":")
    if not kind:
        raise ExtensionAnalyzerError(f"{filename_stem}.py: ANALYZER.name の kind 部分が空です（実際: {name!r}）")
    if not _PREFIX_CHARSET_RE.match(prefix):
        raise ExtensionAnalyzerError(
            f"{filename_stem}.py: prefix {prefix!r} は英小文字・数字・_のみ使えます（先頭は英小文字）")
    if prefix in RESERVED_ANALYZER_PREFIXES:
        raise ExtensionAnalyzerError(f"{filename_stem}.py: prefix {prefix!r} は上流の予約名です")
    if not filename_stem.startswith(prefix + "_"):
        raise ExtensionAnalyzerError(
            f"{filename_stem}.py: ANALYZER.name の prefix {prefix!r} がファイル名と一致しません"
            f"（ファイル名は '{prefix}_...' で始まる必要があります）")
    if not analyzer.extensions:
        raise ExtensionAnalyzerError(f"{filename_stem}.py: ANALYZER.extensions が空です")
    if not isinstance(analyzer.doctype, str) or not analyzer.doctype.strip():
        raise ExtensionAnalyzerError(
            f"{filename_stem}.py: ANALYZER.doctype（種別表示名）を非空の文字列で宣言してください"
            "（空のまま登録すると台帳・取り込み画面の種別が空文字で流れる）")
    for ext in analyzer.extensions:
        if not isinstance(ext, str):
            raise ExtensionAnalyzerError(
                f"{filename_stem}.py: 拡張子は文字列にしてください（実際の型: {type(ext).__name__}）")
        if not _EXT_FORMAT_RE.fullmatch(ext):
            raise ExtensionAnalyzerError(
                f"{filename_stem}.py: 拡張子 {ext!r} は '.' + 英小文字/数字のみの単一区切りにしてください"
                "（照合側 _ext() は PurePosixPath.suffix の単一区切りしか返さないため、"
                "'.d.ts' のような多段接尾辞や大文字宣言は永久に担当なしになります）")
    # 秘匿ファイル（.env/.pem 等）は overrides でも担当できない。担当できると秘匿除外が無効化され、
    # 本文が grep・精読（外部 LLM 送信）へ流れる。
    sensitive = analyzer.extensions & _SENSITIVE_EXT_FOR_COLLISION
    if sensitive:
        raise ExtensionAnalyzerError(
            f"{filename_stem}.py: 秘匿ファイルの拡張子 {sorted(sensitive)} は拡張アナライザで担当できません"
            "（overrides でも不可）")
    not_declared = analyzer.extensions & upstream_ext - analyzer.overrides
    if not_declared:
        raise ExtensionAnalyzerError(
            f"{filename_stem}.py: 拡張子 {sorted(not_declared)} が上流アナライザと衝突しています"
            "（意図的なら ANALYZER.overrides で明示してください）")
    if analyzer.version < 1:
        raise ExtensionAnalyzerError(f"{filename_stem}.py: version は1以上にしてください（実際: {analyzer.version}）")


_ANALYZERS: tuple[Analyzer, ...] = _UPSTREAM_ANALYZERS + discover_extension_analyzers()

# `accepts()`/`classify_document` の分類契約版——分類結果（同じ入力に対する kind/doctype/branch の
# 判定）に影響する意味変更（例: 既定 accepts の扱いを変える・優先順の解決規則を変える）があれば
# 上げる。`config_signature()` の材料（`importance.IMPORTANCE_SCHEMA_VERSION` と同じ流儀）。
# v2: 軽量テキスト枠（`ingest.text_kind`）導入——`classify_document()` の「担当なし」経路が
# 未登録拡張子のテキストファイルを新たに code/document 判定するようになった（従来は未対応の
# まま台帳・ES に載らなかった）。登録簿自体（`_ANALYZERS`/`extensions`）は無変更のため、この
# 版を上げないと `content_sig`/ES `analyzer_config_sig` が drift を検知できず、既存 world が
# 次回 sync/reindex まで新しい分類を反映しない。
CODE_ANALYZERS_SCHEMA_VERSION = 10   # v3: _CALL/_COPY の前方語境界是正（偽参照の除去）
# v4: COPY/CALL 抽出前に引用文字列の中身／行末インラインコメント
# （`*>` 以降）を除去する前処理を追加（COBOL の引用/コメント誤検知の是正）＋ `CALL "PGM"`
# （二重引用符）も INVOKES として受理するよう `_CALL` を拡張。
# v5: `NODE_LABELS` へ `Config` を追加（A6・設定ファイルアナライザの受け皿）。共通層の契約拡張
# （`RefCandidate.reverse`／qualified 名の2段解決／エッジ集約・KNOWN_VIA の Config 系 via 追加）も
# 本版に含む。
# v6（アナライザ拡張）: 既存 `CobolAnalyzer` の `extract_refs` へ `EXEC SQL`→
# `Table`/`ACCESSES(via=exec_sql)` 抽出を追加（同一構成のまま抽出結果が変わる変更・§6 版管理表）。
# `SqlDdlAnalyzer` の新規登録自体は上記の理由により版を上げない。`DefResult.extras`（A10・DDL の
# 複数 `CREATE TABLE` 用）の共通層契約拡張も本版に含む（既存アナライザは `extras` 既定空で無変更）。
# v7（アナライザ拡張 波2）: `CAnalyzer`/`CSharpAnalyzer` の新規登録（S6/S7）＋
# 既存 `CobolAnalyzer` の `EXEC CICS XCTL/LINK` 抽出（S5b）をまとめて1回で版上げする——新規登録
# 単独では `config_signature()` が自動的に構成差分を検知するため版据え置きでもよい前例（S3b）が
# あるが、波2は既存アナライザの抽出結果が変わる変更（S5b）を同時に含むため、対象を1つずつ切り
# 分けず波全体で1回に統一する。
# v8（アナライザ拡張 S3' 残課題）: 既存 `XmlConfigAnalyzer` の `collect_defs` へ
# キー単位 `Config` children（Spring `<bean>`/`<property>`/`<alias>`・MyBatis 文 id/`<resultMap>`・
# Struts `<action>`/`<constant>`）を追加し、既存 `JavaAnalyzer` の設定キー参照抽出へ
# `getBean`/`@Qualifier`/`@Named`/`@Resource(name=...)` を追加した（同一構成のまま抽出結果が
# 変わる変更・§6 版管理表）。
# v9（アナライザ拡張 波3 統合）: `JspAnalyzer`/`HtmlTemplateAnalyzer`/`JsAnalyzer`/
# `CssAnalyzer`（レーンA・画面テンプレート/JS/CSS）・`ShellBatchAnalyzer`（レーンB・シェル/バッチ）・
# `VbAnalyzer`（レーンC・VB.NET/VB6/VBA/VBScript）を新規登録し、`text_kind.CODE_EXT` から
# `.js`/`.sh`/`.bash`/`.zsh`/`.bat`/`.cmd`/`.vb`（専用アナライザに移管した拡張子）を外した——
# 新規アナライザの追加自体は登録簿の構成差分として自動検知されるが、これらの拡張子が軽量テキスト
# 枠から専用アナライザ判定へ切り替わる `classify_document()` の分類契約変更（v2 と同種）を含むため
# 明示的に版を上げる。既存 `JavaAnalyzer` の URL キー定義側（`@RequestMapping`/`@GetMapping`等→
# `Config` children・`key_kind="url"`）追加、`_base.KNOWN_VIA`/`VIA_PRIORITY` への `vba_sql` 追加
# （VbAnalyzer の SQL 文字列参照の受け皿）も本版に含む。
# v10（アナライザ拡張 波3 統合の是正）: `Config` キーの `cid_key` へ `key_kind` を含めて
# 名前空間を分離（同名でも種別が違えば別ノード＝既存 cid と不一致）、単純名解決（`simple_name_defs`）
# を言語（`c_kind`）内に限定（C/C# 間の誤接続を止める＝既存の跨言語一致が unresolved に変わり得る）、
# `JavaAnalyzer` の `@RequestMapping`（クラス）×`@GetMapping`等（メソッド）の URL キーを配列直積で
# 展開（複数値の組み合わせ抜けを解消）——いずれも登録簿の構成（`_ANALYZERS`/`extensions`）自体は
# 無変更のまま抽出結果・cid が変わるため明示的に版を上げる。

# docs/05-グラフ語彙.md のクローズド語彙（アナライザが返してよいラベル/エッジ型の上限・§7 裁定5）。
# （2026-09-04-グラフのソース正典化.md §4）確定リスト＋A6（`Config` 追加）。刈った型は復活させない
# （`ingest.model.NODE_LABELS`/`EDGE_TYPES` と同じ集合＝`world_neo4j.WORLD_EDGE_TYPES` が
# `CORRESPONDS_TO` を別途加算する）。
NODE_LABELS = frozenset({"Module", "Copybook", "Batch", "DataItem", "Table", "Document", "Config"})
EDGE_TYPES = frozenset({"COPIES", "CONTAINS", "INVOKES", "ACCESSES", "DOCUMENTS"})


def known_analyzers() -> tuple[Analyzer, ...]:
    """既知アナライザの一覧（優先順のまま）。"""
    return _ANALYZERS


def registered_extensions() -> frozenset:
    """全アナライザの担当拡張子の和集合（拡張子集合の単一の真実源・§2.4）。"""
    exts: set = set()
    for a in _ANALYZERS:
        exts |= set(a.extensions)
    return frozenset(exts)


def config_signature() -> tuple:
    """現在の有効構成の署名（核の分類契約版＋登録順のアナライザごとの `(name, version, extensions)`）。

    world 署名（`ingest/worker.py::_sig`）・ES 設定署名（`es_index.needs_reindex`）の材料に使う——
    新規アナライザの追加・CODE-1b（管理画面）による有効/無効・並び替え・部品ごとの `version` 変更の
    いずれかで構成が変われば署名が変わり、標準の「署名不一致→再構築」経路で台帳・Neo4j・ES の
    `branch`（`corpus_docs.classify_document` 確定値）が自動的に作り直される（専用の移行機構を
    持たない・`importance.IMPORTANCE_SCHEMA_VERSION` と同じ流儀）。呼び出しごとに `_ANALYZERS`
    から都度計算する（`registered_extensions()` と同じくキャッシュしない）。

    材料を `(name, tuple(extensions))` から `(name, version, tuple(extensions))` へ拡張した
    （拡張の契約 S4・部品ごとの版署名）——**署名の独立まで**が保証範囲: ある部品の `version` を
    上げても他の部品の材料（`name, version, extensions`）はそれぞれ独立のタプルのまま不変。ただし
    `_ANALYZERS` 全体のタプルは変わるため、`config_signature()` 全体の値は変わり、世代署名が
    畳み込まれる world は現行どおり全再構築される（費用ゼロ・時間のみ——ファイル単位の解析キャッシュ
    や部分再構築は別スライス）。この材料形状の変更自体が世代署名を変えるため
    `CODE_ANALYZERS_SCHEMA_VERSION` の明示的な版上げは不要（v9 の新規アナライザ登録と同じ前例）。
    """
    return (CODE_ANALYZERS_SCHEMA_VERSION,
            tuple((a.name, a.version, tuple(sorted(a.extensions))) for a in _ANALYZERS))


def candidates(rel_path: str) -> tuple[Analyzer, ...]:
    """`rel_path` の拡張子を担当し得るアナライザ（優先順）。内容判定（`accepts`）は行わない。"""
    ext = _ext(rel_path)
    if not ext:
        return ()
    return tuple(a for a in _ANALYZERS if ext in a.extensions)


def resolve(rel_path: str, head_text: str = "") -> Analyzer | None:
    """`rel_path` の担当アナライザ（優先順で `accepts()` を通った最初のもの・§7 裁定2/10）。

    どのアナライザも通らなければ `None`（資料の枠へ倒す——実際に資料として扱われるのは既存の
    資料種別に該当する場合のみ・該当しなければ未対応＝§7 裁定10）。
    """
    for a in candidates(rel_path):
        if a.accepts(rel_path, head_text):
            return a
    return None


def resolve_lazy(rel_path: str, read_head) -> Analyzer | None:
    """`resolve()` の遅延読み取り版。`accepts()` を上書きしている候補があるときだけ `read_head()` を呼ぶ。

    候補全員が既定の `accepts`（常に真）のままなら先頭候補がそのまま確定し、`read_head` は
    一度も呼ばれない（内容を読まない＝列挙コストを増やさない・§7 裁定10）。`read_head` は
    実際に必要になるまでファイルを開かない callable として呼び出し側が渡す（例: 先頭数 KB を
    読むクロージャ）——`size`（キーワード専用で呼ぶ・省略時の既定は呼び出し側のクロージャに委ねる）
    を受け取れる必要がある。読む量は候補ごとの `head_bytes`（`Analyzer` 既定4KiB・
    `HtmlTemplateAnalyzer` は64KiB）に従い、候補ごとに自分の head だけで判定する（Pass1 と同じ材料）。
    """
    cands = candidates(rel_path)
    if not cands:
        return None
    overriding = [a for a in cands if _overrides_accepts(a)]
    if not overriding:
        return cands[0]
    # 候補ごとに自分の `head_bytes` 分だけを渡す（world_graph の Pass1 と同じ判定材料にする）。
    # 大きい head を要求する候補の読み取り結果を小さい head の候補にそのまま渡すと、両経路で
    # 分類が食い違う（片方は受理・片方は拒否）。同じサイズの読み取りは 1 回だけ。
    heads: dict[int, str] = {}
    for a in cands:
        if not _overrides_accepts(a):
            return a
        size = getattr(a, "head_bytes", 4096)
        if size not in heads:
            heads[size] = read_head(size=size)
        if a.accepts(rel_path, heads[size]):
            return a
    return None


def _overrides_accepts(a: Analyzer) -> bool:
    """`a` が基底の既定 `accepts`（常に真）をオーバーライドしているか。"""
    return type(a).accepts is not Analyzer.accepts


def _ext(rel_path: str) -> str:
    """`rel_path` の拡張子（`Path.suffix` と同じ規約・ドットのみのファイル名は拡張子なし扱い）。"""
    return PurePosixPath(rel_path).suffix.lower()
