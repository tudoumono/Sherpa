"""言語アナライザの登録簿（拡張子→アナライザ解決の単一の真実源）。

既知アナライザの列挙順＝優先順（同じ拡張子を複数が要求したら上位が担当）。`registered_extensions()` が「コード」と見なす拡張子集合の単一の真実源で、`doc_kinds.CODE_EXT`・`scope._CONTENT_EXT`・`agentic_search._READABLE_EXT`・`ext_api` はこれを参照する。`resolve_lazy()` は拡張子に加えて `accepts()` の内容判定まで見て担当を確定する。
`_ANALYZERS` ＝ `_UPSTREAM_ANALYZERS`（本体の固定リスト）＋ `discover_extension_analyzers()`（フォーク側の `<prefix>_*.py` を名前順で末尾に足す）。
`FW_PLUGINS` ＝ `_UPSTREAM_FW_PLUGINS`（本体の固定リスト）＋ `discover_fw_plugins()`（`<prefix>_*.py` のモジュール属性 `FW_PLUGINS`＋宣言的なルールファイル `rules_*.toml`）。FW プラグインは本体のアナライザの後に `(order, 登録名)` の昇順で適用する（`apply_fw_defs`／`apply_fw_refs`）。
設計: docs/design/rag.md「グラフ」・docs/21-拡張の契約.md §3
"""
from __future__ import annotations

import copy
import importlib.util
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ._base import KNOWN_VIA, VIA_PRIORITY, Analyzer  # noqa: F401  (世界層が参照する再エクスポート)
from ._base import (DefGroup, DefItem, DefResult, Dropped, FwPlugin, PluginAmbiguity, PluginDefs, PluginRefs,
                    RefCandidate, RefResult, TypeCandidate, TypeRelations)
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
from .spring_java import SpringJavaPlugin
from .sql import SqlDdlAnalyzer
from .vb import VbAnalyzer
from . import _rules
from .xml_config import XmlConfigAnalyzer
from .xml_config_fw import FW_PLUGINS as _XML_FW_PLUGINS
from .yaml_config import YamlConfigAnalyzer

# 優先順＝この並び順。拡張子が他と衝突しない新規アナライザは末尾に追加する（`config_signature()` の材料が自動で変わるので `CODE_ANALYZERS_SCHEMA_VERSION` は据え置きでよい）。
_UPSTREAM_ANALYZERS: tuple[Analyzer, ...] = (
    CobolAnalyzer(), CopybookAnalyzer(), JclAnalyzer(), JavaAnalyzer(),
    PropertiesAnalyzer(), YamlConfigAnalyzer(), XmlConfigAnalyzer(),
    SqlDdlAnalyzer(), CAnalyzer(), CSharpAnalyzer(),
    JspAnalyzer(), HtmlTemplateAnalyzer(), JsAnalyzer(), CssAnalyzer(),
    ShellBatchAnalyzer(), VbAnalyzer(),
)

# 上流モジュールのファイル stem（拡張アナライザ発見の対象から除外する。実ファイル名基準）。
_UPSTREAM_MODULE_STEMS = frozenset({
    "cobol", "copybook", "jcl", "java", "c", "csharp", "sql", "js", "jsp", "html", "css",
    "vb", "shell", "xml_config", "xml_config_fw", "yaml_config", "properties", "spring_java",
})

# フォーク側が拡張アナライザの接頭辞として使えない予約語（上流の言語名＋内部モジュール名。`xml`/`yaml` も含める）。
RESERVED_ANALYZER_PREFIXES = frozenset({
    "cobol", "copybook", "jcl", "java", "c", "csharp", "sql", "js", "jsp", "html", "css",
    "vb", "shell", "xml", "yaml", "properties", "base", "registry",
})

# 接頭辞の文字種（英小文字・数字・`_`）。禁止するのは大文字・記号・空文字のみ。
_PREFIX_CHARSET_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# 拡張子の形式。`.` + 英小文字/数字のみの単一区切りに限る（`_ext()` の出力形と一致しないものは担当なしになるため契約違反にする）。
_EXT_FORMAT_RE = re.compile(r"\.[a-z0-9]+")


class ExtensionAnalyzerError(RuntimeError):
    """拡張アナライザの発見時契約違反（接頭辞不一致・拡張子衝突・version<1 等）。発見（モジュール import）時に例外にする。"""


