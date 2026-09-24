package com.acme.a;

import org.springframework.beans.factory.annotation.Qualifier;
import javax.annotation.Resource;

/**
 * S3' 追補（設定キー参照の追加構文＝`getBean`/`@Qualifier`/`@Resource(name=...)`）の最小サンプル。
 * `orderService` は `applicationContext.xml` の `<bean id="orderService">`（S3' XML children）へ、
 * `mailer` は同一 top_scope 内に存在しないキーへ（unresolved）。
 */
public class OrderService {

    @Qualifier("orderService")
    private String qualifierHint;

    @Resource(name = "mailer")
    private String mailer;

    public Object lookup() {
        return getBean("orderService");
    }
}
