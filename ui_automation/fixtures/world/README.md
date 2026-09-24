# UI Automation Evidence World

このフォルダは実サービス UI 自動試験が Sherpa に実際に登録・取り込みする小型 World です。
回答を差し替える仕組みではありません。

固有照合語は `SHERPA-LIVE-ALPHA-927` です。ALPHA-927 の承認者は品質保証部の星野です。
根拠資料は `specs/tax-policy.md`、参考資料は `operations/nightly-runbook.md` です。

`office/`、`media/`、`legacy/` には、決定的な照合語を埋め込んだ実際のOOXML、PDF、画像、
旧Office文書があります。取り込み後は変換来歴、検索本文、OCR観測を原本hashと照合します。
