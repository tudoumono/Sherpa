package com.acme.billing;

import com.acme.tax.TaxCalculator;

/** 別パッケージから TaxCalculator を参照する（cross-package 最近傍解決の検証用）。 */
public class InvoiceService {

    public double totalWithTax(double amount) {
        TaxCalculator calc = new TaxCalculator();
        double rate = TaxCalculator.staticRate();
        // new FakeIgnored(); ← コメント中の偽 CALL（拾わない）
        String note = "new FakeIgnored(); TaxCalculator.fake()"; // 文字列リテラル中の偽 CALL（拾わない）
        return calc.hashCode() == 0 ? amount : amount * (1 + rate) + note.length() * 0;
    }
}
