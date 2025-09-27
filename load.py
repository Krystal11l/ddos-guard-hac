#!/usr/bin/env python3
"""
Адаптивный стресс-тестер: cpu, ram, disk, network.
Поддерживает цель по проценту загрузки (default 60%).
Запускайте осторожно — не на боевых машинах.
"""

import argparse
import threading
import multiprocessing as mp
import time
import os
import tempfile
import psutil
import socket
import random
from collections import deque

# ---------- Параметры и утилиты ----------
def human_bytes(n):
    for unit in ['B','KB','MB','GB','TB']:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"

# ---------- CPU worker ----------
def cpu_worker(control_queue, idx):
    """
    Цикл, выполняющий вычисления с регулируемой duty-cycle.
    control_queue: ожидает float duty [0.0..1.0]
    """
    duty = 0.5
    rnd = 0
    while True:
        try:
            duty = control_queue.get_nowait()
        except Exception:
            pass
        # active period ~ duty*period
        period = 0.5  # 500ms control window per worker
        active = max(0.001, duty * period)
        idle = max(0.0, period - active)
        t0 = time.time()
        # активная работа: бессмысленные вычисления
        while time.time() - t0 < active:
            rnd = (rnd * 1664525 + 1013904223) & 0xFFFFFFFF
            _ = rnd ^ (rnd << 13)
        if idle > 0:
            time.sleep(idle)

# ---------- RAM worker ----------
class RamAllocator:
    def __init__(self, target_mb=0):
        self.buffers = []
        self.lock = threading.Lock()
        self.target_mb = target_mb

    def set_target(self, mb):
        with self.lock:
            self.target_mb = mb

    def adjust(self):
        with self.lock:
            current_mb = sum(len(b) for b in self.buffers) / (1024**2)
            target = self.target_mb
            if current_mb < target:
                # allocate in chunks of 1MB
                to_alloc = int(max(1, min( (target - current_mb), 50 )))  # up to 50MB at once
                for _ in range(to_alloc):
                    self.buffers.append(bytearray(1024*1024))
            elif current_mb > target:
                # free some
                to_free = int(min(len(self.buffers), max(1, int(current_mb - target))))
                for _ in range(to_free):
                    self.buffers.pop()

# ---------- Disk worker ----------
class DiskWorker(threading.Thread):
    def __init__(self, path_dir, control):
        super().__init__(daemon=True)
        self.path_dir = path_dir
        self.control = control  # dict with 'mb_per_sec' target
        self._stop = threading.Event()
        self.file = None

    def run(self):
        fname = os.path.join(self.path_dir, f"stress_disk_{os.getpid()}.tmp")
        # Prepare file
        f = open(fname, "wb")
        self.file = f
        chunk = os.urandom(1024*64)  # 64KB chunk
        try:
            while not self._stop.is_set():
                mbps = self.control.get('mb_per_sec', 1.0)
                if mbps <= 0:
                    time.sleep(0.5)
                    continue
                # write loop for 1 second targeting mbps
                bytes_to_write = int(mbps * 1024*1024)
                written = 0
                tstart = time.time()
                while written < bytes_to_write and not self._stop.is_set():
                    f.write(chunk)
                    written += len(chunk)
                    # occasional flush
                    if written % (1024*1024) < len(chunk):
                        f.flush()
                        os.fsync(f.fileno())
                    # small sleep to avoid hogging if target small
                    if mbps < 1:
                        time.sleep(0.01)
                # sleep to keep per-second pacing
                dt = time.time() - tstart
                if dt < 1.0:
                    time.sleep(1.0 - dt)
        finally:
            try:
                f.close()
                os.remove(fname)
            except Exception:
                pass

    def stop(self):
        self._stop.set()

# ---------- Network worker ----------
class NetWorker(threading.Thread):
    def __init__(self, control, target_addr=("127.0.0.1", 9)):
        super().__init__(daemon=True)
        self.control = control  # dict with 'mb_per_sec'
        self._stop = threading.Event()
        self.target_addr = target_addr
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)

    def run(self):
        chunk = b"x" * 1400  # ~ typical UDP payload
        try:
            while not self._stop.is_set():
                mbps = self.control.get('mb_per_sec', 0.5)
                if mbps <= 0:
                    time.sleep(0.3)
                    continue
                bytes_to_send = int(mbps * 1024*1024)
                sent = 0
                tstart = time.time()
                while sent < bytes_to_send and not self._stop.is_set():
                    try:
                        self.sock.sendto(chunk, self.target_addr)
                        sent += len(chunk)
                    except BlockingIOError:
                        time.sleep(0.001)
                dt = time.time() - tstart
                if dt < 1.0:
                    time.sleep(1.0 - dt)
        finally:
            try:
                self.sock.close()
            except Exception:
                pass

    def stop(self):
        self._stop.set()

