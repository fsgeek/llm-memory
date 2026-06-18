import configparser
from pathlib import Path

from arango import ArangoClient

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "db-config.ini"


def _load_config(path=_CONFIG_PATH):
    if not path.exists():
        raise FileNotFoundError(f"DB config not found: {path}")
    parser = configparser.ConfigParser()
    parser.read(path)
    return parser["database"]


def get_database(path=_CONFIG_PATH):
    """Return a python-arango Database handle for the scoped llm_memory user.
    Fail-stop: missing config or unreachable server raises rather than degrading."""
    cfg = _load_config(path)
    client = ArangoClient(hosts=f"http://{cfg['host']}:{cfg['port']}")
    return client.db(
        cfg["database"],
        username=cfg["user_name"],
        password=cfg["user_password"],
    )
