"""`scripts/syntax_profile.py`（S0・構文分布スクリプト・docs/proposals/2026-09-05-アナライザ拡張.md §9）の単体テスト。

読み取り専用・LLM 不使用・world への書込みなしのため、DB/ES/Neo4j は一切使わない。`fixtures/corpus/`
の既存 COBOL/JCL サンプルと、tmp_path に作る最小サンプル（各構文1本ずつ）で主要カウンタが期待値に
なることを確認する。`scripts/` は暗黙の名前空間パッケージとして `import scripts.syntax_profile`
できる（`tests/unit/test_log_report.py` と同じ手法）。
"""
from __future__ import annotations

from pathlib import Path

import scripts.syntax_profile as sp

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_V1 = REPO_ROOT / "fixtures" / "corpus" / "v1"
FIXTURES_JAVA1 = REPO_ROOT / "fixtures" / "corpus" / "java1"


# ---------------------------------------------------------------------------
# 実 fixtures（COBOL/JCL/copybook）を対象にした実測ベースの期待値
# ---------------------------------------------------------------------------

def test_fixtures_v1_cobol_jcl_copybook_counts():
    stats = sp.scan([str(FIXTURES_V1)])
    report = sp.build_report(stats)

    c = report["cobol"]
    # 7 .cbl + 5 .cpy = 12 ファイル（見出しコメントは COPY/CALL/レベル項目の判定対象外）。
    assert c["files_total"] == 12
    assert c["files_fixed_format"] == 12
    assert c["files_free_format"] == 0
    assert c["lines_numbered"] == 0
    assert c["numbered_ratio_pct"] == 0.0
    assert c["continuation_lines"] == 0
    # COPY: BILLGEN(2)+COMMISUP(2)+CUSTMNT(1)+FEECALC(1)+SALESUP(1)+TAXCALC(1) = 8 行・6 ファイル
    assert c["copy"] == {"files": 6, "lines": 8}
    # CALL literal: AGENTPAY/BILLGEN/COMMISUP/SALESUP の4ファイル・各1行
    assert c["call_literal"] == {"files": 4, "lines": 4}
    assert c["call_dynamic"] == {"files": 0, "lines": 0}
    # レベル項目: 5本の .cpy が各 01+05+05 の3行
    assert c["level_item"] == {"files": 5, "lines": 15}
    assert c["exec_sql"] == {"files": 0, "lines": 0}
    assert c["exec_cics"] == {"files": 0, "lines": 0}

    j = report["jcl"]
    assert j["files_total"] == 2
    # MONTHLY(2)+NIGHTLY(2) の EXEC PGM=
    assert j["exec_pgm"] == {"files": 2, "lines": 4}
    assert j["exec_proc_or_named"] == {"files": 0, "lines": 0}
    assert j["include_member"] == {"files": 0, "lines": 0}
    assert j["proc_names_top20"] == []
    assert j["proc_names_top20_resolved_in_world"] == 0

    assert report["by_extension"][".cbl"] == {"files": 7, "lines": 57}
    assert report["by_extension"][".jcl"]["files"] == 2
    assert report["by_extension"][".cpy"]["files"] == 5
    # トップフォルダ（世代）= "4期" 配下1つに全部入る
    assert set(report["by_generation"].keys()) == {"4期"}


def test_fixtures_java1_counts_no_crash_and_files_total():
    stats = sp.scan([str(FIXTURES_JAVA1)])
    report = sp.build_report(stats)
    assert report["java"]["files_total"] == len(list(FIXTURES_JAVA1.rglob("*.java")))
    assert report["totals"]["files_skipped_unreadable"] == 0
    assert report["totals"]["files_skipped_too_large"] == 0


# ---------------------------------------------------------------------------
# tmp_path 最小サンプル
# ---------------------------------------------------------------------------

def test_cobol_numbered_fixed_format_delevel_and_continuation(tmp_path):
    src = (
        "000010 IDENTIFICATION DIVISION.\n"
        "000020 PROGRAM-ID. NUMBERED.\n"
        "000030 DATA DIVISION.\n"
        "000040 WORKING-STORAGE SECTION.\n"
        "000050 01  WS-ITEM.\n"
        "000060     05  WS-FIELD       PIC X(5).\n"
        "000070 PROCEDURE DIVISION.\n"
        "000080-    CALL 'HELPER'.\n"
        "000090     GOBACK.\n"
    )
    (tmp_path / "NUMBERED.cbl").write_text(src, encoding="utf-8")

    report = sp.build_report(sp.scan([str(tmp_path)]))
    c = report["cobol"]
    assert c["files_total"] == 1
    assert c["files_fixed_format"] == 1
    assert c["files_free_format"] == 0
    assert c["lines_total"] == 9
    assert c["lines_numbered"] == 9
    assert c["numbered_ratio_pct"] == 100.0
    assert c["continuation_lines"] == 1
    assert c["call_literal"] == {"files": 1, "lines": 1}
    assert c["level_item"] == {"files": 1, "lines": 2}


