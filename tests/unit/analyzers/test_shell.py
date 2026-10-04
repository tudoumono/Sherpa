"""`ShellBatchAnalyzer` の単体テスト（シェル/バッチファイル定義・入力ソース片 → 定義・参照・Dropped の表）。"""
from __future__ import annotations

import pytest

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.shell import ShellBatchAnalyzer

A = ShellBatchAnalyzer()
SH = "bin/nightly.sh"
BAT = "bin/run.bat"


def test_extensions_and_name():
    assert A.extensions == frozenset({".sh", ".bash", ".ksh", ".zsh", ".bat", ".cmd"})
    assert A.name == "shell"
    assert A.doctype == "shell"


def test_accepts_all_files_without_content_inspection():
    assert ShellBatchAnalyzer.accepts is Analyzer.accepts


@pytest.mark.parametrize("path,name,kind", [("bin/nightly.sh", "nightly.sh", "shell"), ("bin/run.bat", "run.bat", "bat")])
def test_script_becomes_batch_primary(path, name, kind):
    p = A.collect_defs("echo hi\n", path).primary
    assert p is not None and p.label == "Batch" and p.name == name
    assert p.extra == {"batch_kind": kind}


# ---- 設定キー children（`key_kind="env"`）----
def E(value):
    return {"config_value": value, "key_kind": "env"}


# (入力, path, {name: extra（None は検査しない）})
CHILD_CASES = {
    "export_assignment": ("export ENV=prod\n", SH, {"ENV": E("prod")}),
    "bare_readonly_declare_x": ("LOG_DIR=/var/log\nreadonly APP_NAME=nightly\ndeclare -x DB_HOST=localhost\n", SH,
                                {"LOG_DIR": None, "APP_NAME": None, "DB_HOST": None}),
    "bat_set": ("set ENV=dev\n", BAT, {"ENV": E("dev")}),
    "bat_quoted_set": ('set "ENV=dev"\n', BAT, {"ENV": E("dev")}),
    "duplicate_key_keeps_first": ("export ENV=prod\nexport ENV=staging\n", SH, {"ENV": E("prod")}),
    "functions_not_children": ("do_thing() {\n  echo hi\n}\nfunction other {\n  echo hi\n}\n", SH, {}),
    "bat_label_not_child": (":mylabel\necho hi\n", BAT, {}),
    "quoted_value_unquoted": ('export MSG="hello world"\n', SH, {"MSG": E("hello world")}),
    "hash_comment_line_ignored": ("# export ENV=prod\n", SH, {}),
    "inline_comment_cut_from_value": ("export ENV=prod # inline comment\n", SH, {"ENV": E("prod")}),
    "bat_rem_comment_ignored": ("REM set ENV=prod\n", BAT, {}),
    "bat_double_colon_comment_ignored": (":: set ENV=prod\n", BAT, {}),
    "heredoc_body_assignment_not_child": (
        "cat <<EOF\nexport SHOULD_NOT=beparsed\necho $SHOULD_NOT_VAR\nEOF\necho done\n", SH, {}),
    "leading_assignment_before_command": ("VAR=1 ./run.sh\n", SH, {"VAR": E("1")}),
    "env_prefixed_assignment_not_child": ("env KEY=v ./run.sh\n", SH, {}),
    "export_without_value_not_child": ("export KEY\n", SH, {}),
}


@pytest.mark.parametrize("text,path,children", CHILD_CASES.values(), ids=CHILD_CASES)
def test_collect_defs_config_children(text, path, children):
    res = A.collect_defs(text, path)
    assert {c.name for c in res.children} == set(children)
    for c in res.children:
        assert children[c.name] is None or c.extra == children[c.name]
        assert c.label == "Config" and c.cid_key == f"key:env:{c.name}"


def test_export_assignment_child_line():
    assert A.collect_defs("export ENV=prod\n", SH).children[0].line == 1


# ---- 参照 ----
def I(name, path=None, kind="Batch", **extra):
    """他スクリプト呼び出し（INVOKES via=include）。"""
    e = {"via": "include", "include_path": path or name}
    return ("INVOKES", kind, name, e)


