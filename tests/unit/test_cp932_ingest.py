"""日本語原本の分類・索引入力・グラフ定義が文字化けしないことを実ファイルで確かめる。"""
import pytest

from sherpa import corpus_docs, doc_text, es_index
from sherpa.ingest import world_graph


@pytest.mark.parametrize('encoding', ['cp932', 'utf-8', 'utf-8-sig'])
def test_original_text_and_index_chunks_keep_japanese(tmp_path, encoding):
    path = tmp_path / 'sample.cbl'
    source = '       IDENTIFICATION DIVISION.\r\n       PROGRAM-ID. SAMPLE.\r\n      * 架空の集計処理\r\n'
    path.write_bytes(source.encode(encoding))
    doc = {'name': path.name, 'md_path': str(path), 'branch': 'source'}
    expected = source.replace('\r\n', '\n')
    assert doc_text.read_world_doc_text('pytest-encoding', doc) == expected
    chunks, degraded = es_index._iter_doc_chunk_records('pytest-encoding', doc, None, frozenset())
    records = list(chunks)
    assert degraded is None
    assert records[0][1]['text'] == expected.strip()
    assert records[0][1]['line'] == 1
    assert records[0][3] is True  # ソースは埋め込み対象外。
    assert path.read_bytes() == source.encode(encoding)


@pytest.mark.parametrize('name', ['README', 'guide.unknown'])
def test_cp932_unknown_files_are_reachable_text(tmp_path, name):
    path = tmp_path / name
    source = 'これは架空の利用案内です。処理内容を確認してください。\n'
    path.write_bytes(source.encode('cp932'))
    def head(size=4096):
        return corpus_docs._read_head(path, size)
    assert head() == source
    assert corpus_docs.reachable_as_text(name, path.suffix, head)


def test_cp932_graph_preserves_definition_values(tmp_path):
    path = tmp_path / 'sample.properties'
    path.write_bytes('title=架空の集計処理\n'.encode('cp932'))
    nodes, edges, flags = world_graph.build_world(tmp_path, 'pytest-encoding')
    title = next(n for n in nodes if n['name'] == 'title')
    assert title['config_value'] == '架空の集計処理'
    assert any(e['type'] == 'CONTAINS' and e['dst'] == title['cid'] for e in edges)
    assert not flags
