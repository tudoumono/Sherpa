"""`corpus_docs` とアナライザ登録簿の結線の単体テスト。

- 判定の一元化: `classify_document()`（`resolve_lazy` ベースの単一の判定）を `world_documents`・
  `scan_report`・`status_document_doctype` の3者が共有する——新規言語をレジストリに足す/
  `.txt` のような既存拡張子を上書きで受理するアナライザを差し込んでも、3者が**一致した**
  doctype/branch を返すことを確認する。
- 資料側フォールバックの拡張: `accepts()` が全滅した場合、既存の資料種別（md/office/txt/画像等）に
  該当すれば資料として扱う（`scan_report.analyzer_declined_as_document` で可視化）。該当しなければ
  従来どおり未対応（`scan_report.analyzer_declined` で可視化・§7 裁定10）。
- 読み取り失敗の明示化: 内容判定に必要なヘッダが読み取れない場合は `state="unreadable"`
  （`status_document_doctype` は `_UNREADABLE_DOCTYPE_LABEL`）という明示の失敗にし、
  他のアナライザへ誤配属しない。
"""
from __future__ import annotations

from pathlib import Path

from sherpa import corpus_docs, worlds
from sherpa.ingest import worker
from sherpa.ingest.analyzers import registry
from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult


def _world(monkeypatch, tmp_path):
    wd = tmp_path / "world"
    wd.mkdir()
    der = tmp_path / "derived"
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    return wd, der


class _DummyLangAnalyzer(Analyzer):
    """新規言語を模したダミー（`.dummy` を担当・既定 accepts のまま・doctype は独自の表示名）。"""

    name = "dummylang"
    extensions = frozenset({".dummy"})
    doctype = "ダミー言語"

    def collect_defs(self, text, rel_path):
        return DefResult()

    def extract_refs(self, text, rel_path):
        return RefResult()


def test_new_analyzer_registration_is_picked_up_by_doctype_map_without_code_changes(monkeypatch):
    """レジストリにアナライザを足すだけで `_doctype_map()`（後方互換の `_DOCTYPE` 属性・§2.4）
    と `status_document_doctype` に反映される（拡張子・doctype とも固定 dict の手動更新が要らない）。
    `.dummy` は既定 accepts のみ＝`read_head` は呼ばれないので世界名はダミーで安全。"""
    monkeypatch.setattr(registry, "_ANALYZERS", (*registry.known_analyzers(), _DummyLangAnalyzer()))
    assert corpus_docs._doctype_map()[".dummy"] == "ダミー言語"
    assert corpus_docs.status_document_doctype("x.dummy", "w") == "ダミー言語"
    # 後方互換の `_DOCTYPE` 属性アクセスも新規言語を含む（__getattr__ 経由）。
    assert corpus_docs._DOCTYPE[".dummy"] == "ダミー言語"


def test_new_analyzer_registration_appears_in_world_documents_and_scan_report(monkeypatch, tmp_path):
    """ダミー言語ファイルを実際に置くと、台帳・scan_report の両方に新しい doctype で現れる。
    `analyzer`（`Analyzer.name`＝"dummylang"）は `doctype`（"ダミー言語"）とは独立した値——
    両者が異なる `_DummyLangAnalyzer` で固定し、一覧応答の `analyzer` が `doctype` の別名に
    倒れていないことを確認する（§7 裁定2の受入条件）。"""
    monkeypatch.setattr(registry, "_ANALYZERS", (*registry.known_analyzers(), _DummyLangAnalyzer()))
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "thing.dummy").write_text("何か新言語のソース", encoding="utf-8")

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["thing.dummy"]
    assert docs[0]["doctype"] == "ダミー言語" and docs[0]["branch"] == "source"
    assert docs[0]["analyzer"] == "dummylang"          # name≠doctype を直接固定

    rep = corpus_docs.scan_report("w")
    assert rep["by_doctype"] == {"ダミー言語": 1} and rep["indexed"] == 1 and rep["skipped_other"] == 0
    assert rep["analyzer_declined"] == 0 and rep["unreadable"] == 0


class _AcceptTxtAnalyzer(Analyzer):
    """`.txt` を上書きで受理するアナライザ（`accepts` を明示オーバーライド＝常に真）。

    既存の非コード拡張子（`.txt`）を新しい言語が正式に受理した、という現実的なシナリオを模す。
    """

    name = "accept_txt"
    extensions = frozenset({".txt"})
    doctype = "受理された言語"

    def accepts(self, rel_path, head_text=""):
        return True

    def collect_defs(self, text, rel_path):
        return DefResult()

    def extract_refs(self, text, rel_path):
        return RefResult()