def M(name, **extra):
    """Module 呼び出し（INVOKES via=call）。"""
    return ("INVOKES", "Module", name, {"via": "call", **extra})


def V(name):
    """環境変数参照（ACCESSES via=config_key）。"""
    return ("ACCESSES", "Config", name, {"via": "config_key", "key_kind": "env"})


def D(reason, snippet=None):
    return (reason, snippet)


QJ = {"qualified": True}
# (入力, path, 参照（順序込み・extra は None なら検査しない）, Dropped（None なら検査しない）)
REFS_CASES = {
    # 他スクリプト
    "dot_source": (". ./common.sh\n", SH, [I("common.sh", "./common.sh")], None),
    "source_keyword": ("source lib/common.sh\n", SH, [I("common.sh", "lib/common.sh")], None),
    "bash_interpreter": ("bash ./common.sh\n", SH, [("INVOKES", "Batch", "common.sh", None)], None),
    "bare_dot_slash_script": ("./common.sh\n", SH, [("INVOKES", "Batch", "common.sh", None)], None),
    "bat_call_keyword": ("call common.bat\n", BAT, [I("common.bat")], None),
    "bat_bare_path": ("common.bat\n", BAT, [("INVOKES", "Batch", "common.bat", None)], None),
    "windows_backslash_normalized": ("call ..\\lib\\common.bat\n", BAT, [I("common.bat", "../lib/common.bat")], None),
    # java
    "java_jar_dropped": ("java -jar app.jar\n", SH, [], [D("shell_jar", "app.jar")]),
    "java_dash_d_before_jar_dropped": ("java -Dfoo=bar -jar x.jar\n", SH, [], [D("shell_jar", "x.jar")]),
    "java_dash_d_value_not_main_class": ("java -Dimpl=com.fake.NotMain com.x.Main\n", SH, [M("com.x.Main", **QJ)], None),
    "java_classpath": ("java -cp lib com.acme.BatchMain\n", SH, [M("com.acme.BatchMain", **QJ)], None),
    "java_bare_fqcn": ("java com.acme.Main\n", SH, [M("com.acme.Main", **QJ)], None),
    # 実行ファイルの単独行呼び出し
    "bare_dot_slash_program": ("./PAYROLL\n", SH, [M("PAYROLL")], None),
    "bare_program_name": ("PAYROLL\n", SH, [("INVOKES", "Module", "PAYROLL", None)], None),
    "program_with_extra_tokens_not_bare_call": ("PAYROLL --verbose\n", SH, [], None),
    # SQL スクリプト実行・他ランタイム
    "sqlplus_at_script": ("sqlplus scott/tiger @load.sql\n", SH, [], [D("shell_sql_script", "load.sql")]),
    "psql_dash_f": ("psql -f load.sql\n", SH, None, [D("shell_sql_script", "load.sql")]),
    "mysql_redirect": ("mysql < load.sql\n", SH, None, [D("shell_sql_script", "load.sql")]),
    "node_invocation": ("node batch.js\n", SH, [M("batch.js", include_path="batch.js")], None),
    "python_unsupported": ("python etl.py\n", SH, [], [D("shell_unsupported_runtime", "python")]),
    "perl_unsupported": ("perl legacy.pl\n", SH, None, [D("shell_unsupported_runtime", "perl")]),
    # 変数参照
    "dollar_brace_variable": ('echo "${LOG_DIR}"\n', SH, [V("LOG_DIR")], None),
    "dollar_bare_in_double_quotes": ('echo "$LOG_DIR"\n', SH, [("ACCESSES", "Config", "LOG_DIR", None)], None),
    "percent_variable_in_bat": ("echo %LOG_DIR%\n", BAT, [("ACCESSES", "Config", "LOG_DIR", None)], None),
    "lowercase_local_variable_ignored": ("echo $file\n", SH, [], None),
    "special_positional_variables_ignored": ("echo $1 $@ $? $#\n", SH, [], None),
    "single_quoted_variable_ignored": ("echo '$LOG_DIR'\n", SH, [], None),
    "self_defined_variable_is_self_reference": ('export ENV=prod\necho "$ENV"\n', SH, [], None),
    "brace_default_suffix_and_escaped_dollar": (
        'echo \\$KEY "${OTHER:-default}" "$KEY_SUFFIX"\n', SH, [V("OTHER"), V("KEY_SUFFIX")], None),
    "brace_length_operator": ('echo "${#KEY}"\n', SH, [("ACCESSES", "Config", "KEY", None)], None),
    "export_without_value_is_access": ("export KEY\n", SH, [V("KEY")], None),
    "readonly_declare_x_without_value": ("readonly APP_NAME\ndeclare -x DB_HOST\n", SH,
                                         [("ACCESSES", "Config", "APP_NAME", None), ("ACCESSES", "Config", "DB_HOST", None)],
                                         None),
    # ヒアドキュメント
    "heredoc_body_skipped_and_reported_once": (
        "cat <<EOF\nexport SHOULD_NOT=beparsed\necho $SHOULD_NOT_VAR\nEOF\necho done\n", SH, [], [D("shell_heredoc", "EOF")]),
    "heredoc_dash_variant": ("cat <<-EOF\n  $INSIDE\nEOF\n", SH, [], [D("shell_heredoc")]),
    "heredoc_in_comment_not_start": ("echo hi # <<EOF\necho next\n", SH, None, []),
    "heredoc_in_double_quotes_not_start": ('echo "literal <<EOF"\necho next\n', SH, None, []),
    # 終端語が引用符つき／その他の形でも本文は読まない（終端語ごとに Dropped 1 件・本文のコマンドは辺にしない）
    "heredoc_single_quoted_delim": ("cat <<'EOF'\nbash other.sh\nEOF\n", SH, [], [D("shell_heredoc", "EOF")]),
    "heredoc_double_quoted_delim": ('cat <<"END"\nbash other.sh\nEND\n', SH, [], [D("shell_heredoc", "END")]),
    "heredoc_quoted_with_redirect": ("cat > out.txt <<'EOF'\nbash other.sh\nEOF\n", SH, [], [D("shell_heredoc", "EOF")]),
    "heredoc_quoted_piped_to_sh": ("sh <<'EOF'\nbash other.sh\nEOF\n", SH, [], [D("shell_heredoc", "EOF")]),
    "heredoc_quoted_then_real_command": (
        "cat <<'EOF'\nbash other.sh\nEOF\nbash real.sh\n", SH, [("INVOKES", "Batch", "real.sh", None)],
        [D("shell_heredoc", "EOF")]),
    "heredoc_quoted_in_function": ("f() {\n  cat <<'EOF'\nbash other.sh\nEOF\n}\n", SH, [], [D("shell_heredoc", "EOF")]),
    "heredoc_quoted_dash_variant": ("cat <<-'EOF'\n\tbash other.sh\n\tEOF\n", SH, [], [D("shell_heredoc", "EOF")]),
    "heredoc_quoted_unterminated_reads_to_eof": ("cat <<'EOF'\nbash other.sh\n", SH, [], [D("shell_heredoc", "EOF")]),
    "heredoc_two_on_one_line": (
        "cat <<A <<B\nbash a.sh\nA\nbash b.sh\nB\n", SH, [], [D("shell_heredoc", "A"), D("shell_heredoc", "B")]),
    "heredoc_same_line_command_after_start_is_real": (
        "cat <<EOF; bash real.sh\nbash other.sh\nEOF\n", SH, [("INVOKES", "Batch", "real.sh", None)],
        [D("shell_heredoc", "EOF")]),
    "heredoc_indented_terminator_does_not_end_plain_form": (
        "cat <<EOF\n  EOF\nbash other.sh\nEOF\nbash real.sh\n", SH, [("INVOKES", "Batch", "real.sh", None)],
        [D("shell_heredoc", "EOF")]),
    "heredoc_space_indented_terminator_does_not_end_dash_form": (
        "cat <<-EOF\n  EOF\nbash other.sh\n\tEOF\nbash real.sh\n", SH, [("INVOKES", "Batch", "real.sh", None)],
        [D("shell_heredoc", "EOF")]),
    "heredoc_after_parameter_expansion_hash": (
        "echo ${x#foo} <<EOF\nbash other.sh\nEOF\n", SH, [], [D("shell_heredoc", "EOF")]),
    "heredoc_after_escaped_quote": ("printf \\'x <<EOF\nbash other.sh\nEOF\n", SH, [], [D("shell_heredoc", "EOF")]),
    "heredoc_quoted_delimiter_with_metachar": ("cat <<'END)'\nbash other.sh\nEND)\n", SH, [], [D("shell_heredoc", "END)")]),
    "heredoc_delimiter_with_hyphens": (
        "cat <<END-OF-FILE\nbash other.sh\nEND-OF-FILE\n", SH, [], [D("shell_heredoc", "END-OF-FILE")]),
    "shift_in_arithmetic_is_not_heredoc": ("echo $((1<<2))\nbash real.sh\n", SH, [("INVOKES", "Batch", "real.sh", None)], []),
    "shift_in_string_is_not_heredoc": ('echo "a<<b"\nbash real.sh\n', SH, [("INVOKES", "Batch", "real.sh", None)], []),
    "here_string_is_not_heredoc": ("cat <<<'x'\nbash real.sh\n", SH, [("INVOKES", "Batch", "real.sh", None)], []),
    # コマンド位置の分割・ラッパー
    "after_and_and": ("true && java -cp a.jar:b.jar com.x.Main\n", SH, [M("com.x.Main", **QJ)], None),
    "after_pipe": ("echo ok | node x.js\n", SH, [("INVOKES", "Module", "x.js", None)], None),
    "exec_wrapper_stripped": ("exec java -cp lib com.acme.BatchMain\n", SH, [("INVOKES", "Module", "com.acme.BatchMain", None)], None),
    "nohup_backgrounded_wrapper": ("nohup java -jar x.jar &\n", SH, [], [D("shell_jar", "x.jar")]),
    "if_then_position": ("if [ -f x ]; then ./RUN; fi\n", SH, [("INVOKES", "Module", "RUN", None)], None),
    "for_do_position": ("for x in a b; do ./RUN; done\n", SH, [("INVOKES", "Module", "RUN", None)], None),
    "leading_assignment_command_parsed": ("VAR=1 ./run.sh\n", SH, [("INVOKES", "Batch", "run.sh", None)], None),
    "env_prefix_command_parsed": ("env KEY=v ./run.sh\n", SH, [("INVOKES", "Batch", "run.sh", None)], None),
    # bat の組み込みコマンドは bare exec 判定より先に除外
    "bat_echo_builtin": ("ECHO\n", BAT, [], None),
    "bat_exit_builtin": ("EXIT\n", BAT, [], None),
}


