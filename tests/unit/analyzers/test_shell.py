"""`ShellBatchAnalyzer` の単体テスト（アナライザ拡張 波3 レーン B＝シェル/バッチファイル定義）。"""
from __future__ import annotations

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.shell import ShellBatchAnalyzer

A = ShellBatchAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".sh", ".bash", ".ksh", ".zsh", ".bat", ".cmd"})
    assert A.name == "shell"
    assert A.doctype == "shell"


def test_accepts_all_files_without_content_inspection():
    assert ShellBatchAnalyzer.accepts is Analyzer.accepts


# --- primary（Batch）---

def test_posix_script_becomes_batch_primary_with_shell_kind():
    res = A.collect_defs("echo hi\n", "bin/nightly.sh")
    assert res.primary is not None
    assert res.primary.label == "Batch" and res.primary.name == "nightly.sh"
    assert res.primary.extra == {"batch_kind": "shell"}


def test_bat_script_becomes_batch_primary_with_bat_kind():
    res = A.collect_defs("echo hi\n", "bin/run.bat")
    assert res.primary is not None
    assert res.primary.name == "run.bat"
    assert res.primary.extra == {"batch_kind": "bat"}


# --- 設定キー children（`key_kind="env"`）---

def test_export_assignment_becomes_config_child():
    res = A.collect_defs("export ENV=prod\n", "bin/nightly.sh")
    assert len(res.children) == 1
    child = res.children[0]
    assert child.label == "Config" and child.name == "ENV" and child.cid_key == "key:env:ENV"
    assert child.extra == {"config_value": "prod", "key_kind": "env"}
    assert child.line == 1


def test_bare_assignment_and_readonly_and_declare_x_all_become_config_children():
    text = "LOG_DIR=/var/log\nreadonly APP_NAME=nightly\ndeclare -x DB_HOST=localhost\n"
    res = A.collect_defs(text, "bin/common.sh")
    names = {c.name for c in res.children}
    assert names == {"LOG_DIR", "APP_NAME", "DB_HOST"}


def test_bat_set_assignment_becomes_config_child():
    res = A.collect_defs("set ENV=dev\n", "bin/run.bat")
    assert len(res.children) == 1
    assert res.children[0].name == "ENV"
    assert res.children[0].extra["key_kind"] == "env"


def test_duplicate_key_in_same_file_keeps_first_silently():
    text = "export ENV=prod\nexport ENV=staging\n"
    res = A.collect_defs(text, "bin/nightly.sh")
    assert len(res.children) == 1
    assert res.children[0].extra["config_value"] == "prod"


def test_functions_and_labels_are_not_config_children():
    text = "do_thing() {\n  echo hi\n}\nfunction other {\n  echo hi\n}\n"
    res = A.collect_defs(text, "bin/nightly.sh")
    assert res.children == []


def test_bat_label_is_not_a_config_child():
    res = A.collect_defs(":mylabel\necho hi\n", "bin/run.bat")
    assert res.children == []


def test_quoted_value_is_unquoted_for_config_value():
    res = A.collect_defs('export MSG="hello world"\n', "bin/nightly.sh")
    assert res.children[0].extra["config_value"] == "hello world"


# --- 他スクリプト呼び出し（`INVOKES(via=include)`）---

def test_dot_source_call_becomes_include_reference():
    res = A.extract_refs(". ./common.sh\n", "bin/nightly.sh")
    assert len(res.refs) == 1
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Batch", "common.sh")
    assert ref.extra == {"via": "include", "include_path": "./common.sh"}
    assert ref.line == 1


def test_source_keyword_call_becomes_include_reference():
    res = A.extract_refs("source lib/common.sh\n", "bin/nightly.sh")
    assert res.refs[0].name == "common.sh"
    assert res.refs[0].extra["include_path"] == "lib/common.sh"


def test_bash_interpreter_call_becomes_include_reference():
    res = A.extract_refs("bash ./common.sh\n", "bin/nightly.sh")
    assert res.refs[0].name == "common.sh"


def test_bare_dot_slash_script_call_becomes_include_reference():
    res = A.extract_refs("./common.sh\n", "bin/nightly.sh")
    assert (res.refs[0].edge_type, res.refs[0].kind, res.refs[0].name) == ("INVOKES", "Batch", "common.sh")


def test_bat_call_keyword_becomes_include_reference():
    res = A.extract_refs("call common.bat\n", "bin/run.bat")
    assert (res.refs[0].edge_type, res.refs[0].kind, res.refs[0].name) == ("INVOKES", "Batch", "common.bat")
    assert res.refs[0].extra == {"via": "include", "include_path": "common.bat"}


