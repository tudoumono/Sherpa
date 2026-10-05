package shop.di;

public abstract class BaseRepo<T> implements Repo<T> {
    public T find(String id) { return null; }
}
