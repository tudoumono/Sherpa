package shop.di;

public interface Repo<T> {
    T find(String id);
}
