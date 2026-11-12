import os
import tempfile

# Tests must not depend on the operator's shell or on the machine's lab.env
# (e.g. LAB_MAX_CONCURRENT=50 would make the concurrency test launch 52 browsers),
# so every setting used by the suite is pinned here, before the app is imported.
_tmp = tempfile.mkdtemp(prefix="blc-test-")
os.environ["LAB_ENV_FILE"] = os.path.join(_tmp, "no-lab.env")  # ignore lab.env
os.environ["LAB_DB_PATH"] = os.path.join(_tmp, "test.sqlite3")
os.environ["LAB_PORT"] = "8799"
os.environ["LAB_ITERATION_INTERVAL"] = "0.5"
os.environ["LAB_MAX_CONCURRENT"] = "10"
os.environ["LAB_MAX_SESSIONS"] = "100"
# The 4-core CI box legitimately spikes while 10 Chromiums launch; threshold blocking
# is covered explicitly by test_resource_threshold_blocks.
os.environ["LAB_CPU_THRESHOLD"] = "101"
os.environ["LAB_RAM_THRESHOLD"] = "85"
os.environ["LAB_PRESSURE_BLOCK_LEVEL"] = "warn"
# Custom-URL policy for tests: a fake allow-listed host pinned to 127.0.0.1 (needs the
# private-network opt-in) plus a resource-only host. Ownership verification is enabled
# explicitly in the tests that cover it.
os.environ["LAB_CUSTOM_ALLOWED_HOSTS"] = "allowed.test"
os.environ["LAB_CUSTOM_RESOURCE_HOSTS"] = "cdn.allowed.test"
# mine.test is NOT allow-listed: it is used to test registering a host from the dashboard.
os.environ["LAB_CUSTOM_HOST_OVERRIDES"] = "allowed.test=127.0.0.1,cdn.allowed.test=127.0.0.1,mine.test=127.0.0.1"
os.environ["LAB_CUSTOM_ALLOW_PRIVATE_NETWORK"] = "1"
os.environ["LAB_CUSTOM_REQUIRE_VERIFICATION"] = "0"
os.environ["LAB_CUSTOM_MIN_RELOAD_S"] = "1"
