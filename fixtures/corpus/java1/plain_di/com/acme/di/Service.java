package com.acme.di;

/**
 * フレームワーク非依存の実証用（プレーン Java＝DI アノテーション無し）。`spring_di/Service.java`
 * と型宣言だけを揃え、via（`field_type` と `inject`）だけが異なる同一の INVOKES エッジ集合が
 * 立つことを固定する（裁定2026-09-03）。
 */
public class Service {

    private Engine engine;
}
