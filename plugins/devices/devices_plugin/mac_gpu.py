"""This Mac's GPU, read in-process from the I/O Registry: ctypes into IOKit and CoreFoundation.

Every graphics accelerator publishes a `PerformanceStatistics` dictionary on its IOAccelerator
entry; `ioreg -c IOAccelerator` prints the same one. Reading it is a registry lookup, tens of
microseconds, so it rides the 2 s sample like the CPU and memory calls beside it (`mac.py` says
why nothing here forks). What the two numbers mean: probe.py.

A Mac without the frameworks, without an accelerator, or with a driver that publishes other
names reads as "no GPU" (None): the row then draws no GPU readout, never a zero.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from functools import lru_cache

from devices_plugin.probe import Gpu

ACCELERATOR_CLASS = b"IOAccelerator"
STATISTICS_KEY = b"PerformanceStatistics"
UTILIZATION_KEY = b"Device Utilization %"
MEMORY_KEY = b"In use system memory"

# <CoreFoundation/CFString.h>, <CoreFoundation/CFNumber.h>, <IOKit/IOKitLib.h>
CF_STRING_ENCODING_UTF8 = 0x08000100
CF_NUMBER_SINT64 = 4
IO_MAIN_PORT_DEFAULT = 0
KERN_SUCCESS = 0

io_object_t = ctypes.c_uint32


class _Registry:
    """The two frameworks, bound once, and the three keys as CFStrings that live as long as the process."""

    def __init__(self) -> None:
        iokit = ctypes.CDLL(ctypes.util.find_library("IOKit") or "/System/Library/Frameworks/IOKit.framework/IOKit")
        cf = ctypes.CDLL(
            ctypes.util.find_library("CoreFoundation") or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        iokit.IOServiceMatching.restype = ctypes.c_void_p
        iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
        iokit.IOServiceGetMatchingServices.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(io_object_t)]
        iokit.IOIteratorNext.restype = io_object_t
        iokit.IOIteratorNext.argtypes = [io_object_t]
        iokit.IOObjectRelease.argtypes = [io_object_t]
        iokit.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
        iokit.IORegistryEntryCreateCFProperty.argtypes = [io_object_t, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        cf.CFNumberGetTypeID.restype = ctypes.c_ulong
        cf.CFDictionaryGetTypeID.restype = ctypes.c_ulong
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.iokit, self.cf = iokit, cf
        self.number_type, self.dictionary_type = cf.CFNumberGetTypeID(), cf.CFDictionaryGetTypeID()
        self.statistics, self.utilization, self.memory = (
            cf.CFStringCreateWithCString(None, key, CF_STRING_ENCODING_UTF8)
            for key in (STATISTICS_KEY, UTILIZATION_KEY, MEMORY_KEY)
        )

    def _number(self, dictionary: int, key: int) -> int | None:
        """A dictionary's value as an integer; None when it is absent or not a number. The value is borrowed."""
        value = self.cf.CFDictionaryGetValue(dictionary, key)
        if not value or self.cf.CFGetTypeID(value) != self.number_type:
            return None
        out = ctypes.c_int64(0)
        return out.value if self.cf.CFNumberGetValue(value, CF_NUMBER_SINT64, ctypes.byref(out)) else None

    def accelerators(self) -> list[tuple[int | None, int | None]]:
        """`(utilisation %, bytes in use)` of every accelerator that publishes statistics."""
        iterator = io_object_t(0)
        # The matching dictionary is consumed by the call: it is never released here.
        matching = self.iokit.IOServiceMatching(ACCELERATOR_CLASS)
        if self.iokit.IOServiceGetMatchingServices(IO_MAIN_PORT_DEFAULT, matching, ctypes.byref(iterator)) != KERN_SUCCESS:
            return []
        found: list[tuple[int | None, int | None]] = []
        try:
            while entry := self.iokit.IOIteratorNext(iterator.value):
                try:
                    statistics = self.iokit.IORegistryEntryCreateCFProperty(entry, self.statistics, None, 0)
                    if not statistics:
                        continue
                    try:
                        if self.cf.CFGetTypeID(statistics) == self.dictionary_type:
                            found.append((self._number(statistics, self.utilization), self._number(statistics, self.memory)))
                    finally:
                        self.cf.CFRelease(statistics)
                finally:
                    self.iokit.IOObjectRelease(entry)
        finally:
            self.iokit.IOObjectRelease(iterator.value)
        return found


@lru_cache(maxsize=1)
def _registry() -> _Registry | None:
    try:
        return _Registry()
    except (OSError, AttributeError):  # no framework, or one without these symbols: this Mac reports no GPU
        return None


def fold(accelerators: list[tuple[int | None, int | None]]) -> Gpu | None:
    """Several accelerators as one readout: the busiest one's utilisation, their memory summed (probe.py)."""
    pcts = [pct for pct, _used in accelerators if pct is not None]
    if not pcts:
        return None
    used = [used for _pct, used in accelerators if used is not None]
    return Gpu(pct=float(min(max(0, *pcts), 100)), mem_used=sum(used) if used else None)


def read_gpu() -> Gpu | None:
    """One sample of this Mac's GPU, or None when it reports none."""
    registry = _registry()
    return fold(registry.accelerators()) if registry is not None else None
