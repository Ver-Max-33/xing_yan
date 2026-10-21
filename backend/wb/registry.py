"""Extension registries (spec §3.5, mechanism 1).

Each extension point is a dict filled by a decorator. `autoload` imports every
module under a package so the decorators run; a new file is picked up without
touching existing code.
"""
import importlib
import pkgutil


class Registry(dict):
    def __init__(self, name: str):
        super().__init__()
        self.name = name

    def register(self, key: str):
        def deco(obj):
            if key in self:
                raise KeyError(f"{self.name}: duplicate key {key!r}")
            self[key] = obj
            return obj

        return deco


PROVIDERS = Registry("PROVIDERS")
FUNCTIONS = Registry("FUNCTIONS")  # formula functions (engine/l1/functions)
PRIMITIVES = Registry("PRIMITIVES")  # structure primitives (engine/l2/primitives)
CHANNELS = Registry("CHANNELS")  # notification channels (notify/); V1 has Feishu only
REALTIME = Registry("REALTIME")  # intraday sources (realtime/providers), routed per data profile (P5)
# AI_TASKS is added by the phase that needs it.
ALL = (PROVIDERS, FUNCTIONS, PRIMITIVES, CHANNELS, REALTIME)


def autoload(package: str) -> None:
    """Import every module under `package` so its @register decorators run."""
    pkg = importlib.import_module(package)
    for mod in pkgutil.walk_packages(pkg.__path__, prefix=package + "."):
        importlib.import_module(mod.name)


PACKAGES = ("wb.data.providers", "wb.engine.l1.functions", "wb.engine.l2", "wb.notify", "wb.realtime.providers")  # where entries register


def load_all() -> dict[str, int]:
    """Import every package that registers entries; -> the counts."""
    for p in PACKAGES:
        autoload(p)
    return counts()


def counts() -> dict[str, int]:
    """Entry count per registry; logged at startup and compared after packaging (spec §3.5)."""
    return {r.name: len(r) for r in ALL}