# 資料（非コード）として本体が扱う拡張子。`corpus_docs` が本モジュールを import するため逆向きに import できず、定数を写して整合を単体テストで固定する。
_DOCUMENT_EXT_FOR_COLLISION: frozenset = frozenset({
    ".md", ".markdown", ".txt",
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf",
    ".csv", ".tsv", ".rtf", ".log",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff",  # 画像（office_md.IMAGE_EXT）
})


# 秘匿ファイル（text_kind.SENSITIVE_EXT の写し・同期は単体テストで固定）。
_SENSITIVE_EXT_FOR_COLLISION: frozenset = frozenset({".key", ".pem", ".ppk", ".env"})


def _noncode_document_extensions() -> frozenset:
    """資料（非コード）として本体が扱う拡張子の集合（写し）。"""
    return _DOCUMENT_EXT_FOR_COLLISION


def discover_extension_analyzers(analyzers_dir: Path | None = None) -> tuple[Analyzer, ...]:
    """`<prefix>_*.py`（フォーク側の拡張アナライザ）を発見し、契約を検証して名前順のタプルで返す。

    対象は `analyzers_dir`（省略時は本パッケージのディレクトリ）直下の `*.py` のうち、`_` 始まり・上流モジュール stem・`registry` を除くもの。
    - モジュール属性 `ANALYZER` を持たないものは黙って読み飛ばす。持つものはファイル名に `_` を含まなければ `ExtensionAnalyzerError`。
    - 衝突判定は上流の拡張子集合と、既に見つかった拡張アナライザの `name`／`extensions` に対して行う。`overrides` の除外は上流との衝突判定だけに使い、拡張アナライザ同士は `extensions` 全体で判定する（同じ上流拡張子を両方が `overrides` で共有する場合だけ許可）。
    - `analyzers_dir` を注入しても本番の上流構成に対して検証する。
    """
    base_dir = analyzers_dir if analyzers_dir is not None else Path(__file__).resolve().parent
    upstream_ext: frozenset = frozenset().union(*(a.extensions for a in _UPSTREAM_ANALYZERS))
    # 資料側の拡張子（.md/.txt/Office/PDF 等）も「上流の担当」に含める（`overrides` 無しで登録されると資料の分類経路を奪う）。
    upstream_ext = upstream_ext | _noncode_document_extensions()
    found: list[Analyzer] = []
    found_names: set[str] = set()
    # 拡張子 → (既に見つかった拡張アナライザの name, その拡張子を overrides で宣言していたか)。
    found_ext_owner: dict[str, tuple[str, bool]] = {}
    for path in sorted(base_dir.glob("*.py")):
        stem = path.stem
        if stem.startswith("_") or stem in _UPSTREAM_MODULE_STEMS or stem == "registry":
            continue
        module = _load_module_from_path(stem, path)
        analyzer = getattr(module, "ANALYZER", None)
        if analyzer is None:
            continue  # ANALYZER を持たない＝拡張アナライザではない（読み飛ばす）
        if "_" not in stem:
            # ANALYZER を持つ以上、命名規約違反（`<prefix>_*.py` でない）は読み飛ばさず例外にする。
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
            # 例外: 同じ上流拡張子を両方が overrides で共有している場合だけ許可する。
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
    """`path` を独立モジュールとして読み込む（`sys.modules` へは一時登録のみ・呼び出しごとに一意なモジュール名）。"""
    mod_name = f"_sherpa_ext_analyzer__{stem}__{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ExtensionAnalyzerError(f"{path}: モジュール仕様を構築できません")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except ExtensionAnalyzerError:
        raise
    except Exception as e:  # import・構文の失敗は黙って落とさず、契約違反として明示の失敗にする
        raise ExtensionAnalyzerError(f"{path.name}: モジュールを読み込めません（{type(e).__name__}: {e}）") from e
    finally:
        sys.modules.pop(mod_name, None)
    return module


