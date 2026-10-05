static int ping(int n);
static int pong(int n) {
    return ping(n - 1);
}
static int ping(int n) {
    if (n <= 0) {
        return 0;
    }
    return pong(n);
}
int global_seed = 3;
