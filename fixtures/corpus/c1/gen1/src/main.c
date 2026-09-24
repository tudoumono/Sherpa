#include "util.h"
#include "../inc/log.h"
#include <stdio.h>

#define MAX(a, b) ((a) > (b) ? (a) : (b))

int (*cb)(int);

int main(void) {
    int r = util_add(1, 2);
    int m = MAX(r, 3);
    (*cb)(1);
    return m;
}
