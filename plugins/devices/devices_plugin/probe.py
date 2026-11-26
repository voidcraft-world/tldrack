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

── SWAP IS A LEVEL AND A RATE ────────────────────────────────────────────────
`swap_used` of `swap_total` is how much has been pushed out; on macOS the total is what the
kernel has allocated so far and grows on demand, so a full bar there is not a limit. The number
that says "this box is starved NOW" is the rate: bytes moved in and out of swap per second,
a delta of the kernel's cumulative swap-in and swap-out page counters (`swap_rate`). A machine
can sit on gigabytes of old swap and be fine; one moving hundreds of MB a second is thrashing.

── GPU IS THE BUSIEST DEVICE ─────────────────────────────────────────────────
`gpu_pct` is the device's own utilisation figure: IOKit's "Device Utilization %" on macOS
(`mac_gpu.py`; `ioreg` prints the same on a remote host), `nvidia-smi`'s `utilization.gpu` on a
Linux host. With several GPUs it is the busiest one, and memory is their sum. Apple silicon
shares one memory with the CPU, so there is a `gpu_mem_used` and no total. This machine on
Linux reports no GPU: nothing reads one without a subprocess per sample.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

END = "end"
"""The line that closes one block of `key=value` lines on the remote stream."""

VM_PREFIX = "vm:"
"""A remote macOS host forwards `vm_stat` verbatim with this prefix; `parse_vm_stat` reads it."""

GPU_PREFIX = "gpu:"
"""A remote macOS host forwards `ioreg`'s GPU counters with this prefix; `parse_ioreg_gpu` reads them."""

MIB = 1024**2


@dataclass(frozen=True)
class Ticks:
    """Cumulative CPU ticks since boot: the busy ones and all of them."""

    busy: int
    total: int


@dataclass(frozen=True)
class Gpu:
    """The busiest GPU's utilisation and every GPU's memory, summed (module header)."""

    pct: float | None = None
    mem_used: int | None = None
    mem_total: int | None = None


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
    swap_used: int | None = None
    swap_total: int | None = None
    swap_moved: int | None = None
    """Cumulative bytes swapped in plus out since boot; `swap_rate` turns two of them into a rate."""
    gpu: Gpu | None = None


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


def swap_rate(previous: int | None, current: int | None, seconds: float | None) -> int | None:
    """Bytes a second moved in and out of swap between two reads. None until there are two, or across a reset."""
    if previous is None or current is None or not seconds or seconds <= 0 or current < previous:
        return None
    return round((current - previous) / seconds)


def linux_swap(total_kb: int | None, free_kb: int | None) -> tuple[int | None, int | None]:
    """(used, total) bytes from /proc/meminfo's SwapTotal and SwapFree (both in KiB)."""
    if total_kb is None:
        return None, None
    total = total_kb * 1024
    if free_kb is None:
        return None, total
    return max(total - free_kb * 1024, 0), total


def linux_swap_moved(pages_in: int | None, pages_out: int | None, page_size: int | None) -> int | None:
    """/proc/vmstat's `pswpin` and `pswpout` (pages since boot) as cumulative bytes."""
    if pages_in is None or pages_out is None or not page_size:
        return None
    return (pages_in + pages_out) * page_size


SWAPUSAGE_RE = re.compile(r"(total|used) = (\d+(?:\.\d+)?)M")


def parse_swapusage(text: str | None) -> tuple[int | None, int | None]:
    """(used, total) bytes from `sysctl -n vm.swapusage`: `total = 2048.00M  used = 1024.50M  free = …`."""
    found = {key: round(float(value) * MIB) for key, value in SWAPUSAGE_RE.findall(text or "")}
    return found.get("used"), found.get("total")


NVIDIA_ROW_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*$")


