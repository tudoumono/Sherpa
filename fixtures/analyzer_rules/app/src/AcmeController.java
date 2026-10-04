package com.example.acme;

public class AcmeController {
    @AcmeApi("/orders")
    public void list() {
    }

    @Other("/ignored")
    public void other() {
    }

    public void plain() {
    }
}
