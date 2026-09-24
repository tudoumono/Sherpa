"""公開中の派生物を「世代」として問い合わせる（簡易版・2026-08-16）。

上流（feat/rag-gate）は世代IDを採番し、過去世代を残したまま切り替えるフル世代管理を持つ。
このブランチはそれを採らず、**作り切ってから改名2回で差し替える**簡易版だけを実装している
（`office_md.build_derived` の完了Gate）。公開中は常に1つで、過去世代は残らない。

その簡易版でも、後段の任意処理（OCR）には「いま公開されている派生物が、どの World 内容から
作られたか」を知る手段が要る。原本を再スキャンして比べるのは重く、しかも読んでいる間に原本が
変わりうる。そこで公開時に **World 署名（`worker.world_signature`）を派生物へ刻み**
（`office_md._WORLD_SIG_MARKER`）、それを世代IDとして扱う。

  上流のフル世代管理        このブランチ（簡易版）
  ------------------------  ----------------------------------------
  採番した generation ID    World 署名（内容が変われば必ず変わる）
  世代ディレクトリ          公開中の派生ディレクトリ（`md/`）1つだけ
  過去世代の保持            持たない（差し替え時に旧内容は消える）

キャッシュや「実行中に世代が変わっていないか」の判定としては、どちらも同じ意味で働く
（原本の中身が変われば鍵も変わる）。将来フル世代管理を入れるときは、ここが返す値を
採番済み世代IDに替えるだけで、呼び出し側（`ocr_worker`）は変えなくてよい。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .office_md import _WORLD_SIG_MARKER


def active_dir(derived_root: str | Path) -> Path:
    """公開中の派生物ディレクトリ（人間用 md 層）。簡易版では常に `md/` の1つだけ。

    `.world_sig`（このモジュールが世代IDとして扱うマーカー）は**現行位置のまま** md 層に
    刻まれる（§8.1 三階層のフォルダ分離後も、world 単位のマーカーは層に属さない状態として
    従来位置を維持する裁定）。RAG 正本／中間表現の物理置き場は `active_rag_dir`/`active_ir_dir`。
    """
    return Path(derived_root) / "md"


def active_rag_dir(derived_root: str | Path) -> Path:
    """公開中の RAG 正本＋証跡ディレクトリ（`rag/`・§8.1 三階層・`active_dir` の兄弟）。"""
    return Path(derived_root) / "rag"


def active_ir_dir(derived_root: str | Path) -> Path:
    """公開中の中間表現ディレクトリ（`ir/`・§8.1 三階層・`active_dir` の兄弟）。"""
    return Path(derived_root) / "ir"


def active_world_sig(derived_root: str | Path) -> str | None:
    """公開中の派生物に刻まれた World 署名そのもの（`worlds.last_sig` と同じ値）。"""
    marker = active_dir(derived_root) / _WORLD_SIG_MARKER
    try:
        value = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def generation_id_for(world_sig: str) -> str:
    """World 署名から世代IDを作る（64桁16進）。

    World 署名は SHA1（40桁）だが、OCR ジョブの世代IDは**64桁16進**という契約になっている
    （`store/ocr_jobs.py::_GENERATION_RE`）。桁が合わないと enqueue が弾かれるため、ここで
    決定的に写す。写像は一方向で衝突しないので、識別子としての意味（内容が変われば変わる）は同じ。

    **投入側と照合側は必ずこの関数を通す**こと。片方だけ生の署名を使うと、ジョブは積まれても
    「世代が変わった」と判定され続けて永久に処理されない。
    """
    return hashlib.sha256(world_sig.strip().encode("utf-8")).hexdigest()


def active_generation_id(derived_root: str | Path) -> str | None:
    """公開中の派生物の世代ID。刻まれていなければ None（＝不明）。

    None を「一致」とは決して扱わない側の契約に注意（`ocr_worker` は不一致として扱い、
    古い派生物に対して OCR 結果を書かない）。取り込み前の空ディレクトリや、簡易版導入前に
    作られた派生物がここに該当する。
    """
    sig = active_world_sig(derived_root)
    return generation_id_for(sig) if sig else None