def _validate_extension_analyzer(analyzer, filename_stem: str, upstream_ext: frozenset) -> None:
    """発見した拡張アナライザの契約検証。違反は `ExtensionAnalyzerError`。"""
    if not isinstance(analyzer, Analyzer):
        raise ExtensionAnalyzerError(f"{filename_stem}.py: ANALYZER は Analyzer のインスタンスではありません")
    # 型ミスは契約違反として報告する（後続の集合演算・比較が失敗する前に検査する）。
    if not isinstance(analyzer.extensions, (set, frozenset)):
        raise ExtensionAnalyzerError(
            f"{filename_stem}.py: ANALYZER.extensions は set/frozenset にしてください"
            f"（実際の型: {type(analyzer.extensions).__name__}）")
    if not isinstance(analyzer.version, int) or isinstance(analyzer.version, bool):
        raise ExtensionAnalyzerError(
            f"{filename_stem}.py: ANALYZER.version は int にしてください"
            f"（実際の型: {type(analyzer.version).__name__}）")
    if "version" not in vars(type(analyzer)):
        # 上流クラスを継承した拡張が version を省略すると上流の版を継承してしまう。拡張自身のクラスで宣言させる。
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
    # 秘匿ファイル（.env/.pem 等）は overrides でも担当できない（秘匿除外が無効化され、本文が外部 LLM へ流れるため）。
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

# docs/05-グラフ語彙.md のクローズド語彙（アナライザが返してよいラベル/エッジ型の上限）。`ingest.model.NODE_LABELS`/`EDGE_TYPES` と同じ集合。
NODE_LABELS = frozenset({"Module", "Copybook", "Batch", "DataItem", "Table", "Document", "Config"})
EDGE_TYPES = frozenset({"COPIES", "CONTAINS", "INVOKES", "ACCESSES", "DOCUMENTS"})

# ---- FW プラグイン（本体のアナライザの結果へ FW 固有の定義・参照を足す部品） ----

# 本体の FW プラグイン（上流の `<prefix>_*.py` 規約の外・予約語の接頭辞を使える）。
_UPSTREAM_FW_PLUGINS: tuple[FwPlugin, ...] = (SpringJavaPlugin(), *_XML_FW_PLUGINS)


class FwPluginError(ExtensionAnalyzerError):
    """FW プラグインの登録時契約違反（名前・対象言語・版・適用条件・衝突）。登録（モジュール import）時に例外にする。"""


@dataclass(frozen=True)
class PluginFailure:
    """適用時に例外を出した FW プラグインの 1 件（`phase`＝`defs`／`refs`・`why`＝例外の型と先頭 200 字）。"""

    plugin: str
    phase: str
    why: str


def discover_fw_plugins(analyzers_dir: Path | None = None) -> tuple[FwPlugin, ...]:
    """`<prefix>_*.py` のモジュール属性 `FW_PLUGINS`（`FwPlugin` のリスト）を発見し、契約を検証して `(order, 登録名)` 順のタプルで返す。

    対象ファイルは `discover_extension_analyzers` と同じ（`_` 始まり・上流モジュール stem・`registry` を除く）。`FW_PLUGINS` を持たないモジュールは読み飛ばす。
    持つモジュールはファイル名に `_` を含み、各プラグインの登録名の接頭辞がファイル名の接頭辞と一致すること。import の失敗・契約違反は `FwPluginError`／元の例外のまま送出する（黙って読み飛ばさない）。
    """
    base_dir = analyzers_dir if analyzers_dir is not None else Path(__file__).resolve().parent
    try:
        known_names = {a.name for a in _UPSTREAM_ANALYZERS} | {a.name for a in discover_extension_analyzers(base_dir)}
    except FwPluginError:
        raise
    except ExtensionAnalyzerError as e:
        raise FwPluginError(str(e)) from e
    found: list[FwPlugin] = []
    seen = {p.name for p in _UPSTREAM_FW_PLUGINS}
    for path in sorted(base_dir.glob("*.py")):
        stem = path.stem
        if stem.startswith("_") or stem in _UPSTREAM_MODULE_STEMS or stem == "registry":
            continue
        try:
            module = _load_module_from_path(stem, path)
        except FwPluginError:
            raise
        except ExtensionAnalyzerError as e:   # import・構文の失敗も FW プラグインの発見の契約違反として返す
            raise FwPluginError(str(e)) from e
        plugins = getattr(module, "FW_PLUGINS", None)
        if plugins is None:
            continue
        if "_" not in stem:
            raise FwPluginError(
                f"{stem}.py: FW_PLUGINS を持つファイルは '<prefix>_*.py' の形にしてください（ファイル名に '_' がありません）")
        if not isinstance(plugins, (list, tuple)):
            raise FwPluginError(f"{stem}.py: FW_PLUGINS は list/tuple にしてください（実際の型: {type(plugins).__name__}）")
        for plugin in plugins:
            _validate_fw_plugin(plugin, stem, known_names, upstream=False)
            if plugin.name in seen or plugin.name in known_names:
                raise FwPluginError(f"{stem}.py: 登録名 {plugin.name!r} が既に登録済みの FW プラグイン／アナライザと重複しています")
            seen.add(plugin.name)
            found.append(plugin)
    for path in sorted(base_dir.glob(f"{_rules.RULE_FILE_PREFIX}_*.toml")):   # 宣言的なルールファイル（1 ファイル＝1 プラグイン・docs/21 §3a）
        if path.is_symlink() or path.resolve().parent != base_dir.resolve():   # 置き場所の外を読ませない
            raise FwPluginError(f"{path.name}: ルールファイルはシンボリックリンクにできません・置き場所（{base_dir.name}/）の直下にあること")
        try:
            plugin = _rules.load_rules_plugin(path, node_labels=NODE_LABELS, edge_types=EDGE_TYPES, known_analyzer_names=known_names)
        except _rules.RulesError as e:
            raise FwPluginError(f"{path.name}: {e}") from e
        try:
            _validate_fw_plugin(plugin, path.stem, known_names, upstream=False)
        except FwPluginError as e:
            raise FwPluginError(str(e).replace(f"{path.stem}.py:", f"{path.name}:", 1)) from e
        if plugin.name in seen or plugin.name in known_names:
            raise FwPluginError(f"{path.name}: 登録名 {plugin.name!r} が既に登録済みの FW プラグイン／アナライザと重複しています")
        seen.add(plugin.name)
        found.append(plugin)
    found.sort(key=lambda p: (p.order, p.name))
    return tuple(found)


