from ..core.registry import collectors
from .hmd import HmdCollector

collectors.register("hmd-nokia")(HmdCollector)
