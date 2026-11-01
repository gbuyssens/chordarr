import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="chordarr-test-")
os.environ["CHORDARR_DATA_DIR"] = _tmp
os.environ["CHORDARR_API_KEY"] = "test-key"
os.environ["CHORDARR_ROOT_FOLDER"] = os.path.join(_tmp, "music")
os.makedirs(os.environ["CHORDARR_ROOT_FOLDER"], exist_ok=True)
os.environ["CHORDARR_AUTH_DISABLED_FOR_LOCAL"] = "false"