def parse_nvidia(text: str | None) -> Gpu | None:
    """`nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits`, its rows
    joined by `;`: percent, MiB, MiB a GPU. None when no row reads (no GPU, or a driver that answers `[N/A]`)."""
    rows = [match.groups() for match in map(NVIDIA_ROW_RE.match, (text or "").split(";")) if match]
    if not rows:
        return None
    return Gpu(
        pct=max(float(pct) for pct, _used, _total in rows),
        mem_used=sum(round(float(used) * MIB) for _pct, used, _total in rows),
        mem_total=sum(round(float(total) * MIB) for _pct, _used, total in rows),
    )


IOREG_GPU_RE = re.compile(r'^"(Device Utilization %|In use system memory)"=(\d+)$')


def parse_ioreg_gpu(lines: Iterable[str]) -> Gpu | None:
    """`ioreg`'s IOAccelerator counters, one `"name"=number` a line (`remote_script`). None without a utilisation."""
    pcts: list[int] = []
    used: list[int] = []
    for line in lines:
        match = IOREG_GPU_RE.match(line.strip())
        if match:
            (pcts if match.group(1) == "Device Utilization %" else used).append(int(match.group(2)))
    if not pcts:
        return None
    return Gpu(pct=float(min(max(pcts), 100)), mem_used=sum(used) if used else None)


VM_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes")
VM_COUNT_RE = re.compile(r'^"?([^":]+)"?:\s+(\d+)\.?\s*$')
VM_FIELDS = {
    "Anonymous pages": "internal",
    "Pages purgeable": "purgeable",
    "Pages wired down": "wired",
    "Pages occupied by compressor": "compressor",
}
VM_SWAP_FIELDS = ("Swapins", "Swapouts")


def _vm_counts(lines: Iterable[str]) -> tuple[int | None, dict[str, int]]:
    """`vm_stat`'s page size and every counter it prints, by the name it prints."""
    page_size: int | None = None
    counts: dict[str, int] = {}
    for line in lines:
        size = VM_PAGE_SIZE_RE.search(line)
        if size:
            page_size = int(size.group(1))
            continue
        match = VM_COUNT_RE.match(line.strip())
        if match:
            counts[match.group(1)] = int(match.group(2))
    return page_size, counts


def parse_vm_stat(lines: Iterable[str]) -> int | None:
    """Memory used, in bytes, from `vm_stat` output; None if any counter is missing."""
    page_size, counts = _vm_counts(lines)
    if page_size is None or any(name not in counts for name in VM_FIELDS):
        return None
    return mac_used_bytes(page_size=page_size, **{field: counts[name] for name, field in VM_FIELDS.items()})


def parse_vm_swap_moved(lines: Iterable[str]) -> int | None:
    """Cumulative bytes swapped in plus out, from `vm_stat`'s Swapins and Swapouts; None if either is missing."""
    page_size, counts = _vm_counts(lines)
    if page_size is None or any(name not in counts for name in VM_SWAP_FIELDS):
        return None
    return sum(counts[name] for name in VM_SWAP_FIELDS) * page_size


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


def parse_block(fields: Mapping[str, str], vm_lines: Iterable[str], gpu_lines: Iterable[str] = ()) -> Reading:
    """One remote block (header keys already merged in) as a Reading.

    Linux sends `cpu=` (the /proc/stat line), `mem_total_kb=`/`mem_avail_kb=`, `swap_total_kb=`/`swap_free_kb=`,
    `swap_in=`/`swap_out=` (pages) and, with an NVIDIA driver, `gpu=`; macOS sends `cpu_pct=` (summed
    `ps %cpu`), `mem_total=`, `swap=`, its `vm_stat` lines and its `ioreg` GPU lines.
    """
    cores = _int(fields.get("cores"))
    cpu_pct = _float(fields.get("cpu_pct"))
    vm = list(vm_lines)
    if vm:
        mem_used, mem_total = parse_vm_stat(vm), _int(fields.get("mem_total"))
        swap_used, swap_total = parse_swapusage(fields.get("swap"))
        swap_moved = parse_vm_swap_moved(vm)
        gpu = parse_ioreg_gpu(gpu_lines)
    else:
        mem_used, mem_total = linux_memory(_int(fields.get("mem_total_kb")), _int(fields.get("mem_avail_kb")))
        swap_used, swap_total = linux_swap(_int(fields.get("swap_total_kb")), _int(fields.get("swap_free_kb")))
        swap_moved = linux_swap_moved(_int(fields.get("swap_in")), _int(fields.get("swap_out")), _int(fields.get("page_size")))
        gpu = parse_nvidia(fields.get("gpu"))
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
        swap_used=swap_used,
        swap_total=swap_total,
        swap_moved=swap_moved,
        gpu=gpu,
    )