def test_cobol_free_format_directive_detected(tmp_path):
    src = (
        "       >>SOURCE FORMAT FREE\n"
        "IDENTIFICATION DIVISION.\n"
        "PROGRAM-ID. FREEPROG.\n"
        "PROCEDURE DIVISION.\n"
        "    CALL 'HELPER'.\n"
    )
    (tmp_path / "FREEPROG.cbl").write_text(src, encoding="utf-8")
    report = sp.build_report(sp.scan([str(tmp_path)]))
    c = report["cobol"]
    assert c["files_fixed_format"] == 0
    assert c["files_free_format"] == 1


def test_cobol_exec_sql_table_extraction(tmp_path):
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. SQLPROG.\n"
        "       PROCEDURE DIVISION.\n"
        "           EXEC SQL\n"
        "               SELECT NAME INTO :WS-NAME FROM CUSTOMER WHERE ID = :WS-ID\n"
        "           END-EXEC.\n"
        "           EXEC SQL\n"
        "               UPDATE ACCOUNT SET BAL = BAL - 100\n"
        "           END-EXEC.\n"
        "           EXEC SQL\n"
        "               INSERT INTO LEDGER VALUES (:WS-ID, :WS-NAME)\n"
        "           END-EXEC.\n"
        "           EXEC SQL\n"
        "               DELETE FROM TEMP_LOG WHERE ID = :WS-ID\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    (tmp_path / "SQLPROG.cbl").write_text(src, encoding="utf-8")
    report = sp.build_report(sp.scan([str(tmp_path)]))
    c = report["cobol"]
    assert c["exec_sql"] == {"files": 1, "lines": 4}
    assert c["exec_sql_ref_keywords"] == {
        "FROM": 1, "UPDATE": 1, "INSERT_INTO": 1, "DELETE_FROM": 1,
    }
    assert dict(c["exec_sql_tables_top20"]) == {
        "CUSTOMER": 1, "ACCOUNT": 1, "LEDGER": 1, "TEMP_LOG": 1,
    }


def test_cobol_exec_cics_xctl_link_other(tmp_path):
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. CICSPROG.\n"
        "       PROCEDURE DIVISION.\n"
        "           EXEC CICS XCTL PROGRAM('NEXTPGM')\n"
        "           END-EXEC.\n"
        "           EXEC CICS LINK PROGRAM('SUBPGM')\n"
        "           END-EXEC.\n"
        "           EXEC CICS RETURN\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    (tmp_path / "CICSPROG.cbl").write_text(src, encoding="utf-8")
    report = sp.build_report(sp.scan([str(tmp_path)]))
    c = report["cobol"]
    assert c["exec_cics"] == {"files": 1, "lines": 3}
    assert c["exec_cics_kind"] == {"xctl": 1, "link": 1, "other": 1}


def test_jcl_proc_call_and_include_and_world_resolution(tmp_path):
    src = (
        "//NIGHTJOB JOB (ACCT),'NIGHT',CLASS=A\n"
        "//STEP1   EXEC PROC=DAILYCLN\n"
        "//STEP2   EXEC MONTHEND\n"
        "//STEP3   EXEC PGM=SORT\n"
        "//        INCLUDE MEMBER=CPYLIB1\n"
    )
    (tmp_path / "NIGHTJOB.jcl").write_text(src, encoding="utf-8")
    # DAILYCLN という名前（拡張子は問わない）のファイルを world 内に置く＝解決対象。
    (tmp_path / "DAILYCLN.txt").write_text("dummy", encoding="utf-8")

    report = sp.build_report(sp.scan([str(tmp_path)]))
    j = report["jcl"]
    assert j["files_total"] == 1
    assert j["exec_pgm"] == {"files": 1, "lines": 1}
    assert j["exec_proc_or_named"] == {"files": 1, "lines": 2}
    assert j["include_member"] == {"files": 1, "lines": 1}
    assert dict(j["proc_names_top20"]) == {"DAILYCLN": 1, "MONTHEND": 1}
    assert j["proc_names_top20_resolved_in_world"] == 1