def test_txt_accepting_analyzer_is_consistent_across_listing_scan_and_status(monkeypatch, tmp_path):
    """`.txt` を受理するアナライザがいれば、台帳・scan_report・status の3者が**一致して**
    そのアナライザの doctype/branch=source を返す。"""
    monkeypatch.setattr(registry, "_ANALYZERS", (_AcceptTxtAnalyzer(),))
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "note.txt").write_text("本文", encoding="utf-8")

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["note.txt"]
    assert docs[0]["doctype"] == "受理された言語" and docs[0]["branch"] == "source"

    rep = corpus_docs.scan_report("w")
    assert rep["by_doctype"] == {"受理された言語": 1} and rep["indexed"] == 1
    assert rep["analyzer_declined"] == 0                # 受理された＝declined ではない

    assert corpus_docs.status_document_doctype("note.txt", "w") == "受理された言語"


class _AlwaysDeclineTxtAnalyzer(Analyzer):
    """`.txt` を要求するが `accepts()` が常に偽の不正アナライザ。"""

    name = "decline_txt"
    extensions = frozenset({".txt"})

    def accepts(self, rel_path, head_text=""):
        return False

    def collect_defs(self, text, rel_path):
        return DefResult()

    def extract_refs(self, text, rel_path):
        return RefResult()


def test_accepts_all_reject_falls_back_to_document_branch_not_silently_uncounted(monkeypatch, tmp_path):
    """`.txt` を主張するが `accepts()` が全滅するアナライザがいても、
    従来どおり資料（branch=office・doctype=テキスト）として台帳・scan_report・status に載る
    （コード扱いにも「その他」にもしない＝§7 裁定10）。`.txt` は既存の資料種別に該当するため
    `analyzer_declined_as_document`（資料扱い）で可視化し、`analyzer_declined`（未対応）は増えない。"""
    monkeypatch.setattr(registry, "_ANALYZERS", (_AlwaysDeclineTxtAnalyzer(), *registry.known_analyzers()))
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "note.txt").write_text("本文", encoding="utf-8")

    assert registry.resolve("note.txt", "本文") is None       # 直接確認: accepts 全滅で担当なし

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["note.txt"]
    assert docs[0]["doctype"] == "テキスト" and docs[0]["branch"] == "office"
    assert corpus_docs.status_document_doctype("note.txt", "w") == "テキスト"

    rep = corpus_docs.scan_report("w")
    assert rep["indexed"] == 1 and rep["skipped_other"] == 0
    assert rep["by_doctype"] == {"テキスト": 1}
    assert rep["analyzer_declined_as_document"] == 1           # 担当は居たが全滅＝資料として内訳に残す
    assert rep["analyzer_declined"] == 0                       # 未対応（不採用）ではない


class _AlwaysDeclineDatAnalyzer(Analyzer):
    """`.dat`（資料表にも軽量テキスト枠にも無い拡張子）を要求するが `accepts()` が常に偽の不正アナライザ。"""

    name = "decline_dat"
    extensions = frozenset({".dat"})

    def accepts(self, rel_path, head_text=""):
        return False

    def collect_defs(self, text, rel_path):
        return DefResult()

    def extract_refs(self, text, rel_path):
        return RefResult()


def test_declined_extension_not_in_document_table_falls_to_other_but_is_visible(monkeypatch, tmp_path):
    """資料表（`.md`/`.txt` 等）にも軽量テキスト枠（`ingest.text_kind`）にも無い拡張子（`.dat`）で
    `accepts()` が全滅し、かつ内容もバイナリ（軽量テキスト枠の第2段が `binary` 判定）なら、
    コードとして誤って indexed にはならず「その他」（未対応）へ落ちる——`analyzer_declined`
    （未対応）の内訳が見える（§7 裁定10「既存の資料種別に該当するものは資料・それ以外は未対応」）。
    `analyzer_declined_as_document`（資料扱い）は増えない。"""
    monkeypatch.setattr(registry, "_ANALYZERS", (_AlwaysDeclineDatAnalyzer(),))
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "manifest.dat").write_bytes(b"\x00\x01binary fixture")

    rep = corpus_docs.scan_report("w")
    assert rep["indexed"] == 0                                 # コードとして誤カウントされない
    assert rep["skipped_other"] == 1 and rep["skipped_ext"] == {".dat": 1}
    assert rep["analyzer_declined"] == 1                       # 「担当なし（未対応）」の可視化
    assert rep["analyzer_declined_as_document"] == 0           # 資料扱いではない


