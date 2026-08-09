"""String-name -> implementation registry for collectors, mirroring OEM
Radar's `core/registry.py`. Empty of real entries in Stage 1 — the HMD/Nokia
collector (Stage 2) is the first to register here."""

from __future__ import annotations

from typing import Callable, TypeVar

T = TypeVar("T")


class Registry:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._items: dict[str, type] = {}

    def register(self, name: str) -> Callable[[type[T]], type[T]]:
        def decorator(cls: type[T]) -> type[T]:
            if name in self._items:
                raise ValueError(f"{self.kind} {name!r} already registered")
            self._items[name] = cls
            return cls

        return decorator

    def get(self, name: str) -> type:
        try:
            return self._items[name]
        except KeyError:
            raise KeyError(
                f"no {self.kind} named {name!r}; registered: {sorted(self._items)}"
            ) from None

    def __contains__(self, name: str) -> bool:
        return name in self._items

    def names(self) -> list[str]:
        return sorted(self._items)


collectors = Registry("collector")