def test_bat_bare_path_call_becomes_include_reference():
    res = A.extract_refs("common.bat\n", "bin/run.bat")
    assert res.refs[0].name == "common.bat"


def test_windows_backslash_include_path_is_normalized_to_forward_slash():
    res = A.extract_refs("call ..\\lib\\common.bat\n", "bin/run.bat")
    assert res.refs[0].extra["include_path"] == "../lib/common.bat"


# --- java 呼び出し（`INVOKES(via=call)`）---

def test_java_jar_invocation_is_dropped_as_shell_jar():
    """`-jar` の main class は構造的に読み取れないため、jar はノード化せず Dropped にする。"""
    res = A.extract_refs("java -jar app.jar\n", "bin/nightly.sh")
    assert res.refs == []
    assert [(d.reason, d.snippet) for d in res.dropped] == [("shell_jar", "app.jar")]


def test_java_dash_d_option_before_jar_is_still_dropped_as_shell_jar():
    res = A.extract_refs("java -Dfoo=bar -jar x.jar\n", "bin/nightly.sh")
    assert res.refs == []
    assert [(d.reason, d.snippet) for d in res.dropped] == [("shell_jar", "x.jar")]


def test_java_dash_d_option_value_does_not_leak_into_main_class():
    """`-Dimpl=com.fake.NotMain` の値は main class ではない——読み飛ばして実際の引数を見る。"""
    res = A.extract_refs("java -Dimpl=com.fake.NotMain com.x.Main\n", "bin/nightly.sh")
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Module", "com.x.Main")
    assert ref.extra == {"via": "call", "qualified": True}


def test_java_classpath_invocation_becomes_qualified_module_reference():
    res = A.extract_refs("java -cp lib com.acme.BatchMain\n", "bin/nightly.sh")
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Module", "com.acme.BatchMain")
    assert ref.extra == {"via": "call", "qualified": True}


def test_java_bare_fqcn_invocation_becomes_qualified_module_reference():
    res = A.extract_refs("java com.acme.Main\n", "bin/nightly.sh")
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Module", "com.acme.Main")
    assert ref.extra == {"via": "call", "qualified": True}


# --- 実行ファイルの単独行呼び出し（COBOL 等）---

def test_bare_dot_slash_program_line_becomes_module_call_reference():
    res = A.extract_refs("./PAYROLL\n", "bin/nightly.sh")
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Module", "PAYROLL")
    assert ref.extra == {"via": "call"}


def test_bare_program_name_alone_on_a_line_becomes_module_call_reference():
    res = A.extract_refs("PAYROLL\n", "bin/nightly.sh")
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Module", "PAYROLL")


def test_program_name_with_extra_tokens_on_the_line_is_not_a_bare_call():
    """「単独行」限定——行に他のトークンがあれば bare exec とは判定しない。"""
    res = A.extract_refs("PAYROLL --verbose\n", "bin/nightly.sh")
    assert res.refs == []


# --- SQL スクリプト実行（ノード化しない）---

def test_sqlplus_at_script_is_dropped_as_shell_sql_script():
    res = A.extract_refs("sqlplus scott/tiger @load.sql\n", "bin/nightly.sh")
    assert res.refs == []
    assert [(d.reason, d.snippet) for d in res.dropped] == [("shell_sql_script", "load.sql")]


def test_psql_dash_f_script_is_dropped_as_shell_sql_script():
    res = A.extract_refs("psql -f load.sql\n", "bin/nightly.sh")
    assert [(d.reason, d.snippet) for d in res.dropped] == [("shell_sql_script", "load.sql")]


def test_mysql_redirect_script_is_dropped_as_shell_sql_script():
    res = A.extract_refs("mysql < load.sql\n", "bin/nightly.sh")
    assert [(d.reason, d.snippet) for d in res.dropped] == [("shell_sql_script", "load.sql")]


# --- 他ランタイム（node は解析、python/perl は未対応）---

def test_node_invocation_becomes_module_call_reference():
    res = A.extract_refs("node batch.js\n", "bin/nightly.sh")
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Module", "batch.js")
    assert ref.extra == {"via": "call", "include_path": "batch.js"}


def test_python_invocation_is_dropped_as_unsupported_runtime():
    res = A.extract_refs("python etl.py\n", "bin/nightly.sh")
    assert res.refs == []
    assert [(d.reason, d.snippet) for d in res.dropped] == [("shell_unsupported_runtime", "python")]


def test_perl_invocation_is_dropped_as_unsupported_runtime():
    res = A.extract_refs("perl legacy.pl\n", "bin/nightly.sh")
    assert [(d.reason, d.snippet) for d in res.dropped] == [("shell_unsupported_runtime", "perl")]


# --- 変数参照（`ACCESSES(via=config_key, key_kind="env")`）---

