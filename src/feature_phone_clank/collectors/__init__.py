from ..core.registry import collectors
from .hmd import HmdCollector
from .itel import ItelCollector
from .lava import LavaCollector

collectors.register("hmd-nokia")(HmdCollector)
# EXPERIMENTAL — deliberately absent from config/scope.yaml. Registration
# only makes the collector runnable via `run-experimental`; it never grants
# production access (that gate is config/scope.yaml, checked in
# core/runner.py, not the registry).
collectors.register("itel-india")(ItelCollector)
collectors.register("lava-india")(LavaCollector)
