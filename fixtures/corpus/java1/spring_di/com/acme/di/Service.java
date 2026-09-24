package com.acme.di;

/**
 * フレームワーク非依存の実証用（Spring 風＝DI アノテーション付き）。`plain_di/Service.java` と
 * 型宣言だけを揃え、via（`inject` と `field_type`）だけが異なる同一の INVOKES エッジ集合が
 * 立つことを固定する（裁定2026-09-03）。
 */
public class Service {

    @Autowired
    private Engine engine;
}
