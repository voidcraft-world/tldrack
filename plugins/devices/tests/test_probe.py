"""The arithmetic behind every number the DEVICES section draws."""

from devices_plugin.probe import (
    END,
    VM_PREFIX,
    BlockReader,
    Ticks,
    busy_cores,
    linux_memory,
    linux_ticks,
    mac_used_bytes,
    parse_vm_stat,
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


def test_a_partial_block_at_eof_is_never_read():
    assert feed(BlockReader(), "os=Linux x86_64\ncores=8\ncpu=cpu 1 2 3 4") == []
