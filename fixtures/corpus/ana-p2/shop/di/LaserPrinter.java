package shop.di;

import org.springframework.stereotype.Component;

@Component("laser")
public class LaserPrinter implements Printer {
    public void print(String doc) {}
}
