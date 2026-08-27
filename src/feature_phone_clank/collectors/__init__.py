from ..core.registry import collectors
from .doro import DoroCollector
from .hmd import HmdCollector
from .itel import ItelCollector
from .lava import LavaCollector
from .mudita import MuditaCollector
from .punkt import PunktCollector
from .sunbeam import SunbeamCollector
from .tcl_alcatel import TCLAlcatelCollector

collectors.register("hmd-nokia")(HmdCollector)
# EXPERIMENTAL - deliberately absent from config/scope.yaml. Registration
# only makes the collector runnable via `run-experimental`; it never grants
# production access (that gate is config/scope.yaml, checked in
# core/runner.py, not the registry).
collectors.register("itel-india")(ItelCollector)
collectors.register("lava-india")(LavaCollector)
collectors.register("punkt-ch")(PunktCollector)

# Wave 2 (2026-08-27): same experimental-lane contract as above.
collectors.register("doro-gb")(DoroCollector)
collectors.register("mudita-com")(MuditaCollector)
collectors.register("sunbeam-f1-us")(SunbeamCollector)
collectors.register("tcl-alcatel-global")(TCLAlcatelCollector)
