package com.acme.mybatis;

/**
 * S4' の Table 起点 incoming 探索サンプル: `OrderService -[field_type]-> OrderMapper
 * -[mapper_namespace,reverse]-> OrderMapper.xml(Config) -[mapper_sql]-> Table(ORDERS)` の
 * 逆向き（Table→...→OrderService）が2ホップ超えて届くことを固定する。
 */
public class OrderService {

    private OrderMapper orderMapper;
}
