"""Codex 子プロセスの停止・監視と、失敗ログ・失敗の分類・最終メッセージ保険読取の補助。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import errno
import os
import re
import signal
import stat
import threading
import time
from pathlib import Path

from ... import agentic_search
from ..base import _log


def _humanize_cmd(command: str):
    """Codex が実行したシェルコマンド → 画面の言葉＋実コマンド（detail）。"""
    inner = command
    m = re.search(r'-lc\s+"(.*)"\s*$', command) or re.search(r"-lc\s+'(.*)'\s*$", command)
    if m:
        inner = m.group(1)
    low = inner.lower()
    if "grep" in low or low.startswith("rg ") or " rg " in low:
        label = "ファイルを検索（grep）"
    elif any(k in low for k in ("cat ", "sed ", "head ", "tail ", "less ", "nl ")):
        label = "ファイルを参照"
    elif low.startswith(("ls", "find")) or " find " in low:
        label = "ファイル一覧"
    else:
        label = "コマンド実行"
    return label, inner.strip()[:140]


def _masked_run_dir_path(fp: str, run_dir: Path) -> str:
    """失敗ログにフルパス（uid を含む）を出さず、`run_dir` からの相対部分だけを run_dir の識別子（`run-<乱数>`）に付けて返す。相対化できなければ識別子だけを返す。"""
    try:
        rel = Path(fp).resolve().relative_to(run_dir.resolve())
        return f"{run_dir.name}/{rel}"
    except (OSError, ValueError):
        return run_dir.name


def _killpg(proc) -> None:
    """MCP subprocess / shell child まで確実に殺す（creds env の寿命を延ばさない）。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


_SESSION_REAP_ATTEMPTS = 5  # 固定回数（無限リトライにしない）
_SESSION_REAP_INTERVAL_S = 0.05  # 数十ミリ秒


_STARTUP_STDERR_MAX_BYTES = 4096
_STARTUP_STDERR_MAX_LINES = 20


def _log_startup_stderr(f, returncode, got_any_line: bool, conv, uid) -> None:
    """Codex が `--json` のイベントを1件も出さずに異常終了したときだけ、stderr の先頭を伏せ字にかけてログに残す。正常終了・イベントが出た後の失敗では読まない。どちらでもファイルは閉じて捨てる。"""
    try:
        if returncode not in (None, 0) and not got_any_line:
            f.seek(0)
            head = f.read(_STARTUP_STDERR_MAX_BYTES).decode("utf-8", "replace")
            lines = [ln.rstrip() for ln in head.splitlines() if ln.strip()][:_STARTUP_STDERR_MAX_LINES]
            if lines:
                _log.warning("codex startup failure: returncode=%s conv=%s uid=%s stderr=\n%s",
                             returncode, conv, uid, agentic_search._redact("\n".join(lines)))
    except Exception:
        pass
    finally:
        try:
            f.close()
        except Exception:
            pass


# モデルの思考の区切りの制御記号（例 `<|channel|>`・`<think>`）。表示の1行要約からだけ外す。
_CONTROL_MARKER_RE = re.compile(r"<\|?/?[A-Za-z_]{1,24}\|?>")


def _strip_control_markers(text: str) -> str:
    return _CONTROL_MARKER_RE.sub("", text)

def _kill_session_fallback_killpg(sid: int, reason: str) -> None:
    """pidfd/`/proc` が使えない環境（macOS 等）向けの縮退経路。`_kill_session` と同じ前提（reap する前に呼ぶ）のもと `os.killpg` で group ごと SIGKILL する。setpgid で group を抜けた孤児には届かない縮退版であることを毎回 1 回だけ警告する。
    `start_new_session=True` なので pgid == sid。getpgid は使わない（macOS ではゾンビのリーダーで ESRCH になるため）。
    """
    try:
        os.killpg(sid, signal.SIGKILL)
    except ProcessLookupError:
        return  # group にもう誰もいない＝回収するものが無い
    except Exception as exc:
        _log.warning(
            "codex session reap: killpg フォールバックにも失敗しました（%s・%s）sid=%s",
            reason, type(exc).__name__, sid)
        return
    _log.warning(
        "codex session reap: pidfd/proc 非対応環境のため killpg フォールバックへ縮退しました"
        "（%s・setpgid で group を抜けた子は回収できません）sid=%s", reason, sid)