def test_xml_spring_bean_root_and_class_attr(tmp_path):
    src = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<beans xmlns="http://www.springframework.org/schema/beans">\n'
        '    <bean id="taxCalculator" class="com.acme.tax.TaxCalculator">\n'
        '        <property name="rate" ref="taxRateBean"/>\n'
        '    </bean>\n'
        '</beans>\n'
    )
    (tmp_path / "applicationContext.xml").write_text(src, encoding="utf-8")
    report = sp.build_report(sp.scan([str(tmp_path)]))
    x = report["xml"]
    assert x["files_total"] == 1
    assert dict(x["root_elements_top20"]) == {"beans": 1}
    assert x["class_attr_fqcn"] == {"files": 1, "lines": 1}
    assert x["namespace_attr"] == {"files": 0, "lines": 0}
    assert report["java"]["mybatis"]["mapper_xml_files"] == 0
    assert report["java"]["struts"]["struts_xml_files"] == 0


def test_xml_mybatis_mapper_root_and_namespace(tmp_path):
    src = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN"'
        ' "http://mybatis.org/dtd/mybatis-3-mapper.dtd">\n'
        '<mapper namespace="com.acme.dao.CustomerMapper">\n'
        '    <select id="findById" resultType="com.acme.model.Customer">\n'
        '        SELECT * FROM CUSTOMER WHERE ID = #{id}\n'
        '    </select>\n'
        '</mapper>\n'
    )
    (tmp_path / "CustomerMapper.xml").write_text(src, encoding="utf-8")
    report = sp.build_report(sp.scan([str(tmp_path)]))
    x = report["xml"]
    assert dict(x["root_elements_top20"]) == {"mapper": 1}
    assert x["namespace_attr"] == {"files": 1, "lines": 1}
    assert report["java"]["mybatis"]["mapper_xml_files"] == 1


def test_properties_kv_fqcn_and_placeholder(tmp_path):
    src = (
        "# comment line\n"
        "app.name=MyApp\n"
        "app.version=1.2.3\n"
        "db.driver=com.mysql.cj.jdbc.Driver\n"
        "db.url=${DB_URL}\n"
        "short=ok\n"
    )
    (tmp_path / "application.properties").write_text(src, encoding="utf-8")
    report = sp.build_report(sp.scan([str(tmp_path)]))
    p = report["properties"]
    assert p["files"] == 1
    assert p["keys"] == 5
    assert p["fqcn_like_values"] == 1
    assert p["placeholder_values"] == 1


def test_sql_ddl_create_and_alter(tmp_path):
    src = (
        "CREATE TABLE CUSTOMER (\n"
        "    ID INT PRIMARY KEY,\n"
        "    NAME VARCHAR(100)\n"
        ");\n"
        "\n"
        "CREATE VIEW ACTIVE_CUSTOMER AS SELECT * FROM CUSTOMER WHERE ACTIVE = 1;\n"
        "\n"
        "ALTER TABLE CUSTOMER ADD COLUMN EMAIL VARCHAR(100);\n"
        "\n"
        "CREATE PROCEDURE UPDATE_BALANCE (IN acct INT) BEGIN END;\n"
    )
    (tmp_path / "schema.sql").write_text(src, encoding="utf-8")
    report = sp.build_report(sp.scan([str(tmp_path)]))
    s = report["sql"]
    assert s["files_total"] == 1
    assert s["create_table"] == {"files": 1, "lines": 1}
    assert s["create_view"] == {"files": 1, "lines": 1}
    assert s["create_procedure_or_function"] == {"files": 1, "lines": 1}
    assert s["alter_table"] == {"files": 1, "lines": 1}
    tables = dict(s["tables_top20"])
    assert tables["CUSTOMER"] == 2


def test_c_include_local_and_external(tmp_path):
    src = (
        '#include "myheader.h"\n'
        "#include <stdio.h>\n"
        "\n"
        "int add(int a, int b) {\n"
        "    return a + b;\n"
        "}\n"
    )
    (tmp_path / "sample.c").write_text(src, encoding="utf-8")
    report = sp.build_report(sp.scan([str(tmp_path)]))
    c = report["c_cpp"]
    assert c["files_total"] == 1
    assert c["include_local"] == {"files": 1, "lines": 1}
    assert c["include_external"] == {"files": 1, "lines": 1}