def test_dollar_brace_variable_reference_becomes_config_key_access():
    res = A.extract_refs('echo "${LOG_DIR}"\n', "bin/nightly.sh")
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("ACCESSES", "Config", "LOG_DIR")
    assert ref.extra == {"via": "config_key", "key_kind": "env"}


def test_dollar_bare_variable_reference_inside_double_quotes_is_still_captured():
    res = A.extract_refs('echo "$LOG_DIR"\n', "bin/nightly.sh")
    assert res.refs[0].name == "LOG_DIR"


def test_percent_variable_reference_becomes_config_key_access_in_bat():
    res = A.extract_refs("echo %LOG_DIR%\n", "bin/run.bat")
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("ACCESSES", "Config", "LOG_DIR")


def test_lowercase_local_variable_is_not_a_config_key_reference():
    res = A.extract_refs("echo $file\n", "bin/nightly.sh")
    assert res.refs == []


def test_special_positional_variables_are_not_config_key_references():
    res = A.extract_refs("echo $1 $@ $? $#\n", "bin/nightly.sh")
    assert res.refs == []


def test_variable_reference_inside_single_quotes_is_not_captured():
    """POSIX のシングルクォートは変数展開しない構文のため、変数参照走査からも除外する。"""
    res = A.extract_refs("echo '$LOG_DIR'\n", "bin/nightly.sh")
    assert res.refs == []


def test_self_defined_variable_reference_is_not_a_config_key_reference():
    """定義側が同一ファイル内の `KEY=` なら自己参照——参照エッジを張らない。"""
    text = "export ENV=prod\necho \"$ENV\"\n"
    res = A.extract_refs(text, "bin/nightly.sh")
    assert res.refs == []


def test_variable_defined_in_another_file_is_not_treated_as_self_reference():
    """同一ファイル内に定義が無ければ通常どおり参照エッジを張る（自己参照除外は同一ファイル限定）。"""
    res = A.extract_refs('echo "$LOG_DIR"\n', "bin/nightly.sh")
    assert len(res.refs) == 1 and res.refs[0].name == "LOG_DIR"


# --- コメント（`#`／`REM`/`::`）---

def test_hash_comment_line_is_ignored():
    res = A.collect_defs("# export ENV=prod\n", "bin/nightly.sh")
    assert res.children == []


def test_hash_comment_does_not_hide_assignment_when_after_unquoted_hash():
    """行末の未引用 `#` 以降はコメントとして切り捨てる（値には含めない）。"""
    res = A.collect_defs("export ENV=prod # inline comment\n", "bin/nightly.sh")
    assert res.children[0].extra["config_value"] == "prod"


def test_bat_rem_comment_line_is_ignored():
    res = A.collect_defs("REM set ENV=prod\n", "bin/run.bat")
    assert res.children == []


def test_bat_double_colon_comment_line_is_ignored():
    res = A.collect_defs(":: set ENV=prod\n", "bin/run.bat")
    assert res.children == []


# --- ヒアドキュメント ---

def test_heredoc_body_is_skipped_and_reported_once():
    text = "cat <<EOF\nexport SHOULD_NOT=beparsed\necho $SHOULD_NOT_VAR\nEOF\necho done\n"
    def_res = A.collect_defs(text, "bin/nightly.sh")
    assert def_res.children == []                     # ヒアドキュメント本文内の代入は拾わない
    ref_res = A.extract_refs(text, "bin/nightly.sh")
    heredoc_dropped = [d for d in ref_res.dropped if d.reason == "shell_heredoc"]
    assert len(heredoc_dropped) == 1
    assert heredoc_dropped[0].line == 1 and heredoc_dropped[0].snippet == "EOF"
    assert ref_res.refs == []                          # 本文内の変数参照も拾わない


def test_heredoc_with_dash_variant_is_also_skipped():
    text = "cat <<-EOF\n  $INSIDE\nEOF\n"
    res = A.extract_refs(text, "bin/nightly.sh")
    assert [d.reason for d in res.dropped] == ["shell_heredoc"]
    assert res.refs == []


def test_heredoc_operator_inside_a_comment_is_not_a_heredoc_start():
    text = "echo hi # <<EOF\necho next\n"
    res = A.extract_refs(text, "bin/nightly.sh")
    assert [d for d in res.dropped if d.reason == "shell_heredoc"] == []


def test_heredoc_operator_inside_a_double_quoted_string_is_not_a_heredoc_start():
    text = 'echo "literal <<EOF"\necho next\n'
    res = A.extract_refs(text, "bin/nightly.sh")
    assert [d for d in res.dropped if d.reason == "shell_heredoc"] == []


# --- コマンド位置の分割（未引用の `;`・`|`・`&&`・`||`）とラッパー ---