def _validate_fw_plugin(plugin, owner: str, known_analyzer_names, *, upstream: bool) -> None:
    """FW プラグインの契約検証。違反は `FwPluginError`。`owner` はメッセージ用のファイル stem。"""
    if not isinstance(plugin, FwPlugin):
        raise FwPluginError(f"{owner}.py: FW_PLUGINS の要素は FwPlugin のインスタンスにしてください（実際: {type(plugin).__name__}）")
    if not isinstance(plugin.name, str) or ":" not in plugin.name:
        raise FwPluginError(f"{owner}.py: FwPlugin.name は '<prefix>:<fw>' の形の文字列にしてください（実際: {plugin.name!r}）")
    prefix, _, fw = plugin.name.partition(":")
    if not fw:
        raise FwPluginError(f"{owner}.py: FwPlugin.name の fw 部分が空です（実際: {plugin.name!r}）")
    if not _PREFIX_CHARSET_RE.match(prefix):
        raise FwPluginError(f"{owner}.py: prefix {prefix!r} は英小文字・数字・_のみ使えます（先頭は英小文字）")
    if not upstream:
        if prefix in RESERVED_ANALYZER_PREFIXES:
            raise FwPluginError(f"{owner}.py: prefix {prefix!r} は上流の予約名です")
        if not owner.startswith(prefix + "_"):
            raise FwPluginError(
                f"{owner}.py: FwPlugin.name の prefix {prefix!r} がファイル名と一致しません（ファイル名は '{prefix}_...' で始まる必要があります）")
    if not isinstance(plugin.languages, (set, frozenset)) or not plugin.languages \
            or not all(isinstance(x, str) and x for x in plugin.languages):
        raise FwPluginError(f"{owner}.py: {plugin.name}: languages は対象アナライザ名の非空の set/frozenset にしてください")
    unknown = sorted(set(plugin.languages) - set(known_analyzer_names))
    if unknown:
        raise FwPluginError(f"{owner}.py: {plugin.name}: languages に未登録のアナライザ名があります: {unknown}")
    if not isinstance(plugin.config_kinds, (set, frozenset)) \
            or not all(isinstance(x, str) and x for x in plugin.config_kinds):
        raise FwPluginError(f"{owner}.py: {plugin.name}: config_kinds は非空文字列の set/frozenset にしてください")
    if not isinstance(plugin.version, int) or isinstance(plugin.version, bool) or plugin.version < 1:
        raise FwPluginError(f"{owner}.py: {plugin.name}: version は 1 以上の int にしてください（実際: {plugin.version!r}）")
    if "version" not in vars(type(plugin)):
        raise FwPluginError(f"{owner}.py: {plugin.name}: version は拡張自身のクラスで宣言してください（継承値は署名の独立を破る）")
    if not isinstance(plugin.order, int) or isinstance(plugin.order, bool):
        raise FwPluginError(f"{owner}.py: {plugin.name}: order は int にしてください（実際: {plugin.order!r}）")


