package shop.di;

import org.springframework.stereotype.Component;

@Component
public class UserRepo implements Repo<User> {
    public User find(String id) { return null; }
}