STICKY_KEYS = ("os", "cores", "mem_total", "page_size")


class BlockReader:
    """Feeds the remote stream line by line; returns a Reading each time a block closes.

    The header block (`STICKY_KEYS`: what a host prints once) stays sticky across every later
    block. A partial block at EOF is dropped, never half-read.
    """

    def __init__(self) -> None:
        self._sticky: dict[str, str] = {}
        self._fields: dict[str, str] = {}
        self._vm: list[str] = []
        self._gpu: list[str] = []

    def feed(self, line: str) -> Reading | None:
        line = line.rstrip("\n")
        if line == END:
            fields = {**self._sticky, **self._fields}
            for key in STICKY_KEYS:
                if key in self._fields:
                    self._sticky[key] = self._fields[key]
            vm, gpu, self._fields, self._vm, self._gpu = self._vm, self._gpu, {}, [], []
            return parse_block(fields, vm, gpu)
        if line.startswith(VM_PREFIX):
            self._vm.append(line[len(VM_PREFIX) :])
        elif line.startswith(GPU_PREFIX):
            self._gpu.append(line[len(GPU_PREFIX) :])
        elif "=" in line:
            key, value = line.split("=", 1)
            self._fields[key.strip()] = value.strip()
        return None


def remote_script(sample_seconds: float) -> str:
    """The loop a remote host runs for as long as the stream is open.

    Linux reads /proc with shell builtins only (`read`, `case`), so a sample forks
    nothing but `sleep`, and `nvidia-smi` where the host has one (looked for once). When the
    bridge kills the stream, the next `echo` hits a closed pipe and the loop dies with it;
    nothing is left running on the host.
    """
    return f"""os=$(uname -s)
echo "os=$os $(uname -m)"
if [ "$os" = Linux ]; then
  echo "cores=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc)"
  echo "page_size=$(getconf PAGESIZE 2>/dev/null)"
  command -v nvidia-smi >/dev/null 2>&1 && nvidia=1
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
        MemAvailable:) echo "mem_avail_kb=$value" ;;
        SwapTotal:) echo "swap_total_kb=$value" ;;
        SwapFree:) echo "swap_free_kb=$value"; break ;;
      esac
    done < /proc/meminfo
    if [ -r /proc/vmstat ]; then
      while read -r key value; do
        case $key in
          pswpin) echo "swap_in=$value" ;;
          pswpout) echo "swap_out=$value"; break ;;
        esac
      done < /proc/vmstat
    fi
    if [ -n "$nvidia" ]; then
      echo "gpu=$(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | tr '\\n' ';')"
    fi
  else
    echo "cpu_pct=$(ps -A -o %cpu= | awk '{{s+=$1}} END {{printf "%.1f", s}}')" || exit 0
    echo "swap=$(sysctl -n vm.swapusage 2>/dev/null)"
    vm_stat | sed 's/^/{VM_PREFIX}/'
    ioreg -r -d 1 -w 0 -c IOAccelerator 2>/dev/null | grep -oE '"(Device Utilization %|In use system memory)"=[0-9]+' | sed 's/^/{GPU_PREFIX}/'
  fi
  echo {END} || exit 0
  sleep {sample_seconds:g}
done
"""
