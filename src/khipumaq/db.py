import configparser
import os
from pathlib import Path

from arango import ArangoClient

# The source checkout's config/, where every machine kept it before packaging.
# Read last, so a machine still running from a checkout keeps working until its
# config moves to ~/.config/khipumaq/.
_CHECKOUT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "db-config.ini"


def config_path():
    """Where the database config lives: $KHIPUMAQ_CONFIG if set (and then only
    there), else $XDG_CONFIG_HOME/khipumaq/db-config.ini (default ~/.config),
    else the source checkout's config/db-config.ini. Returns the first that
    exists; raises naming every place looked, so a missing config is never a
    silent fallback to somewhere else."""
    if os.environ.get("KHIPUMAQ_CONFIG"):
        candidates = [Path(os.environ["KHIPUMAQ_CONFIG"])]
    else:
        xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        candidates = [xdg / "khipumaq" / "db-config.ini", _CHECKOUT_CONFIG]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(
        "DB config not found; looked in: " + ", ".join(str(p) for p in candidates)
    )


def _load_config(path):
    parser = configparser.ConfigParser()
    parser.read(path)
    return parser["database"]


def get_database(path=None):
    """Return a python-arango Database handle for the scoped llm_memory user.
    Fail-stop: missing config or unreachable server raises rather than degrading."""
    cfg = _load_config(path or config_path())
    client = ArangoClient(hosts=f"http://{cfg['host']}:{cfg['port']}")
    return client.db(
        cfg["database"],
        username=cfg["user_name"],
        password=cfg["user_password"],
    )
