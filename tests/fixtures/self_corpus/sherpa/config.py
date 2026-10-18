import os

from .env_int import env_int

ENV_NAME = "SHERPA_CONST"


def load():
    a = os.environ.get("SHERPA_ALPHA")
    b = os.getenv("SHERPA_BETA", "x")
    c = os.environ["SHERPA_GAMMA"]
    d = env_int("SHERPA_DELTA", 1)
    e = os.environ.get(ENV_NAME)
    present = "SHERPA_PRESENT" in os.environ
    other = os.environ.get("HOME")
    suffix = "A"
    dynamic = os.environ.get(f"SHERPA_{suffix}")
    os.environ["SHERPA_WRITE"] = "1"
    print("SHERPA_ONLY_IN_MESSAGE is not read")
    return a, b, c, d, e, present, other, dynamic