for _p in _UPSTREAM_FW_PLUGINS:
    _validate_fw_plugin(_p, "upstream", {a.name for a in _ANALYZERS}, upstream=True)

FW_PLUGINS: tuple[FwPlugin, ...] = _UPSTREAM_FW_PLUGINS + discover_fw_plugins()


def fw_plugins() -> tuple[FwPlugin, ...]:
    """登録済みの FW プラグイン（適用順＝`(order, 登録名)` の昇順）。呼び出しごとに `FW_PLUGINS` から並べる（キャッシュしない）。"""
    return tuple(sorted(FW_PLUGINS, key=lambda p: (p.order, p.name)))


def applicable_fw_plugins(analyzer: Analyzer, text: str, rel_path: str) -> tuple[FwPlugin, ...]:
    """`analyzer` が担当したファイルに適用する FW プラグイン（適用順）。

    条件: `analyzer.name` が `languages` に入る。`config_kinds` が非空なら `analyzer.config_kind()` がその集合に入る（`None`＝種別を判定できないファイルには適用しない）。
    """
    out = []
    kind_known = False
    kind = None
    for p in fw_plugins():
        if analyzer.name not in p.languages:
            continue
        if p.config_kinds:
            if not kind_known:
                kind, kind_known = analyzer.config_kind(text, rel_path), True
            if kind not in p.config_kinds:
                continue
        out.append(p)
    return tuple(out)


def _why(exc: Exception) -> str:
    return f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}"


def _check_items(items, cls, what: str) -> list:
    if not isinstance(items, list) or not all(isinstance(x, cls) for x in items):
        raise TypeError(f"{what} は {cls.__name__} のリストにしてください")
    return items


def _plugin_dropped(plugin: FwPlugin, dropped: list) -> list:
    return [Dropped(reason=f"{plugin.name}: {d.reason}", line=d.line, snippet=d.snippet) for d in dropped]


def _ctx_work(plugin: FwPlugin, build_ctx: dict | None) -> dict | None:
    """`uses_build_context` のプラグインへ渡す作業用の `ctx`（取り込み 1 回の dict の浅い写し）。プラグインは値を置き換えて使う（中身を直接変更しない）。成功したときだけ `_ctx_commit` で反映する。"""
    if not plugin.uses_build_context:
        return None
    return dict((build_ctx if build_ctx is not None else {}).get(plugin.name, {}))


def _ctx_commit(plugin: FwPlugin, build_ctx: dict | None, work: dict | None) -> None:
    if work is not None and build_ctx is not None:
        build_ctx[plugin.name] = work


