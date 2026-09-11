#!/usr/bin/env python3
"""`make verify-extension` — 拡張面ごとの契約検査（拡張の契約 S4・docs/21-拡張の契約.md）。

提案書 §9「拡張面の契約表」の順（アナライザ／頭脳 provider／変換アーム／MCP ツール）で、
行ごとに「確認した契約」または「未確認」を出力する。**アナライザ**だけは本スライスの実装対象
（発見規約・接頭辞・拡張子衝突・版・`accepts/collect_defs/extract_refs` の戻り値型・
`config_signature()` への反映）を実際に検査し、違反があれば非ゼロで終了する。頭脳 provider／
変換アーム／MCP ツールは**登録簿の実在と件数の表示だけ**を行い「未確認」と明示する（保証済みと
言わない——契約テストは既存の seams/surface/office_md/mcp テストが担う・§9 参照）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _check_analyzers(registry, base) -> list[str]:
    """アナライザ面の契約検査。戻り値は違反メッセージの一覧（空＝違反なし）。`registry`/`base` は
    呼び出し元（`main()`）が発見時の契約違反を先に捕まえたうえで渡す（モジュール import 自体が
    `ExtensionAnalyzerError` で失敗し得るため、トップレベル import ではなく引数で受け取る）。"""
    violations: list[str] = []

    # (1) 発見・接頭辞・拡張子衝突・version の契約は `discover_extension_analyzers()` 自体が
    # import 時に検証済み（違反があれば `main()` が既に `NG:` を出して終えている）。ここでは
    # 実際に走らせて確認する——本番ディレクトリを再度発見させ、現行の `_ANALYZERS` に含まれる
    # 拡張アナライザと一致することを確認する（発見結果が安定していることの検査）。
    try:
        rediscovered = registry.discover_extension_analyzers()
    except registry.ExtensionAnalyzerError as e:
        violations.append(f"拡張アナライザの発見時契約違反: {e}")
        rediscovered = ()

    known = registry.known_analyzers()
    extension_analyzers = [a for a in known if ":" in a.name]
    if [a.name for a in rediscovered] != [a.name for a in extension_analyzers]:
        violations.append(
            "discover_extension_analyzers() の結果が _ANALYZERS 末尾の拡張アナライザ構成と "
            f"一致しません（発見: {[a.name for a in rediscovered]}・登録済み: "
            f"{[a.name for a in extension_analyzers]}）")

    # (2) accepts が bool を・collect_defs/extract_refs が DefResult/RefResult の**インスタンス**を
    # 返すこと（全登録アナライザ対象・空文字＋ダミー拡張子のインラインサンプルで実行する軽量な型検査。
    # 実際の構文解析の正しさは各アナライザの golden テストが担う——ここは「契約どおりの型を返すか」
    # だけを見る。`isinstance` を使う——型名の文字列比較では同名の偽物クラスを見逃す）。
    for a in known:
        if not a.extensions:
            violations.append(f"{a.name}: extensions が空です")
            continue
        ext = sorted(a.extensions)[0]
        rel = f"x{ext}"
        try:
            accepted = a.accepts(rel, "")
        except Exception as e:
            violations.append(f"{a.name}: accepts() が例外を送出しました（{e!r}）")
        else:
            if not isinstance(accepted, bool):
                violations.append(f"{a.name}: accepts() が bool を返しませんでした（{accepted!r}）")
        try:
            dr = a.collect_defs("", rel)
        except Exception as e:
            violations.append(f"{a.name}: collect_defs() が例外を送出しました（{e!r}）")
        else:
            if not isinstance(dr, base.DefResult):
                violations.append(f"{a.name}: collect_defs() が DefResult を返しませんでした（{type(dr)!r}）")
        try:
            rr = a.extract_refs("", rel)
        except Exception as e:
            violations.append(f"{a.name}: extract_refs() が例外を送出しました（{e!r}）")
        else:
            if not isinstance(rr, base.RefResult):
                violations.append(f"{a.name}: extract_refs() が RefResult を返しませんでした（{type(rr)!r}）")

    # (3) config_signature() に各部品（name, version, extensions）が畳み込まれていること。
    sig_names = {item[0] for item in registry.config_signature()[1]}
    missing_in_sig = {a.name for a in known} - sig_names
    if missing_in_sig:
        violations.append(f"config_signature() に含まれないアナライザ: {sorted(missing_in_sig)}")

    return violations


def _report_analyzers(registry, base) -> tuple[list[str], list[str]]:
    """`(確認した契約の説明一覧, 違反一覧)`。"""
    violations = _check_analyzers(registry, base)
    known = registry.known_analyzers()
    extension_names = [a.name for a in known if ":" in a.name]
    confirmed = [
        f"発見規約（`<prefix>_*.py`・接頭辞一致・拡張子衝突・version>=1）: 登録済み拡張アナライザ "
        f"{len(extension_names)} 件（{', '.join(extension_names) or 'なし'}）",
        f"accepts() が bool を・collect_defs/extract_refs が DefResult/RefResult のインスタンスを返す: "
        f"登録アナライザ全 {len(known)} 件で確認",
        f"config_signature() に部品が載る: 登録アナライザ全 {len(known)} 件で確認",
    ]
    return confirmed, violations


def _report_providers() -> list[str]:
    from sherpa.providers import AGENT_PROVIDERS
    return [f"登録簿の実在: `AGENT_PROVIDERS` {len(AGENT_PROVIDERS)} 件"
            f"（{', '.join(sorted(AGENT_PROVIDERS))}）"]


def _report_arms() -> list[str]:
    from sherpa.ingest import arms
    names = arms.known_arm_names()
    return [f"登録簿の実在: 既知アーム {len(names)} 件（{', '.join(names)}）"]


def _report_mcp_tools() -> list[str]:
    try:
        from sherpa import mcp_server
        defs = mcp_server._tool_defs()
        names = [d["name"] for d in defs]
        return [f"登録簿の実在: 公開ツール定義 {len(names)} 件（{', '.join(names)}）"]
    except Exception as e:
        return [f"登録簿の実在: 取得に失敗しました（{e!r}・環境依存のため未確認のまま扱う）"]


def main(argv: list[str] | None = None) -> int:
    exit_code = 0
    discovery_ok = True

    print("## アナライザ")
    # `registry` の import 自体が module top-level の `discover_extension_analyzers()` 呼び出しで
    # 発見時契約違反（`ExtensionAnalyzerError`）を送出し得る——ここで捕まえて `NG:` 行を出す（従来は
    # import 時に traceback で落ち、`NG:` が一度も出ないまま終了していた・docs/21-拡張の契約.md §5
    # の記述と不一致だった）。import 失敗時は `registry` 名が束縛されないため例外クラスを型として
    # 参照できず、クラス名で判定する（それ以外の予期しない失敗は隠さず再送出する）。
    try:
        from sherpa.ingest.analyzers import registry
        from sherpa.ingest.analyzers import _base as base
    except Exception as e:
        if type(e).__name__ != "ExtensionAnalyzerError":
            raise
        exit_code = 1
        discovery_ok = False
        print(f"NG: 拡張アナライザの発見時契約違反: {e}")
    else:
        confirmed, violations = _report_analyzers(registry, base)
        for line in confirmed:
            print(f"確認した契約: {line}")
        if violations:
            exit_code = 1
            for v in violations:
                print(f"NG: {v}")
    print()

    # 発見失敗（`ExtensionAnalyzerError`）のときは残り3面を評価しない——`registry` に依存する
    # import（provider/アーム/MCP ツールの登録簿）が同じ発見処理を再度走らせて同じ例外を再発させ
    # うるため（アナライザ面のディレクトリは3面と無関係に壊れている可能性がある＝評価不能）。
    for heading, report_fn, note in (
        ("## 頭脳 provider", _report_providers,
         "未確認（契約テストは既存の tests/unit/test_agents_seams.py・test_agents_surface.py が担う）"),
        ("## 変換アーム", _report_arms, "未確認（契約テストは既存の test_office_md* が担う）"),
        ("## MCP ツール", _report_mcp_tools,
         "未確認（契約テストは既存の test_mcp_*・test_codex_mcp_concurrency が担う）"),
    ):
        print(heading)
        if discovery_ok:
            for line in report_fn():
                print(line)
            print(note)
        else:
            print("未確認（アナライザ面の違反により未評価）")
        print()

    print("NG（アナライザ面に契約違反あり）" if exit_code else "OK（アナライザ面は契約違反なし）")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