def _kill_session(sid: int) -> None:
    """`sid` を session id に持つプロセスを、残りが無くなるまで数回 SIGKILL する。呼び出し側は `start_new_session=True` で起動した Popen の pid を渡す。
    プロセスグループでなくセッションで回収するのは、サンドボックス内の子が `setpgid(0,0)` で別グループへ移ると `_killpg` が届かないため（SIGKILL だけが届く）。
    呼び出し側は sid の元の Popen を `wait()` で reap する前に呼ぶこと（reap 後は pid が再利用されうる）。sid 自身（リーダー）は対象から除く。
    判定から送信までの pid 再利用による誤爆は、判定前に確保した pidfd 越しに `pidfd_send_signal` で送って防ぐ。pidfd や `/proc` が使えない環境は `_kill_session_fallback_killpg` へ縮退する。自分自身とこのセッションに属さないプロセスには触れない。
    """
    if sid <= 0:
        return
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        _kill_session_fallback_killpg(sid, "pidfd 未対応")
        return
    my_pid = os.getpid()
    try:
        for _ in range(_SESSION_REAP_ATTEMPTS):
            try:
                candidates = [e for e in os.listdir("/proc") if e.isdigit()]
            except OSError:
                _kill_session_fallback_killpg(sid, "/proc 未対応")
                return
            matched = 0
            for entry in candidates:
                pid = int(entry)
                # sid 自身（リーダー）は呼び出し側の `_killpg`/`proc.wait()` が担当するため対象から除く。
                if pid in (my_pid, sid):
                    continue
                try:
                    fd = os.pidfd_open(pid, 0)
                except OSError as exc:
                    if getattr(exc, "errno", None) == errno.ENOSYS:
                        _log.warning(
                            "codex session reap: pidfd_open が ENOSYS（カーネル未対応）のため中断します sid=%s",
                            sid)
                        return
                    continue  # ESRCH 等（走査中に対象が消えた）は通常経路——次候補へ
                try:
                    try:
                        with open(f"/proc/{entry}/stat", "r", errors="replace") as f:
                            raw = f.read()
                    except OSError:
                        continue  # 走査中にプロセスが消えるのは通常経路
                    # comm フィールドは括弧内で空白/括弧を含み得るため、最後の ')' を境に固定オフセットで読む。境より後ろ: state ppid pgrp session ...
                    paren = raw.rfind(")")
                    if paren == -1:
                        continue
                    fields = raw[paren + 2:].split()
                    if len(fields) < 4:
                        continue
                    try:
                        proc_sid = int(fields[3])
                    except ValueError:
                        continue
                    if proc_sid != sid:
                        continue
                    matched += 1
                    try:
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    except OSError:
                        pass
                finally:
                    os.close(fd)
            if not matched:
                return
            time.sleep(_SESSION_REAP_INTERVAL_S)
    except Exception as exc:
        _log.warning("codex session reap failed: %s errno=%s sid=%s",
                     type(exc).__name__, getattr(exc, "errno", None), sid)