class _AlwaysDeclineDocxAnalyzer(Analyzer):
    """`.docx`（Office＝既存の資料種別）を要求するが `accepts()` が常に偽の不正アナライザ。"""

    name = "decline_docx"
    extensions = frozenset({".docx"})

    def accepts(self, rel_path, head_text=""):
        return False

    def collect_defs(self, text, rel_path):
        return DefResult()

    def extract_refs(self, text, rel_path):
        return RefResult()


class _AlwaysDeclinePngAnalyzer(Analyzer):
    """`.png`（画像＝既存の資料種別）を要求するが `accepts()` が常に偽の不正アナライザ。"""

    name = "decline_png"
    extensions = frozenset({".png"})

    def accepts(self, rel_path, head_text=""):
        return False

    def collect_defs(self, text, rel_path):
        return DefResult()

    def extract_refs(self, text, rel_path):
        return RefResult()


def test_office_and_image_declined_count_as_document_not_unsupported(monkeypatch, tmp_path):
    """`.docx`／`.png`／`.txt`／`.dat` の4種を同時に `accepts()` 全滅させ、内訳が契約どおりに
    分かれることを確認する: 既存の資料種別（Office/画像/txt）に該当する3件は
    `analyzer_declined_as_document`（資料として使える＝indexed に乗る）、資料種別にも軽量
    テキスト枠にも該当しない（内容もバイナリ）`.dat` だけが `analyzer_declined`（未対応）に残る。
    Office/画像判定を確定してから振り分けることで、資料として使える文書を「未対応」に誤集計
    しない（§7 裁定10）。"""
    monkeypatch.setattr(registry, "_ANALYZERS", (
        _AlwaysDeclineDocxAnalyzer(), _AlwaysDeclinePngAnalyzer(),
        _AlwaysDeclineTxtAnalyzer(), _AlwaysDeclineDatAnalyzer()))
    wd, der = _world(monkeypatch, tmp_path)
    (wd / "spec.docx").write_bytes(b"docx fixture")
    (der / "spec.docx.md").write_text("本文", encoding="utf-8")     # 変換成功＝資料として使える
    (wd / "scan.png").write_bytes(b"png fixture")
    (der / "scan.png.md").write_text("画像内容は未解釈である。", encoding="utf-8")
    (wd / "note.txt").write_text("本文", encoding="utf-8")
    (wd / "manifest.dat").write_bytes(b"\x00\x01binary fixture")

    rep = corpus_docs.scan_report("w")

    assert rep["indexed"] == 3                                  # docx/png/txt は使える
    assert rep["skipped_other"] == 1 and rep["skipped_ext"] == {".dat": 1}
    assert rep["analyzer_declined"] == 1                        # 未対応は dat だけ
    assert rep["analyzer_declined_as_document"] == 3            # 資料扱いは docx/png/txt
    assert rep["office_md"] == 1
    assert rep["by_doctype"] == {"Word": 1, "画像": 1, "テキスト": 1}

    # 資料扱いの3件は一覧にも載る（未対応の dat だけ列挙されない・既存契約）。
    assert {d["name"] for d in corpus_docs.world_documents("w")} == {"spec.docx", "scan.png", "note.txt"}


def test_resolve_lazy_read_head_is_only_invoked_for_registered_extensions(monkeypatch, tmp_path):
    """`iter_world_documents` は登録済み拡張子のファイルだけを開いて内容判定する
    （非コード拡張子の大量ファイルを走査してもファイル I/O が増えないことの確認）。"""
    monkeypatch.setattr(registry, "_ANALYZERS", (_AlwaysDeclineTxtAnalyzer(),))   # .txt だけが「候補あり」
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "note.txt").write_text("本文", encoding="utf-8")
    (wd / "note.md").write_text("設計書", encoding="utf-8")     # 登録済みでない拡張子

    reads = []
    orig_read_head = corpus_docs._read_head

    def _tracking_read_head(rp, size=4096):
        reads.append(rp.name)
        return orig_read_head(rp, size)

    monkeypatch.setattr(corpus_docs, "_read_head", _tracking_read_head)
    list(corpus_docs.iter_world_documents("w"))
    assert reads == ["note.txt"]                                # .md は候補が無いので read_head は呼ばれない


