import os


def env_int(name, default):
    return int(os.environ.get(name, default))