def _spawn_stop_watcher(proc, stop_event, reap_lock, reaped) -> "threading.Thread":
    """途中停止: 別スレッドで stop_event を監視し、立ったら `_killpg` で子プロセスごと殺して stdout を EOF にし、ブロック中の read を解放する。`stop_event` が None でも常に起動する（下の pipe 閉じ役のため）。プロセスが自然終了したらスレッドも自分で抜ける（daemon・呼び出し側は join 不要）。
    終了検知に `proc.poll()` は使わない（reap してしまい、`_attempt` の finally の「`_kill_session` → `proc.wait()`」順序が崩れるため）。`os.waitid(..., WNOWAIT)` で reap せずに確認する。
    リーダーの終了/停止を検知したら reap する前に `_kill_session` を呼ぶ（別グループの子が pipe を握ったまま残ると read ループが EOF にならないため）。
    `reap_lock`/`reaped`: finally 側の reap とこのスレッドの判定を直列化し、`reaped["done"]` が立っている（または waitid が ECHILD）なら何もせず戻る（reap 後の pid 再利用で無関係なプロセスを殺さないため）。
    """
    # macOS の CPython には os.waitid が無いため、停止操作だけを見る（終了後の片付けは finally 側の `_kill_session`）。
    _can_peek_exit = hasattr(os, "waitid")

    def _watch(_proc=proc, _ev=stop_event, _lock=reap_lock, _reaped=reaped):
        while True:
            with _lock:
                if _reaped["done"]:
                    return
                exited = False
                if _can_peek_exit:
                    try:
                        exited = os.waitid(
                            os.P_PID, _proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
                    except ChildProcessError:
                        return  # 既に finally で reap 済み（ECHILD）＝ sid には触れない
                if exited:
                    _kill_session(_proc.pid)  # pipe を握ったまま残る子を片付けて EOF にする
                    return
            if _ev is not None and _ev.wait(timeout=0.3):
                with _lock:
                    if not _reaped["done"]:
                        _killpg(_proc)
                        _kill_session(_proc.pid)
                return
            elif _ev is None:
                time.sleep(0.3)
    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    return t


def _spawn_wall_clock_watcher(proc, deadline_mono: float, reap_lock, reaped, hit_state: dict) -> "threading.Thread":
    """1ターン全体（自動継続を含む）の壁時計上限。`deadline_mono`（`time.monotonic()` 基準）に達したら、停止操作と同じ手順（`_killpg` → `_kill_session`）でこの attempt のプロセスを打ち切る。
    `reap_lock`/`reaped` を `_attempt` の finally・`_spawn_stop_watcher` と共有し、二重キルや reap 後の pid 再利用への誤送信を避ける。
    `hit_state["hit"]`: この関数が実際に打ち切った時だけ True を書く。attempt をまたいで同じ dict を渡す。
    """
    def _watch(_proc=proc, _deadline=deadline_mono, _lock=reap_lock, _reaped=reaped, _hit=hit_state):
        while True:
            with _lock:
                if _reaped["done"]:
                    return
            remaining = _deadline - time.monotonic()
            if remaining <= 0:
                with _lock:
                    if not _reaped["done"]:
                        _hit["hit"] = True
                        _killpg(_proc)
                        _kill_session(_proc.pid)
                return
            time.sleep(min(remaining, 0.3))
    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    return t


_LAST_MESSAGE_MAX_BYTES = 16 * 1024 * 1024  # 最終メッセージの保険読取のメモリ保護（回答の長さを切る目的ではない）


def _read_last_message_fallback(path: Path, notices: list[str] | None = None,
                                attempt_no: int | None = None) -> str | None:
    """最終メッセージを通常ファイル・16 MiB の上限で読み、欠落を注記に残す。"""
    def note(text):
        if attempt_no is not None:
            text = f"試行 {attempt_no}: {text}"
        if notices is not None and text not in notices:
            notices.append(text)

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        note(f"最終メッセージ 1 件を読み取り失敗のため採用できませんでした"
             f"（{type(exc).__name__}、大きさ不明）。")
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            note("最終メッセージ 1 件を読み取りできませんでした（通常ファイルではありません、大きさ不明）。")
            return None
        if st.st_size > _LAST_MESSAGE_MAX_BYTES:
            note(f"最終メッセージ 1 件（{st.st_size} バイト）を 16 MiB の上限超過のため採用できませんでした。")
            return None
        if st.st_size <= 0:
            return None
        data = os.read(fd, st.st_size)
        if len(data) < st.st_size:
            note(f"最終メッセージ 1 件の読み取りが途中で終わりました（未取得 {st.st_size - len(data)} バイト）。")
    except OSError as exc:
        note(f"最終メッセージ 1 件を読み取り失敗のため採用できませんでした"
             f"（{type(exc).__name__}、大きさ不明）。")
        return None
    finally:
        os.close(fd)
    try:
        txt = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        note("最終メッセージ 1 件を UTF-8 の読み取り失敗のため採用できませんでした（失われた文字数不明）。")
        return None
    return txt or None


# `codex_error_info` がこの値のとき、ツール結果だけで文脈枠を使い切った。利用者向け文言は「範囲を絞れ」を案内し、終了理由は `budget`（打切りの内訳）に数える。
_CONTEXT_WINDOW_EXCEEDED_CODE = "context_window_exceeded"
# `codex_error_info` を持たない CLI 版でも文脈枠超過を見分ける分類。`error.message` は保存もログ出力もせず、この判定にだけ使う（資料名・抜粋が混ざり得るため）。
_TURN_FAILURE_PATTERNS = (
    (("ran out of room", "context window", "context_window"), _CONTEXT_WINDOW_EXCEEDED_CODE),
)


def _classify_turn_failure(message) -> str | None:
    """`turn.failed` の本文を固定語彙のコードへ分類する（該当しなければ `None`）。"""
    if not isinstance(message, str) or not message:
        return None
    low = message.lower()
    for needles, code in _TURN_FAILURE_PATTERNS:
        if any(n in low for n in needles):
            return code
    return None