# ---------- Controller ----------
def controller_loop(args, cpu_queues, ram_alloc, disk_control, net_control):
    proc = psutil.Process(os.getpid())
    sample_interval = 1.0
    # history to smooth
    hist_len = 5
    cpu_hist = deque(maxlen=hist_len)
    mem_hist = deque(maxlen=hist_len)
    net_hist = deque(maxlen=hist_len)
    disk_hist = deque(maxlen=hist_len)

    # baseline counters for net/disk bytes per sec
    last_net = psutil.net_io_counters()
    last_disk = psutil.disk_io_counters()
    start_time = time.time()
    end_time = start_time + args.duration if args.duration>0 else float('inf')

    # initial targets
    target = args.target_percent
    # convert bandwidth/disk to bytes/sec maxima
    max_net_bps = max(1, args.max_bandwidth_mbps) * 1024 * 1024 / 8.0  # convert Mbps -> Bytes/sec
    max_disk_bps = max(1, args.max_disk_mbps) * 1024 * 1024  # MB/s -> Bytes/sec

    # duty per CPU worker (0..1)
    cpu_duty = 0.2
    # ram target MB
    total_mem = psutil.virtual_memory().total / (1024**2)
    ram_target_mb = total_mem * (target/100.0) * 0.6  # start conservative (60% of target percent)
    ram_alloc.set_target(ram_target_mb)

    # disk/net target MB/s (absolute) initial guesses as portion of max
    disk_control['mb_per_sec'] = args.max_disk_mbps * (target/100.0) * 0.6
    net_control['mb_per_sec'] = args.max_bandwidth_mbps * (target/100.0) * 0.6

    print(f"Запуск контроллера: цель {target}%. Общая память {total_mem:.1f} MB.")
    print(f"Начальные целевые: RAM {ram_target_mb:.1f} MB, NET ~{net_control['mb_per_sec']:.2f} MB/s, DISK ~{disk_control['mb_per_sec']:.2f} MB/s")
    try:
        while time.time() < end_time:
            t0 = time.time()
            # read system metrics
            cpu = psutil.cpu_percent(interval=None)  # instantaneous since last call; we'll sleep later
            mem = psutil.virtual_memory().percent
            # net/disk bytes/s
            cur_net = psutil.net_io_counters()
            cur_disk = psutil.disk_io_counters()
            net_bytes = (cur_net.bytes_sent + cur_net.bytes_recv) - (last_net.bytes_sent + last_net.bytes_recv)
            disk_bytes = (cur_disk.read_bytes + cur_disk.write_bytes) - (last_disk.read_bytes + last_disk.write_bytes)
            last_net = cur_net
            last_disk = cur_disk
            # convert to percents relative to maxima
            net_util_pct = min(100.0, (net_bytes / max(1.0, max_net_bps)) * 100.0)
            disk_util_pct = min(100.0, (disk_bytes / max(1.0, max_disk_bps)) * 100.0)

            cpu_hist.append(cpu)
            mem_hist.append(mem)
            net_hist.append(net_util_pct)
            disk_hist.append(disk_util_pct)

            avg_cpu = sum(cpu_hist)/len(cpu_hist)
            avg_mem = sum(mem_hist)/len(mem_hist)
            avg_net = sum(net_hist)/len(net_hist)
            avg_disk = sum(disk_hist)/len(disk_hist)

            # Simple proportional adjustments to bring each metric toward target
            # CPU: adjust duty across workers
            # If avg_cpu < target*0.98 => increase cpu_duty; if > target => reduce
            if avg_cpu < target * 0.98:
                cpu_duty = min(0.99, cpu_duty + 0.05)
            elif avg_cpu > target * 1.02:
                cpu_duty = max(0.0, cpu_duty - 0.07)
            # send duty to all cpu workers
            for q in cpu_queues:
                try:
                    q.put_nowait(cpu_duty)
                except Exception:
                    pass

            # RAM: try to make allocated memory equal to target percent of total memory
            # translate desired percent into MB (we try to have RAM usage move toward target)
            desired_ram_percent = target  # aim RAM percent to the same % as target
            desired_ram_mb = total_mem * (desired_ram_percent/100.0)
            # but don't exceed 80% of system
            desired_ram_mb = min(desired_ram_mb, total_mem*0.8)
            # set as a fraction of desired (conservative)
            ram_alloc.set_target(desired_ram_mb)

            # Network: adjust net_control['mb_per_sec'] based on avg_net vs target
            if avg_net < target * 0.98:
                net_control['mb_per_sec'] = min(args.max_bandwidth_mbps, net_control['mb_per_sec'] * 1.2 + 0.1)
            elif avg_net > target * 1.02:
                net_control['mb_per_sec'] = max(0.0, net_control['mb_per_sec'] * 0.6)

            # Disk: same
            if avg_disk < target * 0.98:
                disk_control['mb_per_sec'] = min(args.max_disk_mbps, disk_control['mb_per_sec'] * 1.2 + 0.1)
            elif avg_disk > target * 1.02:
                disk_control['mb_per_sec'] = max(0.0, disk_control['mb_per_sec'] * 0.6)

            # perform local adjustments
            ram_alloc.adjust()

            # Print summary line
            uptime = time.time() - start_time
            print(f"[{uptime:5.0f}s] CPU {avg_cpu:5.1f}% duty {cpu_duty:.2f} | RAM {avg_mem:5.1f}% -> target {ram_alloc.target_mb:.0f}MB | NET {avg_net:5.1f}% ({net_control['mb_per_sec']:.2f}MB/s) | DISK {avg_disk:5.1f}% ({disk_control['mb_per_sec']:.2f}MB/s)")
            # sleep for sample interval
            dt = time.time() - t0
            if dt < sample_interval:
                time.sleep(sample_interval - dt)
    except KeyboardInterrupt:
        print("Interrupted by user.")
    finally:
        print("Контроллер завершает работу.")

