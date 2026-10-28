import sys
from importlib.metadata import entry_points

from vllm_private_logging.patch import is_registered, register

PLUGIN_GROUP = "vllm.general_plugins"
PLUGIN_NAME = "private_logging"


def main() -> int:
    names = {ep.name for ep in entry_points(group=PLUGIN_GROUP)}
    if PLUGIN_NAME not in names:
        print(f"Plugin {PLUGIN_NAME} is not installed", file=sys.stderr)
        return 1
    register()
    if not is_registered():
        print("Private logging patch was not applied", file=sys.stderr)
        return 1
    print("Private logging plugin is active")
    return 0


if __name__ == "__main__":
    sys.exit(main())
