from sherpa.bmod import hop2


class C:
    def __init__(self):
        self.v = hop2()

    def go(self):
        return self.v


def hop3():
    return C().go()
