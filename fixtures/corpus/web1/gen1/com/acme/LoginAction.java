package com.acme;

import org.springframework.beans.factory.annotation.Value;

public class LoginAction {

    @Value("${login}")
    private String loginHandlerClass;

    public String execute() {
        return "success";
    }
}
