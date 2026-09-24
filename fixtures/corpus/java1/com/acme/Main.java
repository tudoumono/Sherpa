package com.acme;

/**
 * com.acme.tax.util と com.acme.billing.util の両方から等距離にあるファイル。
 * ambiguous_reference の検証用（Helper は2パッケージに同名で存在する）。
 */
public class Main {
    public void run() {
        Object h = new Helper();
    }
}