def test_csharp_type_decls_inheritance_and_attributes(tmp_path):
    src = (
        "using System;\n"
        "\n"
        "namespace MyApp.Models\n"
        "{\n"
        "    public class Customer : BaseEntity, IEntity\n"
        "    {\n"
        '        [Route("api/customers")]\n'
        "        [HttpGet]\n"
        "        public DbSet<Customer> Customers { get; set; }\n"
        "    }\n"
        "\n"
        "    public interface IEntity { }\n"
        "    public struct Point { }\n"
        "    public enum Status { Active, Inactive }\n"
        "    public record Money(decimal Amount);\n"
        "}\n"
    )
    (tmp_path / "Customer.cs").write_text(src, encoding="utf-8")
    report = sp.build_report(sp.scan([str(tmp_path)]))
    cs = report["csharp"]
    assert cs["files_total"] == 1
    assert cs["type_decls"] == {"class": 1, "interface": 1, "struct": 1, "enum": 1, "record": 1}
    assert cs["inheritance_lines"] == {"files": 1, "lines": 1}
    assert cs["aspnet_attributes"] == {"files": 1, "lines": 2}
    assert cs["dbset_usages"] == {"files": 1, "lines": 1}


def test_cobol_continuation_spans_copy_call_exec(tmp_path):
    # `CALL '...'` の引用文字列自体が継続行をまたぐ最小例（設計RV指摘・粗い判定のみで検知する）。
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. CONTSPAN.\n"
        "       PROCEDURE DIVISION.\n"
        "           CALL 'LONGPROGRAM\n"
        "      -    NAME'.\n"
        "           GOBACK.\n"
    )
    (tmp_path / "CONTSPAN.cbl").write_text(src, encoding="utf-8")
    report = sp.build_report(sp.scan([str(tmp_path)]))
    c = report["cobol"]
    assert c["continuation_lines"] == 1
    assert c["continuation_spans_copy_call_exec"] == {"files": 1, "lines": 1}
    # 継続行をまたぐ CALL は現行の粗い行単位判定では取りこぼす（S1の論理行正規化が要る根拠そのもの）。
    assert c["call_literal"] == {"files": 0, "lines": 0}


def test_cobol_continuation_not_spanning_copy_call_exec_is_not_counted(tmp_path):
    # 継続行があっても直前行/継続行自身に COPY/CALL/EXEC が無ければ「またぐ」に数えない。
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. PLAINCONT.\n"
        "       PROCEDURE DIVISION.\n"
        "           MOVE 'LONGVALUE\n"
        "      -    TAIL' TO WS-FIELD.\n"
        "           GOBACK.\n"
    )
    (tmp_path / "PLAINCONT.cbl").write_text(src, encoding="utf-8")
    report = sp.build_report(sp.scan([str(tmp_path)]))
    c = report["cobol"]
    assert c["continuation_lines"] == 1
    assert c["continuation_spans_copy_call_exec"] == {"files": 0, "lines": 0}


def test_sql_create_table_distribution_and_dml_only_files(tmp_path):
    (tmp_path / "two_tables.sql").write_text(
        "CREATE TABLE A (ID INT);\nCREATE TABLE B (ID INT);\n", encoding="utf-8")
    (tmp_path / "one_table.sql").write_text("CREATE TABLE C (ID INT);\n", encoding="utf-8")
    (tmp_path / "dml_only.sql").write_text(
        "SELECT * FROM C WHERE ID = 1;\nUPDATE C SET ID = 2 WHERE ID = 1;\n", encoding="utf-8")
    report = sp.build_report(sp.scan([str(tmp_path)]))
    s = report["sql"]
    assert s["files_total"] == 3
    assert s["create_table_per_file"] == {"single": 1, "multiple": 1}
    assert s["dml_only_files"] == 1


def test_xml_config_vs_non_config_root(tmp_path):
    (tmp_path / "applicationContext.xml").write_text(
        '<?xml version="1.0"?>\n<beans xmlns="x"></beans>\n', encoding="utf-8")
    (tmp_path / "data.xml").write_text(
        '<?xml version="1.0"?>\n<dataset><row/></dataset>\n', encoding="utf-8")
    report = sp.build_report(sp.scan([str(tmp_path)]))
    x = report["xml"]
    assert x["files_total"] == 2
    assert x["config_root_files"] == 1
    assert x["non_config_root_files"] == 1


def test_encoding_fallback_cp932_then_latin1(tmp_path):
    (tmp_path / "sjis.properties").write_bytes("key=値".encode("cp932"))
    (tmp_path / "raw.properties").write_bytes(b"key=\x81\xffbad")
    report = sp.build_report(sp.scan([str(tmp_path)]))
    enc = report["totals"]["encoding_counts"]
    assert enc.get("cp932") == 1
    assert enc.get("latin-1") == 1


def test_too_large_file_is_skipped(tmp_path):
    big = tmp_path / "big.sql"
    big.write_bytes(b"-- " + b"x" * (sp.MAX_FILE_BYTES + 1))
    report = sp.build_report(sp.scan([str(tmp_path)]))
    assert report["totals"]["files_skipped_too_large"] == 1
    assert report["sql"]["files_total"] == 0


