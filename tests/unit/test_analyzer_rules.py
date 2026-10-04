"""宣言的なルールファイル（TOML）の契約: 適用・未適用・読み込みの失敗・署名（ANA-15 P4・docs/21 §3c）。"""
from __future__ import annotations

from pathlib import Path

import pytest

import scripts.verify_extension as verify_extension
from sherpa.ingest import world_graph
from sherpa.ingest.analyzers import _base, _rules, registry

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "analyzer_rules"
_FILES = ["app/src/AcmeController.java", "app/src/com/example/acme/OrderService.java", "app/conf/acme-beans.xml", "app/conf/other-mapper.xml"]


def _build(monkeypatch, plugins, root=FIX):
    monkeypatch.setattr(registry, "FW_PLUGINS", tuple(plugins))
    nodes, edges, flags = world_graph.build_world(root, "w", files=[(root / f, f) for f in _FILES])[:3]
    return nodes, edges, flags


def _keys(edges):
    return {(e["src"].rsplit("/", 1)[-1].split("#")[0], e["type"], e["dst"].rsplit("/", 1)[-1].split("#")[0], e.get("via")) for e in edges}


def _copy_rules(tmp_path, transform=None) -> Path:
    text = (FIX / "rules_acme.toml").read_text(encoding="utf-8")
    (tmp_path / "rules_acme.toml").write_text(transform(text) if transform else text, encoding="utf-8")
    return tmp_path


def test_example_rules_add_api_entry_and_class_ref_only_where_they_apply(monkeypatch):
    plugins = registry.discover_fw_plugins(FIX)
    assert [p.name for p in plugins] == ["rules:acme"] and plugins[0].order == 200
    nodes0, edges0, _ = _build(monkeypatch, [])
    nodes1, edges1, flags1 = _build(monkeypatch, plugins)
    blob0, blob1 = repr(nodes0), repr(nodes1)
    assert "key:url:/orders" not in blob0 and "key:url:/orders" in blob1          # @AcmeApi の付いたメソッドだけ API の入口になる
    assert "/ignored" not in blob1
    added = _keys(edges1) - _keys(edges0)
    assert ("acme-beans.xml", "INVOKES", "OrderService.java", "bean_class") in added          # <beans> の中の要素
    assert not any(a[0] == "other-mapper.xml" for a in added)                             # <mapper> には適用しない（root の指定）
    assert any(f["reason"] == "dropped_syntax" and f["why"] == "rules:acme: rule_missing_attribute:acme-service-class"
               for f in flags1)                                                            # class の無い要素は黙って落とさず申告する
    assert not [f for f in flags1 if f["reason"] == "plugin_failed"]


def test_signature_changes_when_one_rule_line_changes_without_a_version_bump(tmp_path):
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    _copy_rules(base_dir)
    changed_dir = tmp_path / "changed"
    changed_dir.mkdir()
    _copy_rules(changed_dir, lambda t: t.replace('(#eq? @n "AcmeApi")', '(#eq? @n "AcmeApi2")'))
    bumped_dir = tmp_path / "bumped"
    bumped_dir.mkdir()
    _copy_rules(bumped_dir, lambda t: t.replace('id = "acme-api-entry"\nversion = 1', 'id = "acme-api-entry"\nversion = 2'))
    (p0,), (p1,), (p2,) = (registry.discover_fw_plugins(d) for d in (base_dir, changed_dir, bumped_dir))
    assert p0.version == p1.version == 1                                                  # プラグインの version は上げていない
    assert p0.signature_extra != p1.signature_extra and p0.signature_extra[0] != p1.signature_extra[0]
    assert p0.signature_extra[1] == p1.signature_extra[1] == (("acme-api-entry", 1), ("acme-service-class", 1))
    assert p2.signature_extra[1] == (("acme-api-entry", 2), ("acme-service-class", 1))   # ルールの版もハッシュとは別に載る


def test_config_signature_carries_rule_material_and_moves_with_the_rule_file(monkeypatch, tmp_path):
    def signature(d):
        monkeypatch.setattr(registry, "FW_PLUGINS", registry.discover_fw_plugins(d))
        return registry.config_signature()
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    _copy_rules(a)
    _copy_rules(b, lambda t: t.replace('attribute = "class"', 'attribute = "type"'))
    sa, sb = signature(a), signature(b)
    assert sa != sb and sa[:2] == sb[:2]                                                  # ルール 1 行で署名が変わる（本体の材料は不変）
    item = next(i for i in sa[2] if i[0] == "rules:acme")
    assert item[:5] == ("rules:acme", 1, ("java", "xml_config"), (), 200) and item[5][0].startswith("sha256:")
    assert signature(a) == sa                                                             # 同じ内容なら同じ（決定的）