def apply_fw_defs(plugins, text: str, rel_path: str, base: DefResult, build_ctx: dict | None = None) -> tuple[DefResult, list]:
    """本体の `DefResult` へ FW プラグイン（渡された順）の追加の定義を足す。`(統合した DefResult, [PluginFailure])`。

    プラグインには `base` のコピーを渡す（互いの出力・本体の出力は見えず、消せない）。同じ `(label, 識別子, 行)` の子と同じ `(label, 識別子)` の
    主体以外の定義は 1 件にする（本体が先）。例外を出したプラグインの出力は丸ごと捨て、`PluginFailure` で返す。
    """
    children, extras, dropped = list(base.children), list(base.extras), list(base.dropped)
    seen_children = {(c.label, c.key, c.line) for c in base.children}
    seen_extras = {(g.primary.label, g.primary.key) for g in base.extras}
    seen_keys = {(c.label, c.key) for c in base.children}
    failures: list = []
    for p in plugins:
        try:   # 検証・キーの生成・結合を一時の領域で終え、全部成功したときだけ反映する（失敗したら出力を丸ごと捨てる）
            work = _ctx_work(p, build_ctx)
            out = p.collect_defs(text, rel_path, copy.deepcopy(base), **({} if work is None else {"ctx": work}))
            if not isinstance(out, PluginDefs):
                raise TypeError("collect_defs は PluginDefs を返してください")
            p_children = _check_items(out.children, DefItem, "children")
            p_extras = _check_items(out.extras, DefGroup, "extras")
            p_dropped = _check_items(out.dropped, Dropped, "dropped")
            tmp_children, tmp_extras, t_seen_c, t_seen_e = [], [], set(seen_children), set(seen_extras)
            t_keys, dup_defs = set(seen_keys), []
            for c in p_children:
                k = (c.label, c.key, c.line)
                hash(k)
                if k in t_seen_c:
                    continue
                if (c.label, c.key) in t_keys:   # 同じ (種別, キー) は同じノードになる＝先の定義を残し、後のものは申告して捨てる
                    dup_defs.append(Dropped("plugin_duplicate_def", c.line or 0, str(c.key)[:80]))
                    continue
                t_seen_c.add(k)
                t_keys.add((c.label, c.key))
                tmp_children.append(c)
            for g in p_extras:
                _check_items(g.children, DefItem, "extras.children")
                k = (g.primary.label, g.primary.key)
                hash(k)
                if k not in t_seen_e:
                    t_seen_e.add(k)
                    tmp_extras.append(g)
            tmp_dropped = _plugin_dropped(p, p_dropped + dup_defs)
        except Exception as e:  # プラグインの失敗は本体の結果を残して申告する（呼び出し側が flags へ）
            failures.append(PluginFailure(p.name, "defs", _why(e)))
            continue
        _ctx_commit(p, build_ctx, work)
        children.extend(tmp_children)
        extras.extend(tmp_extras)
        seen_children, seen_extras, seen_keys = t_seen_c, t_seen_e, t_keys
        dropped.extend(tmp_dropped)
    return DefResult(primary=base.primary, children=children, extras=extras, dropped=dropped), failures


def _ref_key(r: RefCandidate) -> tuple:
    return (r.edge_type, r.kind, r.name, r.line, (r.extra or {}).get("via"), r.source_symbol_id, r.reverse)


def apply_fw_refs(plugins, text: str, rel_path: str, base_defs: DefResult, base_refs: RefResult,
                  types: TypeRelations | None = None, build_ctx: dict | None = None) -> tuple[RefResult, list, list]:
    """本体の `RefResult` へ FW プラグイン（渡された順）の追加の参照を足す。`(統合した RefResult, [PluginFailure], [PluginAmbiguity])`。

    重複（辺の種別・種別・名前・行・via・始点・向きが同じ）は 1 件（本体が先・先のプラグインが先）。`file_context` は本体のまま。失敗の扱いは `apply_fw_defs` と同じ。
    `types`＝型の関係の口（`uses_type_relations` のプラグインだけに渡す。それ以外は引けない口を渡す）。決まらなかった申告（`PluginAmbiguity`）は重複を除いてそのまま返す。
    """
    refs, dropped = list(base_refs.refs), list(base_refs.dropped)
    seen = {_ref_key(r) for r in refs}
    ambiguities: list = []
    seen_amb: set = set()
    failures: list = []
    for p in plugins:
        try:   # 検証・キーの生成・結合を一時の領域で終え、全部成功したときだけ反映する
            work = _ctx_work(p, build_ctx)
            out = p.extract_refs(text, rel_path, copy.deepcopy(base_defs), copy.deepcopy(base_refs),
                                 types if (p.uses_type_relations and types is not None) else TypeRelations(),
                                 **({} if work is None else {"ctx": work}))
            if not isinstance(out, PluginRefs):
                raise TypeError("extract_refs は PluginRefs を返してください")
            p_refs = _check_items(out.refs, RefCandidate, "refs")
            p_dropped = _check_items(out.dropped, Dropped, "dropped")
            p_amb = _check_items(out.ambiguous, PluginAmbiguity, "ambiguous")
            for a in p_amb:
                _check_items(a.candidates, TypeCandidate, "ambiguous.candidates")
            tmp_refs, tmp_amb, t_seen, t_seen_amb = [], [], set(seen), set(seen_amb)
            for r in p_refs:
                k = _ref_key(r)
                hash(k)
                if k not in t_seen:
                    t_seen.add(k)
                    tmp_refs.append(r)
            for a in p_amb:
                k = (a.kind, a.name, a.line, a.via, a.source_symbol_id)
                hash(k)
                if k not in t_seen_amb:
                    t_seen_amb.add(k)
                    tmp_amb.append(a)
            tmp_dropped = _plugin_dropped(p, p_dropped)
        except Exception as e:
            failures.append(PluginFailure(p.name, "refs", _why(e)))
            continue
        _ctx_commit(p, build_ctx, work)
        refs.extend(tmp_refs)
        ambiguities.extend(tmp_amb)
        seen, seen_amb = t_seen, t_seen_amb
        dropped.extend(tmp_dropped)
    return RefResult(refs=refs, dropped=dropped, file_context=base_refs.file_context), failures, ambiguities


