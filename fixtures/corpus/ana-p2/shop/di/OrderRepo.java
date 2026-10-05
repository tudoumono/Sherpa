package shop.di;

import org.springframework.stereotype.Component;

@Component
public class OrderRepo implements Repo<Order> {
    public Order find(String id) { return null; }
}
