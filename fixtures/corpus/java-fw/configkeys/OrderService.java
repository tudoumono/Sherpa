package com.acme.configkeys;

import org.springframework.beans.factory.annotation.Value;

/**
 * S3'（設定キー参照・A7 案B）の最小サンプル。`tax.rate` は properties/YAML 両方の同名キーへ、
 * `db.url` は env/dev・env/prod の同名キーへ（A9・同一 top_scope 内の同名 `Config` キー全件）。
 */
public class OrderService {

    @Value("${tax.rate}")
    private String taxRate;

    public String dbUrl() {
        return System.getProperty("db.url");
    }

    public String missing() {
        return System.getProperty("no.such.key");
    }
}
