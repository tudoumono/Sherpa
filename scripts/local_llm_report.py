"""回答のモデルをローカル LLM（Ollama）にしたときに、回答が浅い・エラーになる原因を切り分ける調べ（`scripts/local_llm_report.sh`）。

入力: ① Codex のセッション記録（<users>/<利用者>/workspace/.codex-sessions/<会話番号>/sessions/…/rollout-*.jsonl）
      ② Ollama の `/api/ps`・`/api/version` ③ Ollama のログ
手順: ① 記録を 1 ターン（rollout 1 本）ずつ数える ② 数字から判定を立てる（線は冒頭の定数）
      ③ 画面の最後に、口頭で読み上げられる番号つきの「伝える欄」を出す。詳細は txt に保存してパスだけ出す。
守ること: 標準ライブラリだけ・`sherpa` を import しない・読み取りだけ（書くのは詳細の txt 1 つ）。
         質問や回答の文・資料のファイル名・パス・ホスト名・利用者名・鍵を出力に出さない（道具の名前と数字だけ）。
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

# 判定の線（数だけ。特定の質問・資料の形に合わせた条件は足さない）
RATIO_TIGHT = 0.9          # 入力の最大 ÷ 文脈長 がこれ以上＝枠がぎりぎり
RATIO_FIRST_HEAVY = 0.5    # 入力の最初 ÷ 文脈長 がこれ以上＝最初の指示で枠の大半
RATIO_ROOM = 0.5           # 入力の最大 ÷ 文脈長 がこれ未満＝枠に余裕がある
FAIL_RATIO = 0.3           # 道具の失敗 ÷ 呼び出し総数 がこれ以上＝失敗が多い
RECOMMEND_CTX = 64000      # 文脈長の目安（これ以上にして流し直す）
DEFAULT_TURNS = 5
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_LOG = "~/.ollama/logs/server.log"
HTTP_TIMEOUT_S = 5
PROBE_TIMEOUT_S = 180
CLOUD_PROVIDERS = ("openai", "azure", "bedrock", "anthropic")

EXCEEDS = "exceeds the available context size"
LOG_PATTERNS = (
    ("exceeds", EXCEEDS),
    ("truncated", "truncated = 1"),
    ("name_warn", "no reverse mapping found for function name"),
    ("http400", re.compile(r"\|\s*400\s*\|")),
)

# 道具の種類（名前は sherpa/mcp_server.py の道具定義と同じ）
SEARCH_KINDS = (
    ("全文", frozenset({"es_search"})),
    ("グラフ", frozenset({"graph_neighbors", "graph_resolve", "graph_impact"})),
    ("文字列", frozenset({"ripgrep_search"})),
    ("読み取り", frozenset({"read_doc", "read_around", "file_head", "doc_outline", "compare_documents", "xlsx_sheets",
                         "xlsx_range", "docx_paragraphs", "pptx_slides", "pdf_pages", "pdf_page_image"})),
    ("一覧", frozenset({"list_docs", "folder_tree", "glob_search", "name_lookup"})),
)
_EXEC_TOOL_RE = re.compile(r"mcp__sherpa__(\w+)")
_CALL_TYPES = ("function_call", "custom_tool_call", "local_shell_call")
_OUTPUT_TYPES = ("function_call_output", "custom_tool_call_output")

# 失敗の種類: 名前 → (画面の短い言い方, 説明)
FAIL_LABELS = {
    "unsupported_call": ("名前の頭が落ちた", "道具の名前の前半（mcp__sherpa/）が欠けて呼ばれ、Codex が知らない道具として断った"),
    "exec_rejected_args": ("シェルの引数", "シェルを実行する道具へ渡した引数が決まりに合わず断られた"),
    "exec_spawn_failed": ("シェルの起動", "シェルのコマンドを起動できなかった"),
    "mcp_protocol_error": ("道具との通信", "道具のサーバーとのやり取りがエラーで終わった"),
    "exit_nonzero": ("コマンドの異常終了", "シェルのコマンドが 0 以外の終了コードで終わった"),
    "mcp_tool_error": ("道具のエラー", "道具が自分でエラーを返した"),
    "call_error": ("呼び出しの形", "呼び出しの引数が読めない形だった"),
}


def out_text(out) -> str:
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        return "\n".join(str(x.get("text", "")) for x in out if isinstance(x, dict))
    return json.dumps(out, ensure_ascii=False)


def fail_kind(text: str):
    """道具の結果の文面から失敗の種類を返す（成功は None）。"""
    t = text.lstrip()
    if t.startswith("unsupported call"):
        return "unsupported_call"
    if t.startswith("`justification` requires") or t.startswith("approval policy is Never"):
        return "exec_rejected_args"
    if t.startswith("exec_command failed"):
        return "exec_spawn_failed"
    if "failed:" in t[:60] and "Mcp error" in t:
        return "mcp_protocol_error"
    m = re.search(r"Process exited with code (\d+)", t)
    if m and m.group(1) != "0":
        return "exit_nonzero"
    if re.search(r'Output:\s*\{"error"', t) or t.startswith('{"error"'):
        return "mcp_tool_error"
    if t.startswith(("failed to parse function arguments", "tool call error", "error:")):
        return "call_error"
    return None


def search_kind_counts(named_calls) -> dict:
    """名前つきの呼び出し（道具の名前の一覧）から、検索の種類ごとの回数を数える。"""
    c = {label: 0 for label, _ in SEARCH_KINDS}
    for name in named_calls:
        for label, names in SEARCH_KINDS:
            if name in names:
                c[label] += 1
    return c


def _payload(o):
    p = o.get("payload")
    return p if isinstance(p, dict) else {}


def rollout_stats(path: str) -> dict:
    """rollout 1 本（1 ターン）の数字を数える。"""
    rows = []
    with open(path, errors="replace") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    meta = next((_payload(o) for o in rows if o.get("type") == "session_meta"), {})
    ctxrow = next((_payload(o) for o in rows if o.get("type") == "turn_context"), {})
    infos = [_payload(o)["info"] for o in rows if _payload(o).get("type") == "token_count" and _payload(o).get("info")]
    ins = [i["last_token_usage"]["input_tokens"] for i in infos if isinstance(i.get("last_token_usage"), dict)]
    total = infos[-1].get("total_token_usage", {}) if infos else {}
    call_ns = {}
    calls_named, calls_bare = [], []
    fails = collections.Counter()
    exec_named = []
    for o in rows:
        p = _payload(o)
        ty = p.get("type")
        if ty in _CALL_TYPES:
            ns = p.get("namespace") or ""
            name = str(p.get("name") or ty)
            call_ns[p.get("call_id")] = (ns, name)
            (calls_named if ns else calls_bare).append(name)
            if name in ("exec", "exec_command"):
                exec_named += _EXEC_TOOL_RE.findall(str(p.get("arguments") or p.get("input") or ""))
        elif ty in _OUTPUT_TYPES:
            k = fail_kind(out_text(p.get("output")))
            if k:
                fails[k] += 1
    done = [_payload(o) for o in rows if _payload(o).get("type") == "task_complete"]
    err_events = [_payload(o) for o in rows if _payload(o).get("type") in ("turn_aborted", "error", "stream_error")]
    err_text = ""
    last = done[-1] if done else {}
    if isinstance(last.get("error"), dict):
        err_text += str(last["error"].get("message", ""))
    err_text += " ".join(json.dumps(e, ensure_ascii=False) for e in err_events)
    m = re.search(r"exceeds the available context size \((\d+) tokens\)", err_text)
    stamps = [o.get("timestamp") for o in rows if o.get("timestamp")]
    src = meta.get("source")
    return {
        "provider": str(meta.get("model_provider") or ""), "model": str(ctxrow.get("model") or ""),
        "cli_version": str(meta.get("cli_version") or ""), "is_child": isinstance(src, dict),
        "started": stamps[0] if stamps else "",
        "model_calls": len(infos), "input_first": ins[0] if ins else None, "input_max": max(ins) if ins else None,
        "output_tokens": total.get("output_tokens") or 0, "reasoning_tokens": total.get("reasoning_output_tokens") or 0,
        "reasoning_items": sum(1 for o in rows if _payload(o).get("type") == "reasoning"),
        "codex_window": infos[-1].get("model_context_window") if infos else None,
        "tools_total": len(calls_named) + len(calls_bare), "tools_named": len(calls_named), "tools_bare": len(calls_bare),
        "fails": dict(fails), "fails_total": sum(fails.values()),
        "search": search_kind_counts(calls_named + exec_named),
        "tool_names": dict(collections.Counter(calls_named + calls_bare)),
        "completed": bool(done), "ended_error": bool(err_text.strip()),
        "exceeds_in_error": EXCEEDS in err_text, "error_ctx": int(m.group(1)) if m else None,
    }


def is_local(provider: str) -> bool:
    p = provider.lower()
    return not any(c in p for c in CLOUD_PROVIDERS)


def _ratio(a, b):
    return (a / b) if a is not None and b else None


def judge(st: dict, ctx, log=None) -> list:
    """判定の名前（複数同時に立つ）を返す。接続先が Ollama でないターンは判定しない。log は Ollama のログの数
    （このターンに結び付けてよいときだけ渡す）。"""
    if not is_local(st["provider"]):
        return []
    out = []
    ctx = ctx or st.get("error_ctx")
    r_max, r_first = _ratio(st["input_max"], ctx), _ratio(st["input_first"], ctx)
    if st["exceeds_in_error"] or (log and log.get("exceeds")):
        out.append("枠で落ちた")
    if r_max is not None and r_max >= RATIO_TIGHT:
        out.append("枠がぎりぎり")
    if r_first is not None and r_first >= RATIO_FIRST_HEAVY:
        out.append("最初の指示で枠の大半")
    if ctx and st["codex_window"] and st["codex_window"] > ctx and r_max is not None and r_max >= RATIO_ROOM:
        out.append("Codex が枠を取り違えている")
    many_fail = st["tools_total"] > 0 and st["fails_total"] / st["tools_total"] >= FAIL_RATIO
    no_split = st["search"]["全文"] == 0 and st["search"]["グラフ"] == 0
    if many_fail:
        out.append("道具の呼び出しの失敗が多い")
    if no_split:
        out.append("検索の道具を使い分けていない")
    if st["model_calls"] > 0 and st["reasoning_tokens"] == 0 and st["reasoning_items"] == 0:
        out.append("推論していない")
    if r_max is not None and r_max < RATIO_ROOM and not st["ended_error"] and st["completed"] and (many_fail or no_split):
        out.append("早く切り上げている")
    return out


def next_action(name: str, st: dict, ctx) -> str:
    if name == "枠で落ちた":
        return "Ollama の文脈長を %d 以上にして同じ質問を流し直し、もう一度このスクリプトを流す" % max(RECOMMEND_CTX, (ctx or 0) * 2)
    if name == "枠がぎりぎり":
        return "文脈長に余裕がない。Ollama の文脈長を %d 以上にして流し直す" % max(RECOMMEND_CTX, (ctx or 0) * 2)
    if name == "最初の指示で枠の大半":
        return "最初の指示だけで枠の半分以上を使っている。文脈長を上げて流し直す"
    if name == "Codex が枠を取り違えている":
        return "Codex が見ている値と Ollama の値（%s）を伝える（合わないと圧縮が働かず枠を超える）" % ctx
    if name == "道具の呼び出しの失敗が多い":
        return "失敗の内訳（種類と回数）を伝える"
    if name == "検索の道具を使い分けていない":
        return "全文検索・グラフの道具がモデルに渡っているかを確かめ、この判定を伝える"
    if name == "推論していない":
        return "推論の記録が 1 回も無い。推論の強さの設定を確かめ、この判定を伝える"
    if name == "早く切り上げている":
        return "枠には余裕がある。失敗・使い分けの判定を先に直して流し直す"
    return ""


def log_counts(path: str):
    """Ollama のログの該当行を数える。無ければ None。"""
    if not path or not os.path.isfile(path):
        return None
    c = collections.Counter()
    with open(path, errors="replace") as f:
        for line in f:
            for key, pat in LOG_PATTERNS:
                if (pat in line) if isinstance(pat, str) else pat.search(line):
                    c[key] += 1
    return {k: c.get(k, 0) for k, _ in LOG_PATTERNS}


def log_grep_line(path: str) -> str:
    pats = "; ".join("printf '%%s: ' '%s'; grep -c -e '%s' %s" % (p if isinstance(p, str) else " 400 ", p if isinstance(p, str) else r"| 400 |", path)
                     for _, p in LOG_PATTERNS)
    return pats


def _get_json(url: str):
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def ollama_info(base: str, model: str):
    """/api/ps・/api/version から (版, 文脈長) を返す。取れないものは None。"""
    base = base.rstrip("/")
    ver = (_get_json(base + "/api/version") or {}).get("version")
    models = (_get_json(base + "/api/ps") or {}).get("models") or []
    pick = next((m for m in models if model and str(m.get("name", "")).startswith(model.split(":")[0])), None)
    if pick is None:
        pick = next((m for m in models if not re.search(r"embed|bge", str(m.get("name", "")), re.I)), None)
    return ver, (pick or {}).get("context_length")


def find_turns(users_dir: str, convs):
    """(会話番号, [rollout のパス（古い順）]) の一覧を、指定の会話か新しい順の既定で返す。"""
    base = {}
    for p in glob.glob(os.path.join(users_dir, "*", "workspace", ".codex-sessions", "*", "sessions", "**", "rollout-*.jsonl"),
                       recursive=True):
        cid = p.split(os.sep + ".codex-sessions" + os.sep, 1)[1].split(os.sep, 1)[0]
        base.setdefault(cid, []).append(p)
    if convs:
        return [(c, sorted(base[c])) for c in convs if c in base]
    newest = sorted(base, key=lambda c: max(os.path.getmtime(p) for p in base[c]), reverse=True)
    return [(c, sorted(base[c])) for c in newest]


def render_block(label: str, st: dict, ctx, judges: list, log, children: int) -> list:
    local = is_local(st["provider"])
    lines = ["%s（%s）  判定: %s" % (label, "ローカル" if local else "クラウド", "・".join(judges) if judges else "なし")]
    ctx_txt = "%s" % ctx if ctx else "不明（--ctx で指定）"
    cw = st["codex_window"]
    lines.append(" 1 文脈長            %s%s" % (ctx_txt if local else "Ollama ではない", "（Codex が見ている値 %s）" % cw if cw else ""))
    pct = ("（%d%%）" % round(100 * st["input_max"] / ctx)) if local and ctx and st["input_max"] else ""
    lines.append(" 2 入力 最初→最大    %s → %s%s" % (st["input_first"], st["input_max"], pct))
    lines.append(" 3 モデルの呼び出し  %d 回%s" % (st["model_calls"], "（子エージェント %d 本は別）" % children if children else ""))
    lines.append(" 4 道具              %d 回（失敗 %d）" % (st["tools_total"], st["fails_total"]))
    top = sorted(st["fails"].items(), key=lambda kv: -kv[1])
    lines.append(" 5 失敗の内訳        %s" % ("・".join("%s %d" % (FAIL_LABELS.get(k, (k, ""))[0], n) for k, n in top) or "なし"))
    lines.append(" 6 検索の種類        %s" % "・".join("%s %d" % (k, v) for k, v in st["search"].items()))
    lines.append(" 7 推論              %d 回（トークン %d）" % (st["reasoning_items"], st["reasoning_tokens"]))
    if st["exceeds_in_error"]:
        end = "エラー（文脈長の超過）"
    elif st["ended_error"]:
        end = "エラー（その他）"
    elif st["completed"]:
        end = "自分で終えた"
    else:
        end = "終了の記録なし"
    lines.append(" 8 終わり方          %s" % end)
    if not local:
        pass
    elif log is not None:
        lines.append(" 9 Ollama のログ     枠の超過 %d・切り詰め %d・名前の警告 %d・HTTP400 %d（ログ全体）"
                     % (log["exceeds"], log["truncated"], log["name_warn"], log["http400"]))
    else:
        lines.append(" 9 Ollama のログ     読めない（詳細の txt に数え方）")
    for j in judges:
        lines.append("次にやること（%s）: %s" % (j, next_action(j, st, ctx)))
        if j == "道具の呼び出しの失敗が多い":
            for k, n in top[:3]:
                lines.append("  ・%s %d 回＝%s" % (FAIL_LABELS.get(k, (k, ""))[0], n, FAIL_LABELS.get(k, ("", "?"))[1]))
    return lines


def probe(base: str, model: str) -> list:
    """Slouch を通さず Ollama の /v1/responses へ小さい依頼を 2 つ送る。"""
    def post(body):
        req = urllib.request.Request(base.rstrip("/") + "/v1/responses", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    res = []
    tool = {"type": "function", "name": "probe_echo", "description": "Echo the given text.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}
    try:
        r = post({"model": model, "input": "Call the tool probe_echo with text ping.", "tools": [tool]})
        names = [o.get("name") for o in r.get("output", []) if o.get("type") == "function_call"]
        res.append("道具の呼び出し: %s" % ("定義どおりの名前で返った" if "probe_echo" in names else "名前が違う／呼ばれなかった（返った名前 %s）" % (names or "なし")))
    except (urllib.error.URLError, OSError, ValueError) as e:
        res.append("道具の呼び出し: 試せなかった（%s）" % type(e).__name__)
    try:
        r = post({"model": model, "input": "What is 17 times 23? Think step by step.", "reasoning": {"effort": "high"}})
        rt = ((r.get("usage") or {}).get("output_tokens_details") or {}).get("reasoning_tokens") or 0
        items = [o for o in r.get("output", []) if o.get("type") == "reasoning" and (o.get("content") or o.get("summary"))]
        res.append("推論の強さの指定: 推論トークン %s・推論の出力 %d 件（%s）"
                   % (rt, len(items), "反映されている" if rt or items else "反映されていない"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        res.append("推論の強さの指定: 試せなかった（%s）" % type(e).__name__)
    return res


def _out_dir(root: str, given: str) -> str:
    for d in ([given] if given else [os.path.join(root, "data", "run", "local-llm-report"),
                                     os.path.join(tempfile.gettempdir(), "local-llm-report")]):
        try:
            os.makedirs(d, exist_ok=True)
            if os.access(d, os.W_OK):
                return d
        except OSError:
            pass
    return ""


def main(argv=None) -> int:
    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    ap = argparse.ArgumentParser(description="ローカル LLM の回答が浅い・エラーになる原因の切り分け（読み取りだけ）")
    ap.add_argument("--users-dir", help="users のフォルダ（既定: 環境変数 SHERPA_USERS_DIR、無ければ <置き場所>/data/users）")
    ap.add_argument("--conv", help="会話番号（カンマ区切り）。省略で新しい順に --turns 本")
    ap.add_argument("--turns", type=int, default=DEFAULT_TURNS, help="新しい順に見る会話の数（既定 %d）" % DEFAULT_TURNS)
    ap.add_argument("--ollama-url", help="Ollama の接続先（既定: 環境変数 OLLAMA_URL、無ければ %s）" % DEFAULT_OLLAMA_URL)
    ap.add_argument("--ctx", type=int, help="Ollama の文脈長（Ollama の機械で `ollama ps` の CONTEXT の数字）")
    ap.add_argument("--ollama-log", default=DEFAULT_OLLAMA_LOG, help="Ollama のログ（既定 %s）" % DEFAULT_OLLAMA_LOG)
    ap.add_argument("--out", help="詳細の保存先フォルダ")
    ap.add_argument("--probe", action="store_true", help="Ollama の /v1/responses に小さい依頼を送って道具の名前と推論を試す")
    ap.add_argument("--probe-model", help="--probe のモデル名（既定: 記録のモデル、無ければ gpt-oss:20b）")
    a = ap.parse_args(argv)

    users = os.path.expanduser(a.users_dir or os.environ.get("SHERPA_USERS_DIR") or "data/users")
    if not os.path.isabs(users) and not a.users_dir:
        users = os.path.join(root, users)
    url = a.ollama_url or os.environ.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL
    convs = [c.strip() for c in a.conv.split(",") if c.strip()] if a.conv else None
    found = find_turns(users, convs)
    if not convs:
        found = found[:max(1, a.turns)]

    turns = []  # (会話番号, 通し番号, 統計, 子の本数)
    for cid, paths in found:
        stats = [rollout_stats(p) for p in paths]
        parents = [s for s in stats if not s["is_child"]]
        kids = len(stats) - len(parents)
        for i, s in enumerate(parents):
            turns.append((cid, i + 1 if len(parents) > 1 else 0, s, kids))

    model = next((t[2]["model"] for t in turns if t[2]["model"] and is_local(t[2]["provider"])), "")
    ver, ps_ctx = ollama_info(url, model) if any(is_local(t[2]["provider"]) for t in turns) or not turns else (None, None)
    ctx = a.ctx or ps_ctx
    log_path = os.path.expanduser(a.ollama_log)
    log = log_counts(log_path)
    local_n = sum(1 for t in turns if is_local(t[2]["provider"]))

    screen = ["ローカル LLM の調べ（%d ターン）" % len(turns)]
    if not local_n:
        screen.append("Ollama: 見ない（接続先が Ollama のターンが無い）")
    elif ver or ps_ctx:
        screen.append("Ollama: つながった（版 %s・文脈長 %s）" % (ver or "不明", ps_ctx or "載っていない"))
    else:
        screen.append("Ollama: つながらない／載っていない")
    if local_n and not ctx:
        screen.append("文脈長が分からない。Ollama の機械で `ollama ps` を流して CONTEXT の数字を --ctx に渡して、もう一度流す")
    if log is None and local_n:
        screen.append("Ollama のログ: 読めない（詳細の txt にある 1 行を Ollama の機械で流して数字を伝える）")
    if not turns:
        screen.append("セッションの記録が見つからない（--users-dir と --conv を確かめる）")
    blocks, detail = [], []
    for cid, nth, st, kids in turns:
        label = "会話 %s%s" % (cid, "（%d 回目）" % nth if nth else "")
        js = judge(st, ctx, log if len(turns) == 1 else None)
        blocks.append(render_block(label, st, ctx, js, log, kids))
        detail.append("%s 判定=%s %s" % (label, js, json.dumps({k: v for k, v in st.items() if k != "started"}, ensure_ascii=False)))

    probe_lines = probe(url, a.probe_model or model or "gpt-oss:20b") if a.probe else []

    d = _out_dir(root, a.out)
    path = ""
    if d:
        path = os.path.join(d, "local-llm-report-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))
        txt = ["# ローカル LLM の調べ  %s" % datetime.now().strftime("%Y-%m-%d %H:%M"),
               "Ollama 版=%s 文脈長(/api/ps)=%s 文脈長(使った値)=%s" % (ver, ps_ctx, ctx), ""]
        txt += ["\n".join(b) + "\n" for b in blocks] + [""] + detail
        if log is None:
            txt += ["", "# Ollama の機械で数える 1 行（ログの場所は環境で違う。systemd なら journalctl -u ollama の出力を渡す）",
                    log_grep_line("~/.ollama/logs/server.log")]
        txt += [""] + probe_lines
        try:
            with open(path, "w") as f:
                f.write("\n".join(txt) + "\n")
        except OSError:
            path = ""
    if len(turns) > 1 and log is not None:
        screen.append("Ollama のログの数は全ターンの合計（判定に使うのは 1 ターンだけ指定したとき）")
    print("\n".join(screen))
    for b in blocks:
        print()
        print("\n".join(b))
    if probe_lines:
        print()
        print("\n".join(probe_lines))
    print()
    print("詳細: %s" % path if path else "詳細: 保存できなかった")
    return 0


if __name__ == "__main__":
    sys.exit(main())
