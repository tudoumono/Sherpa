package shop.di;

import org.springframework.stereotype.Component;

@Component
public class MailNotifier implements Notifier {
    public void send(String to) {}
}