@pytest.mark.parametrize("transform,needle", [
    (lambda t: t + "\n[[rule\n", "TOML を読めません"),
    (lambda t: t.replace('(#eq? @n "AcmeApi"))', '(#eq? @n "AcmeApi")'), "クエリを読めません"),
    (lambda t: t.replace("@value)))", "@other)))"), "@value"),
    (lambda t: t.replace('via = "bean_class"', 'via = "no_such_via"'), "via"),
    (lambda t: t.replace('kind = "Module"', 'kind = "Concept"'), "kind"),
    (lambda t: t.replace('edge_type = "INVOKES"', 'edge_type = "REALIZES"'), "edge_type"),
    (lambda t: t.replace('key_kind = "url"', 'key_kind = "mystery"'), "key_kind"),
    (lambda t: t.replace('key_kind = "url"', ""), "key_kind"),
    (lambda t: t.replace('languages = ["java", "xml_config"]', 'languages = ["java", "nosuch"]'), "未登録のアナライザ名"),
    (lambda t: t.replace('name = "acme"', 'name = "other"', 1), "ファイル名"),
    (lambda t: t.replace('order = 200', 'order = "1"'), "order"),
    (lambda t: t.replace('id = "acme-service-class"', 'id = "acme-api-entry"'), "重複"),
    (lambda t: t.replace('element = "service"', 'element = "service"\nunknown_key = 1'), "未知のキー"),
    (lambda t: t.replace('languages = ["java", "xml_config"]', 'languages = ["java", "xml_config"]\nconfig_kinds = ["spring_beans"]'), "config_kinds"),
    (lambda t: t.replace('version = 1\norder', 'version = 0\norder'), "version"),
    (lambda t: t.replace('id = "acme-api-entry"\nversion = 1', 'id = "acme-api-entry"'), "version"),
    (lambda t: t.replace('via = "bean_class"', 'via = "exec_sql"'), "出せる (edge_type, kind)"),
    (lambda t: t.replace('edge_type = "INVOKES"', 'edge_type = "ACCESSES"'), "出せる (edge_type, kind)"),
    (lambda t: t.replace("(string_literal) @value", "(identifier) @value"), "string_literal"),
    (lambda t: t.replace('via = "bean_class"', 'via = "include"').replace('kind = "Module"', 'kind = "Config"'), "出せる (edge_type, kind)"),
    (lambda t: t.replace('via = "bean_class"', 'via = ["bean_class"]'), "文字列にしてください"),
    (lambda t: t.replace('kind = "Module"', 'kind = ["Module"]'), "文字列にしてください"),
    (lambda t: t.replace('edge_type = "INVOKES"', 'edge_type = ["INVOKES"]'), "文字列にしてください"),
    (lambda t: t.replace('key_kind = "url"', 'key_kind = ["url"]'), "文字列にしてください"),
    (lambda t: t.replace('language = "java"', 'language = ["java"]'), "文字列にしてください"),
    (lambda t: t.replace('emit = "def"', 'emit = ["def"]'), "文字列にしてください"),
    (lambda t: t.replace('name_form = "qualified"', 'name_form = ["qualified"]'), "文字列にしてください"),
])
def test_broken_rule_files_fail_loudly_at_registration(tmp_path, transform, needle):
    _copy_rules(tmp_path, transform)
    with pytest.raises(registry.FwPluginError) as ei:
        registry.discover_fw_plugins(tmp_path)
    assert needle in str(ei.value) and "rules_acme.toml" in str(ei.value)


def test_rule_file_registers_alongside_python_plugins_and_rejects_name_collision(tmp_path):
    _copy_rules(tmp_path)
    (tmp_path / "rules_dup.py").write_text(
        "from sherpa.ingest.analyzers._base import FwPlugin\n"
        "class P(FwPlugin):\n    name = 'rules:acme'\n    languages = frozenset({'xml_config'})\n    version = 1\n"
        "FW_PLUGINS = [P()]\n", encoding="utf-8")
    with pytest.raises(registry.FwPluginError, match="重複"):
        registry.discover_fw_plugins(tmp_path)


def test_exception_while_applying_rules_is_reported_as_plugin_failure_and_keeps_base(monkeypatch):
    (plugin,) = registry.discover_fw_plugins(FIX)
    monkeypatch.setattr(_rules._ts, "parse", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    text = (FIX / "app/src/AcmeController.java").read_text(encoding="utf-8")
    out, failures = registry.apply_fw_defs([plugin], text, "app/src/AcmeController.java", _base.DefResult())
    assert [(f.plugin, f.phase) for f in failures] == [("rules:acme", "defs")] and out.children == []


def test_verify_extension_accepts_a_registered_rule_plugin(monkeypatch):
    rules = registry.discover_fw_plugins(FIX)
    monkeypatch.setattr(registry, "FW_PLUGINS", registry._UPSTREAM_FW_PLUGINS + rules)
    monkeypatch.setattr(registry, "discover_fw_plugins", lambda *a, **k: rules)
    assert verify_extension._check_fw_plugins(registry, _base) == []


def test_symlinked_rule_file_is_rejected(tmp_path):
    real = tmp_path / "outside"
    real.mkdir()
    _copy_rules(real)
    inside = tmp_path / "analyzers"
    inside.mkdir()
    (inside / "rules_acme.toml").symlink_to(real / "rules_acme.toml")
    with pytest.raises(registry.FwPluginError, match="シンボリックリンク"):
        registry.discover_fw_plugins(inside)


def test_same_config_key_from_another_plugin_keeps_the_first_and_reports_the_later(monkeypatch):
    (plugin,) = registry.discover_fw_plugins(FIX)
    text = (FIX / "app/src/AcmeController.java").read_text(encoding="utf-8")
    first = _base.DefItem("Config", "/orders", line=9, cid_key="key:url:/orders", extra={"key_kind": "url"})
    base = _base.DefResult(primary=_base.DefItem("Module", "AcmeController"), children=[first])
    out, failures = registry.apply_fw_defs([plugin], text, "app/src/AcmeController.java", base)
    assert failures == [] and [(c.key, c.line) for c in out.children] == [("key:url:/orders", 9)]
    assert [(d.reason, d.snippet) for d in out.dropped] == [("rules:acme: plugin_duplicate_def", "key:url:/orders")]
