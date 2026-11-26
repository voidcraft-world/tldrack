"""The arithmetic behind every number the DEVICES section draws."""

from devices_plugin.probe import (
    END,
    GPU_PREFIX,
    MIB,
    VM_PREFIX,
    BlockReader,
    Gpu,
    Ticks,
    busy_cores,
    linux_memory,
    linux_swap,
    linux_swap_moved,
    linux_ticks,
    mac_used_bytes,
    parse_ioreg_gpu,
    parse_nvidia,
    parse_swapusage,
    parse_vm_stat,
    parse_vm_swap_moved,
    swap_rate,
)

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     3377.
Pages wired down:                             432684.
Pages purgeable:                                5688.
"Translation faults":                    58693726134.
Anonymous pages:                              781801.
Pages occupied by compressor:                 807290.
""".splitlines()


def test_linux_ticks_counts_iowait_as_idle_and_skips_guest():
    # user nice system idle iowait irq softirq steal guest guest_nice
    ticks = linux_ticks("cpu  100 10 50 800 40 0 0 0 999 999")
    assert ticks == Ticks(busy=160, total=1000)


def test_linux_ticks_refuses_a_per_core_or_garbled_line():
    assert linux_ticks("cpu0 1 2 3 4") is None
    assert linux_ticks("cpu a b c d") is None
    assert linux_ticks("") is None


def test_busy_cores_is_the_busy_share_of_the_delta_times_cores():
    assert busy_cores(Ticks(100, 1000), Ticks(400, 2000), 8) == 2.4


def test_busy_cores_needs_two_reads_and_a_forward_counter():
    assert busy_cores(None, Ticks(1, 2), 8) is None
    assert busy_cores(Ticks(5, 10), Ticks(5, 10), 8) is None
    assert busy_cores(Ticks(5, 10), Ticks(1, 3), 8) is None  # counters reset (reboot)
    assert busy_cores(Ticks(5, 10), Ticks(6, 20), None) is None


def test_busy_cores_clamps_to_the_core_count():
    assert busy_cores(Ticks(0, 0), Ticks(20, 10), 4) == 4.0


def test_linux_memory_is_total_less_available():
    assert linux_memory(16_000_000, 12_000_000) == (4_000_000 * 1024, 16_000_000 * 1024)
    assert linux_memory(None, 5) == (None, None)
    assert linux_memory(100, None) == (None, 100 * 1024)


def test_mac_used_is_app_plus_wired_plus_compressed():
    assert mac_used_bytes(internal=100, purgeable=10, wired=20, compressor=30, page_size=4) == (90 + 20 + 30) * 4
    assert mac_used_bytes(internal=5, purgeable=10, wired=0, compressor=0, page_size=4) == 0


def test_parse_vm_stat_feeds_the_same_formula():
    assert parse_vm_stat(VM_STAT) == mac_used_bytes(781801, 5688, 432684, 807290, 16384)


def test_parse_vm_stat_needs_every_counter():
    assert parse_vm_stat(VM_STAT[:-1]) is None
    assert parse_vm_stat(VM_STAT[1:]) is None


def feed(reader: BlockReader, text: str):
    return [reading for line in text.splitlines() if (reading := reader.feed(line + "\n"))]


def test_linux_stream_keeps_the_header_sticky():
    reader = BlockReader()
    readings = feed(
        reader,
        f"""os=Linux x86_64
