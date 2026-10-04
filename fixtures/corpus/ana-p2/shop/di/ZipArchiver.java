package shop.di;

import org.springframework.context.annotation.Primary;
import org.springframework.stereotype.Component;

@Primary
@Component
public class ZipArchiver implements Archiver {
    public void pack(String path) {}
}
