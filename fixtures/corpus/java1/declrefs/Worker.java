package com.acme.declrefs;

import java.util.List;

/**
 * JAVA-2 の受け入れテスト用フィクスチャ（フィールド/コンストラクタ引数/メソッド引数の宣言型・
 * ジェネリクス型引数1段・JDK 型除外を1ファイルで確認する）。
 */
public class Worker {

    private final Engine engine;
    private String label;
    private List<TaxCalc> calcs;

    public Worker(Engine engine, int retries) {
        this.engine = engine;
    }

    public void process(TaxCalc calc, String note) {
    }
}
