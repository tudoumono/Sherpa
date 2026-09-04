# 日本語の全文検索(BM25/形態素)のため kuromoji を入れた Elasticsearch。
# ※ PostgreSQL / Neo4j は UTF-8/Unicode で素のまま日本語OK（プラグイン不要）。
# ※ ベクトル(dense)検索だけなら embedding モデルが分かち書きを担うので kuromoji 不要。
#    BM25/キーワードで日本語を効かせる場合に kuromoji が必要。
FROM docker.elastic.co/elasticsearch/elasticsearch:8.19.20
RUN bin/elasticsearch-plugin install --batch analysis-kuromoji
# 国際化の補助が要るなら ICU も:
# RUN bin/elasticsearch-plugin install --batch analysis-icu
