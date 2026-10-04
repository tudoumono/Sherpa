package shop.di;

import org.springframework.stereotype.Component;

@Component
public class CloudStorage implements Storage {
    public void save(String key) {}
}
