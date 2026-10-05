package shop.di;

public class PlainHolder {

    private final Notifier notifier;

    public PlainHolder(Notifier notifier) {
        this.notifier = notifier;
    }
}