class _OverrideAcceptZzAnalyzer(Analyzer):
    """`.zz` を担当し `accepts` を明示オーバーライド（常に真）——`resolve_lazy` に内容読み取りを
    強制させるためだけのフェイク（既定 accepts のままだと read_head が一度も呼ばれず、
    読み取り失敗を再現できない）。
    """

    name = "override_zz"
    extensions = frozenset({".zz"})
    doctype = "override"

    def accepts(self, rel_path, head_text=""):
        return True

    def collect_defs(self, text, rel_path):
        return DefResult()

    def extract_refs(self, text, rel_path):
        return RefResult()


def _break_open_for(monkeypatch, filename: str):
    """`Path.open` を `filename` という名前のファイルだけ失敗させる（他は素通し・対象ファイル限定必須）。"""
    real_open = Path.open

    def _boom_open(self, *a, **kw):
        if self.name == filename:
            raise OSError("simulated read failure")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", _boom_open)


def test_unreadable_head_produces_explicit_state_in_listing_and_scan_report(monkeypatch, tmp_path):
    """内容判定に必要なヘッダが読めない場合、列挙結果は `state="unreadable"`（doctype/branch は
    None）になり、次点アナライザへ誤配属しない（判定を打ち切る）。scan_report も `unreadable`
    カウンタで可視化し、`indexed`/`skipped_other` のどちらにも数えない。"""
    monkeypatch.setattr(registry, "_ANALYZERS", (_OverrideAcceptZzAnalyzer(),))
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "bad.zz").write_text("dummy", encoding="utf-8")
    _break_open_for(monkeypatch, "bad.zz")

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["bad.zz"]
    assert docs[0]["state"] == "unreadable" and docs[0]["reason"] == "read_failed"
    assert docs[0]["doctype"] is None and docs[0]["branch"] is None

    rep = corpus_docs.scan_report("w")
    assert rep["unreadable"] == 1
    assert rep["indexed"] == 0 and rep["skipped_other"] == 0    # 誤って別カテゴリに数えない


def test_unreadable_head_produces_explicit_label_in_status_document_doctype(monkeypatch, tmp_path):
    """`status_document_doctype` も読み取り失敗を明示のラベルにする（`None` にはしない＝
    原本自体は存在するため対象外の付帯物と区別する）。"""
    monkeypatch.setattr(registry, "_ANALYZERS", (_OverrideAcceptZzAnalyzer(),))
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "bad.zz").write_text("dummy", encoding="utf-8")
    _break_open_for(monkeypatch, "bad.zz")

    assert corpus_docs.status_document_doctype("bad.zz", "w") == corpus_docs._UNREADABLE_DOCTYPE_LABEL
    # 存在する原本として数える（対象外の付帯物＝None とは区別する）。
    assert corpus_docs.manifest_doctype_count(["bad.zz"], "w") == 1


# ===================================================================
# アナライザ構成署名（`registry.config_signature()`）を world 署名の材料に畳み込む
# （既存データ移行の代替＝`importance.IMPORTANCE_SCHEMA_VERSION` と同じ流儀）。SQL アナライザ追加や
# CODE-1b の有効/無効・並び替えのいずれかで構成が変われば、標準の「署名不一致→全再構築」経路で
# 台帳・Neo4j・ES の branch（confirmed 判定）が自動的に作り直される。純粋な `config_signature()`
# 自体の性質（拡張子/順序変化への反応）は `tests/unit/analyzers/test_registry.py` で検証する——
# ここでは world 署名（`worker.world_signature_of_root`）への配線だけを確認する。
# ===================================================================

def test_world_signature_changes_when_analyzer_config_signature_changes(monkeypatch, tmp_path):
    """`registry.config_signature()` は world 署名の材料に含まれる——構成が変わると、ソース
    ファイル自体が不変でも署名が変わる。同じ構成なら署名は再現する。"""
    wd = tmp_path / "world"
    wd.mkdir()
    (wd / "a.md").write_text("x", encoding="utf-8")

    monkeypatch.setattr(registry, "_ANALYZERS", (CobolAnalyzerStub := registry.known_analyzers()[0],))
    sig_v1 = worker.world_signature_of_root(wd)

    monkeypatch.setattr(registry, "_ANALYZERS", (CobolAnalyzerStub, _DummyLangAnalyzer()))
    sig_v2 = worker.world_signature_of_root(wd)
    assert sig_v2 != sig_v1

    monkeypatch.setattr(registry, "_ANALYZERS", (CobolAnalyzerStub,))
    assert worker.world_signature_of_root(wd) == sig_v1, "同じ構成・同じ内容なら署名は再現する"
