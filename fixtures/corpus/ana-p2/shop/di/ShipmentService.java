package shop.di;

import javax.annotation.Resource;
import javax.inject.Inject;
import javax.inject.Named;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.stereotype.Service;

@Service
public class ShipmentService {

    private final Notifier notifier;
    private final Printer printer;

    @Autowired
    private Archiver archiver;

    @Resource(name = "dotPrinter")
    private Printer dotPrinter;

    @Inject
    @Named("auditSink")
    private Sink sink;

    private Storage storage;

    public ShipmentService(Notifier notifier, @Qualifier("laser") Printer printer) {
        this.notifier = notifier;
        this.printer = printer;
    }

    @Inject
    public void setStorage(Storage storage) {
        this.storage = storage;
    }
}
