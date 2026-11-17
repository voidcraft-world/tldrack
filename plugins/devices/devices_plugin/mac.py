"""This machine's counters, read in-process: ctypes into the Mach host calls on macOS, /proc on Linux.

No subprocess per sample. The bridge is a long-lived process on a machine every agent shares,
so a 2 s sample that forked `vm_stat` and `top` would cost more than the readout is worth;
the kernel calls below cost microseconds. The meaning of each number: probe.py.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
from functools import lru_cache

from devices_plugin.probe import Reading, Ticks, linux_memory, linux_ticks, mac_used_bytes

# <mach/host_info.h>
HOST_CPU_LOAD_INFO = 3
HOST_VM_INFO64 = 4
CPU_STATE_USER, CPU_STATE_SYSTEM, CPU_STATE_IDLE, CPU_STATE_NICE = 0, 1, 2, 3
KERN_SUCCESS = 0

natural_t = ctypes.c_uint32


class VmStatistics64(ctypes.Structure):
    """`struct vm_statistics64` (<mach/vm_statistics.h>), field for field; only four are read."""

    _fields_ = [
        ("free_count", natural_t),
        ("active_count", natural_t),
        ("inactive_count", natural_t),
        ("wire_count", natural_t),
        ("zero_fill_count", ctypes.c_uint64),
        ("reactivations", ctypes.c_uint64),
        ("pageins", ctypes.c_uint64),
        ("pageouts", ctypes.c_uint64),
        ("faults", ctypes.c_uint64),
        ("cow_faults", ctypes.c_uint64),
        ("lookups", ctypes.c_uint64),
        ("hits", ctypes.c_uint64),
        ("purges", ctypes.c_uint64),
        ("purgeable_count", natural_t),
        ("speculative_count", natural_t),
        ("decompressions", ctypes.c_uint64),
        ("compressions", ctypes.c_uint64),
        ("swapins", ctypes.c_uint64),
        ("swapouts", ctypes.c_uint64),
        ("compressor_page_count", natural_t),
        ("throttled_count", natural_t),
        ("external_page_count", natural_t),
        ("internal_page_count", natural_t),
        ("total_uncompressed_pages_in_compressor", ctypes.c_uint64),
    ]


class _Mach:
    """libSystem's host calls, bound once. `mach_host_self()` hands out a port right per call,
    so it is taken once here rather than per sample."""

    def __init__(self) -> None:
        lib = ctypes.CDLL(ctypes.util.find_library("System") or "/usr/lib/libSystem.B.dylib")
        lib.mach_host_self.restype = ctypes.c_uint32
        lib.host_statistics.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(natural_t)]
        lib.host_statistics64.argtypes = lib.host_statistics.argtypes
        lib.sysctlbyname.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        self.lib = lib
        self.host = lib.mach_host_self()
        try:
            self.page_size = ctypes.c_size_t.in_dll(lib, "vm_kernel_page_size").value
        except ValueError:
            self.page_size = os.sysconf("SC_PAGESIZE")
        self.mem_total = self._sysctl_u64(b"hw.memsize")

    def _sysctl_u64(self, name: bytes) -> int | None:
        value = ctypes.c_uint64(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        if self.lib.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
            return None
        return value.value

    def ticks(self) -> Ticks | None:
        load = (natural_t * 4)()
        count = natural_t(4)
        if self.lib.host_statistics(self.host, HOST_CPU_LOAD_INFO, load, ctypes.byref(count)) != KERN_SUCCESS:
            return None
        total = sum(load)
        return Ticks(busy=total - load[CPU_STATE_IDLE], total=total)

    def mem_used(self) -> int | None:
        stats = VmStatistics64()
        count = natural_t(ctypes.sizeof(stats) // ctypes.sizeof(ctypes.c_int))
        if self.lib.host_statistics64(self.host, HOST_VM_INFO64, ctypes.byref(stats), ctypes.byref(count)) != KERN_SUCCESS:
            return None
        return mac_used_bytes(
            stats.internal_page_count, stats.purgeable_count, stats.wire_count, stats.compressor_page_count, self.page_size
        )


@lru_cache(maxsize=1)
def _mach() -> _Mach:
    return _Mach()


def _os_label() -> str:
    return f"{platform.system()} {platform.machine()}"


def _read_mac() -> Reading:
    mach = _mach()
    return Reading(os=_os_label(), cores=os.cpu_count(), ticks=mach.ticks(), mem_used=mach.mem_used(), mem_total=mach.mem_total)


def _read_linux() -> Reading:
    with open("/proc/stat", encoding="ascii") as stat:
        ticks = linux_ticks(stat.readline())
    total_kb = available_kb = None
    with open("/proc/meminfo", encoding="ascii") as meminfo:
        for line in meminfo:
            key, _, rest = line.partition(":")
            if key == "MemTotal":
                total_kb = int(rest.split()[0])
            elif key == "MemAvailable":
                available_kb = int(rest.split()[0])
                break
    mem_used, mem_total = linux_memory(total_kb, available_kb)
    return Reading(os=_os_label(), cores=os.cpu_count(), ticks=ticks, mem_used=mem_used, mem_total=mem_total)


def read_local() -> Reading:
    """One sample of this machine. Raises OSError on a platform with neither source."""
    system = platform.system()
    if system == "Darwin":
        return _read_mac()
    if system == "Linux":
        return _read_linux()
    raise OSError(f"no CPU/memory source for {system}")