@pytest.mark.parametrize("text,path,refs,dropped", REFS_CASES.values(), ids=REFS_CASES)
def test_extract_refs(text, path, refs, dropped):
    res = A.extract_refs(text, path)
    if refs is not None:
        assert [(r.edge_type, r.kind, r.name) for r in res.refs] == [(e, k, n) for e, k, n, _x in refs]
        for r, (_e, _k, _n, extra) in zip(res.refs, refs):
            assert extra is None or r.extra == extra
    expected = dropped or []  # None は「申告なし」の期待
    assert len(res.dropped) == len(expected)
    for d, (reason, snippet) in zip(res.dropped, expected):
        assert d.reason == reason and (snippet is None or d.snippet == snippet)


def test_dot_source_reference_line():
    assert A.extract_refs(". ./common.sh\n", SH).refs[0].line == 1


def test_heredoc_dropped_line_and_no_refs_inside_body():
    res = A.extract_refs("cat <<EOF\necho $SHOULD_NOT_VAR\nEOF\n", SH)
    assert [(d.reason, d.line) for d in res.dropped] == [("shell_heredoc", 1)]
    assert res.refs == []


def test_leading_assignment_before_command_is_config_child_and_command_is_parsed():
    res = A.collect_defs("VAR=1 ./run.sh\n", SH)
    assert [(c.name, c.extra["config_value"]) for c in res.children] == [("VAR", "1")]
