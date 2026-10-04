package shop.di;

import javax.inject.Named;

@Named
public class NullSink implements Sink {
    public void write(String line) {}
}
