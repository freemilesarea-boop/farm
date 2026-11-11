import os
import tempfile

# Configure before the app modules are imported.
_tmp = tempfile.mkdtemp(prefix="blc-test-")
os.environ.setdefault("LAB_DB_PATH", os.path.join(_tmp, "test.sqlite3"))
os.environ.setdefault("LAB_PORT", "8799")
os.environ.setdefault("LAB_ITERATION_INTERVAL", "0.5")
os.environ.setdefault("LAB_MAX_CONCURRENT", "10")
# The 4-core CI box legitimately spikes while 10 Chromiums launch; threshold blocking
# is covered explicitly by test_resource_threshold_blocks.
os.environ.setdefault("LAB_CPU_THRESHOLD", "101")
