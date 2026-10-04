package shop.di;

import org.springframework.stereotype.Component;

@Component
public class TwoConstructors {

    private final Notifier notifier;

    public TwoConstructors() {
        this.notifier = null;
    }

    public TwoConstructors(Notifier notifier) {
        this.notifier = notifier;
    }
}