def test_symlink_not_followed(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "a.sql").write_text("CREATE TABLE T (X INT);\n", encoding="utf-8")
    scan_dir = tmp_path / "scan"
    scan_dir.mkdir()
    link = scan_dir / "linked"
    try:
        link.symlink_to(real_dir, target_is_directory=True)
    except OSError:
        import pytest
        pytest.skip("symlink 作成に失敗する環境（権限不足等）")
    report = sp.build_report(sp.scan([str(scan_dir)]))
    assert report["sql"]["files_total"] == 0


# ---------------------------------------------------------------------------
# --json 出力のキー構造スナップショット
# ---------------------------------------------------------------------------

_EXPECTED_TOP_KEYS = {
    "meta", "totals", "by_extension", "by_generation", "cobol", "jcl", "java",
    "xml", "properties", "yaml", "sql", "c_cpp", "csharp", "config_key_refs",
}


def test_report_top_level_keys_are_stable(tmp_path):
    (tmp_path / "empty.cbl").write_text("       IDENTIFICATION DIVISION.\n", encoding="utf-8")
    report = sp.build_report(sp.scan([str(tmp_path)]))
    assert set(report.keys()) == _EXPECTED_TOP_KEYS
    assert report["meta"] == {"read_only": True, "llm_used": False, "world_writes": False}


def test_cli_main_runs_against_fixtures_and_prints_table(capsys):
    rc = sp.main([str(FIXTURES_V1)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "読み取り専用" in out
    assert "[COBOL]" in out


# ---------------------------------------------------------------------------
# コード内設定キー参照（構文ベース／辞書突合ベース）
# ---------------------------------------------------------------------------

def test_config_key_refs_syntax_and_dict_match(tmp_path):
    (tmp_path / "application.properties").write_text(
        "tax.rate=0.1\napp.name=MyApp\n", encoding="utf-8")
    (tmp_path / "application.yaml").write_text(
        "db:\n"
        "  url: jdbc:mysql://localhost/db\n"
        "  pool:\n"
        "    size: 10\n",
        encoding="utf-8",
    )
    (tmp_path / "Sample.java").write_text(
        "public class Sample {\n"
        '    @Value("${tax.rate}")\n'
        "    private String taxRate;\n"
        "\n"
        "    public void load(Environment env) {\n"
        '        String name = env.getProperty("app.name");\n'
        '        String other = getProperty("unknown.key");\n'
        '        String direct = "db.pool.size";\n'
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "Sample.cs").write_text(
        "using System.Configuration;\n"
        "\n"
        "public class Sample\n"
        "{\n"
        "    public void Load()\n"
        "    {\n"
        '        var x = ConfigurationManager.AppSettings["tax.rate"];\n'
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    report = sp.build_report(sp.scan([str(tmp_path)]))
    ck = report["config_key_refs"]
    # properties(tax.rate/app.name) + yaml(db/db.url/db.pool/db.pool.size) = 6 のユニークキー。
    assert ck["keys_total"] == 6

    # 構文ベース: @Value(java)+env.getProperty(java)+getProperty(java)+AppSettings(cs) の4件・2ファイル。
    assert ck["syntax"]["files"] == 2
    assert ck["syntax"]["lines"] == 4
    assert dict(ck["syntax"]["top"]) == {"tax.rate": 2, "app.name": 1, "unknown.key": 1}

    # 辞書突合ベース: java の "app.name"/"db.pool.size"（構文外のベタ書き文字列）＋cs の "tax.rate" = 3件・2ファイル。
    dm = ck["dict_match"]
    assert dm["files"] == 2
    assert dm["lines"] == 3
    assert dict(dm["top"]) == {"app.name": 1, "db.pool.size": 1, "tax.rate": 1}


def test_config_key_refs_dict_match_unmeasurable_without_config_files(tmp_path):
    (tmp_path / "Sample.java").write_text(
        'public class Sample {\n    @Value("${tax.rate}")\n    private String taxRate;\n}\n',
        encoding="utf-8",
    )
    report = sp.build_report(sp.scan([str(tmp_path)]))
    ck = report["config_key_refs"]
    assert ck["keys_total"] == 0
    assert ck["syntax"]["files"] == 1
    assert ck["syntax"]["lines"] == 1
    assert ck["dict_match"] == "計測不能（キー集合なし）"


def test_cli_main_json_output_is_valid_json(capsys):
    import json
    rc = sp.main([str(FIXTURES_V1), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert set(parsed.keys()) == _EXPECTED_TOP_KEYS