# ---------- Main ----------
def main():
    parser = argparse.ArgumentParser(description="Adaptive multi-resource stress (keeps each resource <= target_percent approx.)")
    parser.add_argument("--duration", type=int, default=60, help="Seconds to run (0 = indefinite)")
    parser.add_argument("--target-percent", type=float, default=60.0, help="Target percent for each resource (default 60)")
    parser.add_argument("--cpu-workers", type=int, default=max(1, mp.cpu_count()), help="Number of CPU worker processes")
    parser.add_argument("--max-bandwidth-mbps", type=float, default=100.0, help="Estimated max network bandwidth in Mbps (used to compute utilization).")
    parser.add_argument("--max-disk-mbps", type=float, default=200.0, help="Estimated max disk throughput in MB/s for utilization calcs.")
    parser.add_argument("--tmp-dir", type=str, default=None, help="Directory to write disk files (defaults to system temp)")
    args = parser.parse_args()

    # Validate
    if not (1 <= args.target_percent <= 95):
        print("target-percent должен быть в диапазоне [1..95]")
        return

    # Prepare CPU workers (multiprocessing)
    cpu_queues = []
    cpu_procs = []
    for i in range(args.cpu_workers):
        q = mp.Queue(maxsize=1)
        p = mp.Process(target=cpu_worker, args=(q,i), daemon=True)
        p.start()
        cpu_queues.append(q)
        cpu_procs.append(p)

    # RAM allocator (threaded)
    ram_alloc = RamAllocator()

    # Disk worker (thread)
    tmpdir = args.tmp_dir or tempfile.gettempdir()
    disk_control = {'mb_per_sec': 0.5}
    disk_worker = DiskWorker(tmpdir, disk_control)
    disk_worker.start()

    # Network worker (thread)
    net_control = {'mb_per_sec': 0.5}
    net_worker = NetWorker(net_control, target_addr=("127.0.0.1", 9))
    net_worker.start()

    # Controller loop (main thread)
    try:
        controller_loop(args, cpu_queues, ram_alloc, disk_control, net_control)
    finally:
        # stop threads/processes
        print("Останавливаю сетевой/дисковый воркеры...")
        try:
            net_worker.stop()
            disk_worker.stop()
        except Exception:
            pass
        for p in cpu_procs:
            try:
                p.terminate()
            except Exception:
                pass
        print("Готово.")

if __name__ == "__main__":
    main()