cores=8
{END}
cpu=cpu  100 0 100 800 0 0 0 0 0 0
mem_total_kb=16000000
mem_avail_kb=12000000
{END}
cpu=cpu  200 0 200 1600 0 0 0 0 0 0
mem_total_kb=16000000
mem_avail_kb=11000000
{END}""",
    )
    assert len(readings) == 3
    header, first, second = readings
    assert header.os == "Linux x86_64" and header.mem_used is None
    assert first.cores == 8 and first.os == "Linux x86_64"
    assert first.ticks == Ticks(busy=200, total=1000)
    assert busy_cores(first.ticks, second.ticks, second.cores) == 1.6
    assert second.mem_used == 5_000_000 * 1024


def test_mac_stream_reports_cpu_directly_and_reads_vm_stat():
    reader = BlockReader()
    vm = "\n".join(VM_PREFIX + line for line in VM_STAT)
    readings = feed(reader, f"os=Darwin arm64\ncores=4\nmem_total=17179869184\n{END}\ncpu_pct=250.0\n{vm}\n{END}")
    reading = readings[-1]
    assert reading.cpu_busy == 2.5 and reading.ticks is None
    assert reading.mem_total == 17179869184
    assert reading.mem_used == parse_vm_stat(VM_STAT)


def test_mac_stream_clamps_ps_sum_to_cores():
    readings = feed(BlockReader(), f"cores=2\n{END}\ncpu_pct=380\n{END}")
    assert readings[-1].cpu_busy == 2.0


def test_swap_rate_is_bytes_a_second_between_two_reads():
    assert swap_rate(1_000, 5_000, 2.0) == 2_000
    assert swap_rate(None, 5_000, 2.0) is None  # the first read
    assert swap_rate(5_000, 1_000, 2.0) is None  # a counter that went back: a reboot
    assert swap_rate(1_000, 5_000, 0) is None
    assert swap_rate(1_000, 5_000, None) is None


def test_linux_swap_is_total_less_free_and_its_counters_are_pages():
    assert linux_swap(2048, 512) == (1536 * 1024, 2048 * 1024)
    assert linux_swap(0, 0) == (0, 0)  # no swap configured
    assert linux_swap(None, 512) == (None, None)
    assert linux_swap(2048, None) == (None, 2048 * 1024)
    assert linux_swap_moved(10, 30, 4096) == 40 * 4096
    assert linux_swap_moved(10, None, 4096) is None
    assert linux_swap_moved(10, 30, None) is None


def test_parse_swapusage_reads_the_sysctl_line():
    assert parse_swapusage("total = 2048.00M  used = 1024.50M  free = 1023.50M  (encrypted)") == (
        round(1024.5 * MIB),
        2048 * MIB,
    )
    assert parse_swapusage("") == (None, None)
    assert parse_swapusage(None) == (None, None)


def test_vm_stat_carries_the_swap_counters_too():
    lines = [*VM_STAT, "Swapins:                                     100.", "Swapouts:                                    300."]
    assert parse_vm_swap_moved(lines) == 400 * 16384
    assert parse_vm_stat(lines) == parse_vm_stat(VM_STAT)  # the memory formula ignores them
    assert parse_vm_swap_moved(VM_STAT) is None


def test_several_nvidia_gpus_read_as_the_busiest_and_their_memory_summed():
    assert parse_nvidia("38, 1024, 24576;91, 2048, 24576;") == Gpu(pct=91.0, mem_used=3072 * MIB, mem_total=49152 * MIB)
    assert parse_nvidia("[N/A], [N/A], [N/A];") is None
    assert parse_nvidia("") is None
    assert parse_nvidia(None) is None


def test_ioreg_gpu_lines_read_as_utilisation_and_memory_in_use():
    assert parse_ioreg_gpu(['"Device Utilization %"=38', '"In use system memory"=9964273664']) == Gpu(
        pct=38.0, mem_used=9964273664
    )
    assert parse_ioreg_gpu(['"Device Utilization %"=12', '"Device Utilization %"=70']) == Gpu(pct=70.0)
    assert parse_ioreg_gpu(['"In use system memory"=9964273664']) is None  # memory alone is not a GPU readout
    assert parse_ioreg_gpu([]) is None


def test_linux_stream_carries_swap_and_an_nvidia_gpu():
    reader = BlockReader()
    for line in ("os=Linux x86_64", "cores=8", "page_size=4096", END):
        reader.feed(line)
    block = [
        "cpu=cpu  100 0 0 900 0 0 0 0",
        "mem_total_kb=1000",
        "mem_avail_kb=400",
        "swap_total_kb=2048",
        "swap_free_kb=1024",
        "swap_in=10",
        "swap_out=30",
        "gpu=38, 1024, 24576;",
        END,
    ]
    reading = [reader.feed(line) for line in block][-1]
    assert (reading.swap_used, reading.swap_total, reading.swap_moved) == (1024 * 1024, 2048 * 1024, 40 * 4096)
    assert reading.gpu == Gpu(pct=38.0, mem_used=1024 * MIB, mem_total=24576 * MIB)


def test_a_linux_host_without_swap_counters_or_a_gpu_reports_neither():
    reader = BlockReader()
    for line in ("os=Linux x86_64", "cores=8", END):
        reader.feed(line)
    reading = [reader.feed(line) for line in ("cpu=cpu  100 0 0 900 0 0 0 0", "mem_total_kb=1000", "mem_avail_kb=400", END)][-1]
    assert (reading.swap_used, reading.swap_total, reading.swap_moved, reading.gpu) == (None, None, None, None)
    assert reading.mem_used == 600 * 1024


def test_mac_stream_carries_swap_and_the_ioreg_gpu():
    reader = BlockReader()
    for line in ("os=Darwin arm64", "cores=10", "mem_total=34359738368", END):
        reader.feed(line)
    block = [
        "cpu_pct=220.0",
        "swap=total = 2048.00M  used = 1024.00M  free = 1024.00M  (encrypted)",
        *(VM_PREFIX + line for line in [*VM_STAT, "Swapins:     100.", "Swapouts:     300."]),
        GPU_PREFIX + '"Device Utilization %"=38',
        GPU_PREFIX + '"In use system memory"=9964273664',
        END,
    ]
    reading = [reader.feed(line) for line in block][-1]
    assert (reading.swap_used, reading.swap_total, reading.swap_moved) == (1024 * MIB, 2048 * MIB, 400 * 16384)
    assert reading.gpu == Gpu(pct=38.0, mem_used=9964273664)
    # The next block's GPU lines are its own: none sent means none read.
    assert [reader.feed(line) for line in ("cpu_pct=1.0", *(VM_PREFIX + line for line in VM_STAT), END)][-1].gpu is None


def test_a_partial_block_at_eof_is_never_read():
    assert feed(BlockReader(), "os=Linux x86_64\ncores=8\ncpu=cpu 1 2 3 4") == []
