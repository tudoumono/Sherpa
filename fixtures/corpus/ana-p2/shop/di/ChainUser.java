package shop.di;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

@Component
public class ChainUser {

    @Autowired
    private Repo<Item> items;

    @Autowired
    public void setMeters(Meter... meters) {
    }
}
