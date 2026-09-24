"""閉域向け「頭脳（AI）の選び方」案内の食い違い是正（2026-08-18・実機報告⑥）。

不具合: `sherpa/agent_constructs.py` の契約では、選べる頭脳は標準3つ（openai/ollama/codex）＋
`SHERPA_EXTRA_AGENTS`（カンマ区切り env）で明示的に有効化した追加分（gemini/bedrock/heuristic）
だけであり、`SHERPA_AGENT` が `enabled_agents()` に無い値なら既定へ戻る（`default_agent()`）。
ところが出荷している閉域向け案内（`.env.example` / `scripts/install_offline_kit.sh` /
`docs/manual/offline-kit.md`）は `SHERPA_EXTRA_AGENTS` を併記せず `SHERPA_AGENT=heuristic` だけを
勧めていたため、案内どおりに設定しても heuristic は選ばれず codex へ倒れていた（Codex CLI が無い
閉域ホストでは「設定を忘れたのに AI が動かない」の根本原因になっていた）。同時に、キットは Codex
CLI を同梱し `OPENAI_API_KEY` があれば導入時に認証まで自動で済ませるようになったため、旧案内の
「OPENAI_API_KEY 等は空のままにする」も実態と合わなくなっていた。

ENV-ONE（env 例の1本化・2026-09-03）: 開発機向け `.env.example` と本番向け env 例ファイル
（撤去済み）の2ファイルを唯一の `.env.example` へ統合した（選び間違い＝閉域網へ dev 用をそのまま
持ち込む事故を防ぐため）。これに伴い、旧ファイルが担っていた「閉域キット導入時の Codex CLI
自己認証専用の `OPENAI_API_KEY=`（空の有効行）」「単独では選べない頭脳の案内（heuristic 等）」の
契約は `.env.example` 側へ引き継いだ——アプリ本体の OpenAI キー（`OPENAI_API_KEY` 等は system_settings
管理画面が唯一の設定先）とは別経路であることは変わらない。

このテストは:
  - 出荷する案内が `SHERPA_AGENT=heuristic` を勧めるときは、必ず同じ案内文の中に
    `SHERPA_EXTRA_AGENTS=heuristic` も併記していることを固定する（将来また食い違えば落ちる）。
  - `.env.example` に、単独では選べない `SHERPA_AGENT=heuristic` を**有効行**として
    残していないことを固定する。
  - 各案内が閉域で実際に動く3通り（OpenAI直結/Codex・ローカルLLM(Ollama)・簡易AIなし）を
    読み取れる語彙を含んでいることを固定する。
  - `sherpa/agent_constructs.py` / `sherpa/providers/__init__.py` の実装は本チケットの対象外
    （このテストは案内文だけを見る。実装の契約自体は tests/unit/test_default_agent_autoselect.py）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"
INSTALL_KIT = ROOT / "scripts" / "install_offline_kit.sh"
MANUAL = ROOT / "docs" / "manual" / "offline-kit.md"

GUIDANCE_FILES = [ENV_EXAMPLE, INSTALL_KIT, MANUAL]


def _assert_heuristic_paired(text: str, label: str) -> None:
    """`SHERPA_AGENT=heuristic` を勧める箇所には必ず `SHERPA_EXTRA_AGENTS=heuristic` も
    書かれていること（片方だけだと enabled_agents() に無い値として無視され、既定へ倒れる）。"""
    if "SHERPA_AGENT=heuristic" not in text:
        return
    assert "SHERPA_EXTRA_AGENTS=heuristic" in text, (
        f"{label}: SHERPA_AGENT=heuristic を勧めているのに SHERPA_EXTRA_AGENTS=heuristic の併記が"
        "見当たらない（片方だけだと選べない値として無視され、既定（codex）へ倒れる）"
    )


@pytest.mark.parametrize("path", GUIDANCE_FILES, ids=[p.name for p in GUIDANCE_FILES])
def test_heuristic_guidance_always_paired_with_extra_agents(path: Path):
    _assert_heuristic_paired(path.read_text(encoding="utf-8"), path.name)


def test_env_example_has_no_active_unselectable_heuristic_line():
    """`.env.example` の**有効行**（コメントでない行）に、単独では選べない
    `SHERPA_AGENT=heuristic` を置かないこと（案内としてコメントアウトされているのは可）。"""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    active_lines = [ln.strip() for ln in text.splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]
    assert "SHERPA_AGENT=heuristic" not in active_lines, (
        ".env.example に有効な SHERPA_AGENT=heuristic 行がある"
        "（SHERPA_EXTRA_AGENTS を有効化しない限り選べない値＝実際には codex に倒れる）"
    )


def test_env_example_documents_three_working_configurations():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" in text
    assert "SHERPA_AGENT=ollama" in text
    assert "SHERPA_EXTRA_AGENTS=heuristic" in text


def test_env_example_openai_key_is_an_empty_active_line_not_a_placeholder():
    """RV HIGH（2026-08-18 指摘2）の再発防止（ENV-2 で一度 `.env.example` 側は鍵行を全撤去したが、
    ENV-ONE（2026-09-03・env 例の1本化）で `.env.example` は閉域キット導入時の Codex CLI 認証専用
    として `OPENAI_API_KEY=` の有効行を（旧・本番向け env 例ファイルから引き継いで）持つ契約に
    なった＝この1行だけはプレースホルダ混入を引き続き固定で防ぐ必要がある）。
    `OPENAI_API_KEY=sk-REPLACE_ME` 等が有効行だと `agent_constructs._auto_default_agent()`／
    `_select_provider` の openai 分岐が「キーあり」と誤認し、実行時に 401 で失敗する。"""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    active_lines = [ln.strip() for ln in text.splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]
    key_lines = [ln for ln in active_lines if ln.startswith("OPENAI_API_KEY=")]
    assert key_lines == ["OPENAI_API_KEY="], (
        f".env.example の OPENAI_API_KEY 有効行がプレースホルダ/非空/欠落/重複: {key_lines!r}"
    )


def test_install_kit_no_longer_tells_users_to_leave_openai_key_blank():
    """本キットは Codex CLI を同梱し、OPENAI_API_KEY があれば導入時に認証まで自動で行う
    （7b・sherpa_codex_ensure_auth）。「OPENAI_API_KEY 等は空のままにする」という実態と食い違う
    旧文言を**利用者向けの案内（echo 行）**として復活させていないこと（`#` コメント側でこの是正の
    経緯として旧文言を引用するのは対象外＝コメント行は除外して検査する）。"""
    text = INSTALL_KIT.read_text(encoding="utf-8")
    echo_lines = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
    assert "OPENAI_API_KEY 等は空のままにする" not in echo_lines


def test_install_kit_documents_three_working_configurations():
    text = INSTALL_KIT.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" in text
    assert "SHERPA_AGENT=ollama" in text
    assert "SHERPA_EXTRA_AGENTS=heuristic" in text


def test_manual_documents_three_working_configurations():
    text = MANUAL.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" in text
    assert "SHERPA_AGENT=ollama" in text
    assert "SHERPA_EXTRA_AGENTS=heuristic" in text


def test_env_example_notes_extra_agents_required_alongside_agent():
    """`.env.example` にも、選択肢に無い頭脳（heuristic/gemini/bedrock）を既定にするには
    SHERPA_EXTRA_AGENTS も要ることが SHERPA_AGENT の直近のコメントから読み取れること。"""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    idx_agent = text.index("SHERPA_AGENT=codex")
    window = text[max(0, idx_agent - 400): idx_agent]
    assert "SHERPA_EXTRA_AGENTS" in window


def test_env_example_has_no_active_agent_line_so_autoselect_applies():
    """RV HIGH（2026-08-18 指摘2）: `cp .env.example .env` の通常導線で `SHERPA_AGENT=codex` が
    有効行のままだと、自動選択（`agent_constructs._auto_default_agent`）が一度も働かない。
    既定はコメントアウトであること。"""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    active_lines = [ln.strip() for ln in text.splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]
    agent_lines = [ln for ln in active_lines if ln.startswith("SHERPA_AGENT=")]
    assert not agent_lines, f".env.example に有効な SHERPA_AGENT= 行がある（自動選択を殺す）: {agent_lines!r}"
