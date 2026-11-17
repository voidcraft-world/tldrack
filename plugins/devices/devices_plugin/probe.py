"""What a device's raw counters mean: pure functions, no I/O, so every number the rack draws is tested.

── CPU IS A DELTA, NOT A LOAD AVERAGE ────────────────────────────────────────
"2.2 CPUs" is how many cores were busy over the last sample interval. Linux and
macOS both keep cumulative busy/idle tick counters; the change between two
reads, as a fraction of the change in all ticks, times the core count, is that
number. A load average is not it: it counts runnable AND waiting threads, so an
I/O-bound box reads "loaded" with idle cores. A remote macOS host is the one
exception: no shell tool prints its tick counters, so it reports the summed
`ps %cpu` instead, a decaying average over the last minute (`REMOTE_SCRIPT`).

── MEMORY IS WHAT ACTIVITY MONITOR AND `free` CALL USED ──────────────────────
macOS: app memory (anonymous pages less purgeable) + wired + compressed, the
"Memory Used" figure. Linux: MemTotal − MemAvailable, `free`'s "used" plus the
caches the kernel would not give back. Both leave out reclaimable file cache,
which is what makes "20% used" mean "80% is there for the taking".
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

END = "end"
"""The line that closes one block of `key=value` lines on the remote stream."""

VM_PREFIX = "vm:"
"""A remote macOS host forwards `vm_stat` verbatim with this prefix; `parse_vm_stat` reads it."""


@dataclass(frozen=True)
class Ticks:
    """Cumulative CPU ticks since boot: the busy ones and all of them."""

    busy: int
    total: int


@dataclass(frozen=True)
class Reading:
    """One sample of one device. None means "this source does not report it (yet)"."""

    os: str | None = None
    cores: int | None = None
    ticks: Ticks | None = None
    cpu_busy: float | None = None
    """Cores-equivalent, when the source reports it directly (a remote macOS host)."""
    mem_used: int | None = None
    mem_total: int | None = None


def linux_ticks(line: str) -> Ticks | None:
    """The aggregate `cpu` line of /proc/stat.

    Fields: user nice system idle iowait irq softirq steal guest guest_nice. guest and
    guest_nice are already inside user and nice, so only the first eight are summed.
    iowait is idle: a core waiting on a disk is free to run anything else.
    """
    words = line.split()
    if not words or words[0] != "cpu":
        return None
    try:
        fields = [int(word) for word in words[1:9]]
    except ValueError:
        return None
    if len(fields) < 4:
        return None
    total = sum(fields)
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return Ticks(busy=total - idle, total=total)


def busy_cores(previous: Ticks | None, current: Ticks | None, cores: int | None) -> float | None:
    """Cores busy between two tick reads. None until there are two, or across a counter reset."""
    if previous is None or current is None or not cores:
        return None
    elapsed = current.total - previous.total
    if elapsed <= 0:
        return None
    fraction = (current.busy - previous.busy) / elapsed
    return round(min(max(fraction, 0.0), 1.0) * cores, 2)


def linux_memory(total_kb: int | None, available_kb: int | None) -> tuple[int | None, int | None]:
    """(used, total) bytes from /proc/meminfo's MemTotal and MemAvailable (both in KiB)."""
    if total_kb is None:
        return None, None
    total = total_kb * 1024
    if available_kb is None:
        return None, total
    return max(total - available_kb * 1024, 0), total


def mac_used_bytes(internal: int, purgeable: int, wired: int, compressor: int, page_size: int) -> int:
    """Activity Monitor's "Memory Used": app memory + wired + compressed, from page counts.

    The one formula for this machine on macOS (`mac.py`, from host_statistics64) and a remote macOS host
    (`parse_vm_stat`, from `vm_stat`), which report the same kernel counters.
    """
    return max(internal - purgeable, 0) * page_size + wired * page_size + compressor * page_size


VM_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes")
VM_COUNT_RE = re.compile(r'^"?([^":]+)"?:\s+(\d+)\.?\s*$')
VM_FIELDS = {
    "Anonymous pages": "internal",
    "Pages purgeable": "purgeable",
    "Pages wired down": "wired",
    "Pages occupied by compressor": "compressor",
}


