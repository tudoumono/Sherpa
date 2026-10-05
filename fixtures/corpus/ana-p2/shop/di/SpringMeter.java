package shop.di;

import org.springframework.stereotype.Component;

@Component
public class SpringMeter implements Meter {
    public int read() { return 1; }
}
