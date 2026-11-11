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