def parse_vm_stat(lines: Iterable[str]) -> int | None:
    """Memory used, in bytes, from `vm_stat` output; None if any counter is missing."""
    page_size: int | None = None
    counts: dict[str, int] = {}
    for line in lines:
        size = VM_PAGE_SIZE_RE.search(line)
        if size:
            page_size = int(size.group(1))
            continue
        match = VM_COUNT_RE.match(line.strip())
        if match and match.group(1) in VM_FIELDS:
            counts[VM_FIELDS[match.group(1)]] = int(match.group(2))
    if page_size is None or len(counts) != len(VM_FIELDS):
        return None
    return mac_used_bytes(page_size=page_size, **counts)


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def parse_block(fields: Mapping[str, str], vm_lines: Iterable[str]) -> Reading:
    """One remote block (header keys already merged in) as a Reading.

    Linux sends `cpu=` (the /proc/stat line) and `mem_total_kb=`/`mem_avail_kb=`; macOS
    sends `cpu_pct=` (summed `ps %cpu`), `mem_total=` and its `vm_stat` lines.
    """
    cores = _int(fields.get("cores"))
    cpu_pct = _float(fields.get("cpu_pct"))
    vm = list(vm_lines)
    if vm:
        mem_used, mem_total = parse_vm_stat(vm), _int(fields.get("mem_total"))
    else:
        mem_used, mem_total = linux_memory(_int(fields.get("mem_total_kb")), _int(fields.get("mem_avail_kb")))
    cpu_busy = None
    if cpu_pct is not None:
        cpu_busy = round(cpu_pct / 100, 2) if not cores else round(min(cpu_pct / 100, cores), 2)
    return Reading(
        os=fields.get("os") or None,
        cores=cores,
        ticks=linux_ticks(fields["cpu"]) if "cpu" in fields else None,
        cpu_busy=cpu_busy,
        mem_used=mem_used,
        mem_total=mem_total,
    )


class BlockReader:
    """Feeds the remote stream line by line; returns a Reading each time a block closes.

    The header block (`os`, `cores`, macOS's `mem_total`) is printed once, so its keys stay
    sticky across every later block. A partial block at EOF is dropped, never half-read.
    """

    def __init__(self) -> None:
        self._sticky: dict[str, str] = {}
        self._fields: dict[str, str] = {}
        self._vm: list[str] = []

    def feed(self, line: str) -> Reading | None:
        line = line.rstrip("\n")
        if line == END:
            fields = {**self._sticky, **self._fields}
            for key in ("os", "cores", "mem_total"):
                if key in self._fields:
                    self._sticky[key] = self._fields[key]
            vm, self._fields, self._vm = self._vm, {}, []
            return parse_block(fields, vm)
        if line.startswith(VM_PREFIX):
            self._vm.append(line[len(VM_PREFIX) :])
        elif "=" in line:
            key, value = line.split("=", 1)
            self._fields[key.strip()] = value.strip()
        return None


def remote_script(sample_seconds: float) -> str:
    """The loop a remote host runs for as long as the stream is open.

    Linux reads /proc with shell builtins only (`read`, `case`), so a sample forks
    nothing but `sleep`. When the bridge kills the stream, the next `echo` hits a
    closed pipe and the loop dies with it; nothing is left running on the host.
    """
    return f"""os=$(uname -s)
echo "os=$os $(uname -m)"
if [ "$os" = Linux ]; then
  echo "cores=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc)"
else
  echo "cores=$(sysctl -n hw.logicalcpu)"
  echo "mem_total=$(sysctl -n hw.memsize)"
fi
echo {END} || exit 0
while :; do
  if [ "$os" = Linux ]; then
    read -r cpu < /proc/stat
    echo "cpu=$cpu" || exit 0
    while read -r key value _; do
      case $key in
        MemTotal:) echo "mem_total_kb=$value" ;;
        MemAvailable:) echo "mem_avail_kb=$value"; break ;;
      esac
    done < /proc/meminfo
  else
    echo "cpu_pct=$(ps -A -o %cpu= | awk '{{s+=$1}} END {{printf "%.1f", s}}')" || exit 0
    vm_stat | sed 's/^/{VM_PREFIX}/'
  fi
  echo {END} || exit 0
  sleep {sample_seconds:g}
done
"""
