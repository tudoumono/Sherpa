package shop.di;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

@Component
public class WidgetUser {

    @Autowired
    private Meter meter;

    @Autowired
    private Repo<User> users;

    @Autowired
    private Repo<?> anyRepo;
}
