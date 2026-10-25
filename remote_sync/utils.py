import os
import sys


verbose = False


def progress(message, *, detail=False):
    if detail and not verbose:
        return
    # Keep progress after any buffered plan output, including when piped.
    sys.stdout.flush()
    print(message, file=sys.stderr, flush=True)


def require_env(name):
    value = os.environ.get(name)
    assert value, f"Set {name} in sopsSecretsFile."
    return value
