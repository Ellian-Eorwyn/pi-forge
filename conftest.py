"""Suite-wide defaults for the Python tests.

Several modules — ``forge_llm.service_doctor``, ``forge_embeddings.embeddings_doctor``,
``harness.served_fingerprint`` — read the deployment's state API to describe what
is behind an endpoint. That read is optional everywhere, but a test suite must
not depend on whether a particular host is up: on a developer machine it is, so
assertions would quietly vary with whatever the stack happens to be serving, and
in CI it is not, so every such call would spend its timeout.

Turning it off by default keeps the suite hermetic. A test that wants the
behaviour sets ``FORGE_STACK_STATE_URL`` at its own stub and clears this, which
is what the ``stack_state`` tests do.
"""

import os


def pytest_configure(config):
    os.environ.setdefault("PI_FORGE_SKIP_STACK_DISCOVERY", "1")