# `accepts()`/`classify_document` の分類契約版。分類結果（kind/doctype/branch）に影響する意味変更があれば上げる（`config_signature()` の材料）。
CODE_ANALYZERS_SCHEMA_VERSION = 11  # 原本の文字コード・分類・固定形式の桁幅を含む解析契約の版
# 解析結果（抽出される定義・参照・cid）に影響する変更をしたときに上げる。上げると署名が変わり、world が再構築される。


def known_analyzers() -> tuple[Analyzer, ...]:
    """既知アナライザの一覧（優先順のまま）。"""
    return _ANALYZERS


def registered_extensions() -> frozenset:
    """全アナライザの担当拡張子の和集合（拡張子集合の単一の真実源）。"""
    exts: set = set()
    for a in _ANALYZERS:
        exts |= set(a.extensions)
    return frozenset(exts)


def config_signature() -> tuple:
    """現在の有効構成の署名（核の分類契約版＋登録順のアナライザごとの `(name, version, extensions)`＋適用順の FW プラグインごとの `(登録名, version, languages, config_kinds, order)`〔ルールファイルのプラグインは末尾にルールファイルのハッシュ・ルール ID と版〕）。

    world 署名・ES 設定署名の材料。構成や部品の `version` が変わると署名が変わり、標準の「署名不一致→再構築」で台帳・Neo4j・ES の `branch` が作り直される。呼び出しごとに `_ANALYZERS` から計算する（キャッシュしない）。
    """
    return (CODE_ANALYZERS_SCHEMA_VERSION,
            tuple((a.name, a.version, tuple(sorted(a.extensions))) for a in _ANALYZERS),
            tuple((p.name, p.version, tuple(sorted(p.languages)), tuple(sorted(p.config_kinds)), p.order)
                  + ((p.signature_extra,) if p.signature_extra else ())
                  for p in fw_plugins()))


def candidates(rel_path: str) -> tuple[Analyzer, ...]:
    """`rel_path` の拡張子を担当し得るアナライザ（優先順）。内容判定（`accepts`）は行わない。"""
    ext = _ext(rel_path)
    if not ext:
        return ()
    return tuple(a for a in _ANALYZERS if ext in a.extensions)


def resolve(rel_path: str, head_text: str = "") -> Analyzer | None:
    """`rel_path` の担当アナライザ（優先順で `accepts()` を通った最初のもの）。どれも通らなければ `None`（資料の枠へ倒す）。"""
    for a in candidates(rel_path):
        if a.accepts(rel_path, head_text):
            return a
    return None


def resolve_lazy(rel_path: str, read_head) -> Analyzer | None:
    """`resolve()` の遅延読み取り版。`accepts()` を上書きしている候補があるときだけ `read_head()` を呼ぶ。

    候補全員が既定の `accepts` なら先頭候補で確定し、`read_head` は呼ばれない。`read_head` は必要になるまでファイルを開かない callable で、`size` をキーワード専用で受け取れること。読む量は候補ごとの `head_bytes`（既定4KiB・`HtmlTemplateAnalyzer` は64KiB）に従う。
    """
    cands = candidates(rel_path)
    if not cands:
        return None
    overriding = [a for a in cands if _overrides_accepts(a)]
    if not overriding:
        return cands[0]
    # 候補ごとに自分の `head_bytes` 分だけを渡す（world_graph の Pass1 と同じ判定材料にする。大きい head の読み取り結果を小さい head の候補へ渡すと両経路で分類が食い違う）。同じサイズの読み取りは1回だけ。
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
