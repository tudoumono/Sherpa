package shop.di;

import org.springframework.stereotype.Component;

@Component
public class DiskStorage implements Storage {
    public void save(String key) {}
}
