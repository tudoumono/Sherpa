package com.acme.tax;

/**
 * 税額計算の主体クラス（public＝primary）。
 * 同一ファイル内の非 public 型（RoundingHelper）を伴う。
 */
public class TaxCalculator extends AbstractCalculator implements Taxable {

    private final RoundingHelper rounding = new RoundingHelper();

    @Override
    public double rate() {
        return 0.10;
    }

    public double calc(double amount) {
        // new RoundingHelper() はコメント中なので偽マッチしない
        return rounding.round(amount * rate());
    }

    public static double staticRate() {
        return 0.10;
    }
}

/** 非 public 型（primary 以外の def・Module ノード化される）。 */
class RoundingHelper {
    double round(double v) {
        return Math.round(v * 100.0) / 100.0;
    }
}
