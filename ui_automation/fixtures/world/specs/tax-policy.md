# 税計算ポリシー

文書識別子: SHERPA-LIVE-ALPHA-927

2026年度の検証用税率（`TEST-TAX-RATE`）は 12.5% です。端数は明細ごとに切り捨てます。
この値を利用するプログラムは `programs/TAXCALC.cbl` で、夜間処理 `jobs/NIGHTLY.jcl` から呼び出されます。

この文書だけを税率と端数処理の根拠として扱います。