def test_command_after_unquoted_and_and_operator_is_still_classified():
    res = A.extract_refs("true && java -cp a.jar:b.jar com.x.Main\n", "bin/nightly.sh")
    java_refs = [r for r in res.refs if r.kind == "Module" and r.name == "com.x.Main"]
    assert len(java_refs) == 1
    assert java_refs[0].extra == {"via": "call", "qualified": True}


def test_command_after_unquoted_pipe_is_still_classified():
    res = A.extract_refs("echo ok | node x.js\n", "bin/nightly.sh")
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Module", "x.js")


def test_exec_wrapper_prefix_is_stripped_before_classification():
    res = A.extract_refs("exec java -cp lib com.acme.BatchMain\n", "bin/nightly.sh")
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Module", "com.acme.BatchMain")


def test_nohup_backgrounded_wrapper_prefix_is_stripped_before_classification():
    res = A.extract_refs("nohup java -jar x.jar &\n", "bin/nightly.sh")
    assert res.refs == []
    assert [(d.reason, d.snippet) for d in res.dropped] == [("shell_jar", "x.jar")]


def test_if_then_command_is_classified_at_the_then_position():
    res = A.extract_refs("if [ -f x ]; then ./RUN; fi\n", "bin/nightly.sh")
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Module", "RUN")


def test_for_do_command_is_classified_at_the_do_position():
    res = A.extract_refs("for x in a b; do ./RUN; done\n", "bin/nightly.sh")
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Module", "RUN")


# --- 先頭代入付きコマンド（`VAR=1 ./run.sh`）---

def test_leading_assignment_before_command_is_config_child_and_command_is_parsed():
    text = "VAR=1 ./run.sh\n"
    def_res = A.collect_defs(text, "bin/nightly.sh")
    assert len(def_res.children) == 1
    assert def_res.children[0].name == "VAR"
    assert def_res.children[0].extra["config_value"] == "1"
    ref_res = A.extract_refs(text, "bin/nightly.sh")
    ref = ref_res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Batch", "run.sh")


def test_env_prefixed_assignment_is_not_config_child_but_command_is_still_parsed():
    text = "env KEY=v ./run.sh\n"
    def_res = A.collect_defs(text, "bin/nightly.sh")
    assert def_res.children == []
    ref_res = A.extract_refs(text, "bin/nightly.sh")
    ref = ref_res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Batch", "run.sh")


# --- 変数参照の拡張形（`${KEY:-d}` 等）とエスケープ除外 ---

def test_brace_variable_default_and_suffix_forms_are_captured_and_escaped_dollar_is_excluded():
    text = 'echo \\$KEY "${OTHER:-default}" "$KEY_SUFFIX"\n'
    res = A.extract_refs(text, "bin/nightly.sh")
    names = {r.name for r in res.refs}
    assert names == {"OTHER", "KEY_SUFFIX"}


def test_brace_variable_length_operator_form_is_captured():
    res = A.extract_refs('echo "${#KEY}"\n', "bin/nightly.sh")
    assert {r.name for r in res.refs} == {"KEY"}


# --- 値なし宣言（`export KEY`）は Config 定義にせず ACCESSES にする ---

def test_export_without_value_is_not_a_config_child():
    res = A.collect_defs("export KEY\n", "bin/nightly.sh")
    assert res.children == []


def test_export_without_value_becomes_config_key_access_reference():
    res = A.extract_refs("export KEY\n", "bin/nightly.sh")
    assert len(res.refs) == 1
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("ACCESSES", "Config", "KEY")
    assert ref.extra == {"via": "config_key", "key_kind": "env"}


def test_readonly_and_declare_x_without_value_also_become_config_key_access():
    res = A.extract_refs("readonly APP_NAME\ndeclare -x DB_HOST\n", "bin/nightly.sh")
    assert {r.name for r in res.refs} == {"APP_NAME", "DB_HOST"}


# --- bat の `set "KEY=value"`（外側引用符は構文）---

def test_bat_quoted_set_assignment_becomes_config_child():
    res = A.collect_defs('set "ENV=dev"\n', "bin/run.bat")
    assert len(res.children) == 1
    assert res.children[0].name == "ENV"
    assert res.children[0].extra == {"config_value": "dev", "key_kind": "env"}


# --- bat の組み込みコマンドは bare exec 判定より先に除外する ---

def test_bat_echo_builtin_alone_is_not_a_bare_module_call():
    res = A.extract_refs("ECHO\n", "bin/run.bat")
    assert res.refs == []


def test_bat_exit_builtin_alone_is_not_a_bare_module_call():
    res = A.extract_refs("EXIT\n", "bin/run.bat")
    assert res.refs == []
