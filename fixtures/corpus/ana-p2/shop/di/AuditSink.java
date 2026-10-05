package shop.di;

import javax.inject.Named;

@Named("auditSink")
public class AuditSink implements Sink {
    public void write(String line) {}
}
