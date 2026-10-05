package app;

import lib.Target;
import w1.*;
import static lib.Statics.make;

public class Main {
    void run() {
        Object a = new Target();
        Object b = new Local();
        Object c = new Thing();
        Object d = new Missing();
        Object e = new Statics();
        Object f = new Absent();
    }
}
