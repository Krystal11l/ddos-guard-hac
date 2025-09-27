#!/usr/bin/env python3
"""
Фоновый анализатор системы:
- CPU, RAM, Disk, Temp-папка
- Температура CPU
- Время отклика диска
- Нагрузка сети
- Температура SSD-контроллера
- Popup и советы при превышении порогов
"""

import psutil
import time
import threading
import os
import tempfile
import tkinter as tk
from tkinter import messagebox


class RealTimeAnalyzer:
    def __init__(self, interval=5):
        self.interval = interval
        self.running = False

    def get_most_idle_background_process(self):
        """Находит процесс с наибольшим временем бездействия"""
        max_idle = 0
        candidate = None
        for p in psutil.process_iter(['pid', 'name', 'status', 'create_time', 'memory_percent']):
            try:
                if p.info['status'] == psutil.STATUS_SLEEPING:
                    idle_time = time.time() - p.info['create_time']
                    if idle_time > max_idle:
                        max_idle = idle_time
                        candidate = p
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return candidate, max_idle

    def show_popup(self, title, message):
        """Выводит popup с сообщением"""
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(title, message)
        root.destroy()

    def analyze(self):
        self.running = True
        print("Фоновый анализатор запущен. Наблюдение за системой...")

        last_net = psutil.net_io_counters()

        while self.running:
            cpu = psutil.cpu_percent(interval=1)
            ram = psutil.virtual_memory().percent
            disk = psutil.disk_usage('/').percent
            temp_dir = tempfile.gettempdir()
            temp_size = sum(os.path.getsize(os.path.join(root, f))
                            for root, dirs, files in os.walk(temp_dir)
                            for f in files if os.path.exists(os.path.join(root, f))) / 1024 / 1024  # MB

            # --- Температура CPU ---
            cpu_temp = None
            try:
                temps = psutil.sensors_temperatures()
                if "coretemp" in temps:
                    cpu_temp = max([t.current for t in temps["coretemp"]])
            except Exception:
                pass

            # --- Температура SSD ---
            ssd_temp = None
            try:
                if "nvme" in temps:
                    ssd_temp = max([t.current for t in temps["nvme"]])
            except Exception:
                pass

            # --- Отклик диска ---
            disk_io = psutil.disk_io_counters()
            disk_latency = None
            if disk_io.read_count + disk_io.write_count > 0:
                disk_latency = (disk_io.read_time + disk_io.write_time) / \
                               (disk_io.read_count + disk_io.write_count)

            # --- Сеть ---
            net_now = psutil.net_io_counters()
            net_sent = (net_now.bytes_sent - last_net.bytes_sent) / 1024  # KB/s
            net_recv = (net_now.bytes_recv - last_net.bytes_recv) / 1024
            last_net = net_now

            # ---------------- Советы ----------------

            if cpu >= 80:
                msg = f"CPU загружен на {cpu:.1f}%. Закройте тяжёлые приложения."
                print(msg)
                self.show_popup("Высокая загрузка CPU", msg)

            if ram >= 75:
                msg = f"RAM используется на {ram:.1f}%. Освободите память."
                print(msg)
                self.show_popup("Высокая загрузка RAM", f"'{msg}' \nСОВЕТ: закройте ненужные приложения или очистите кэш, чтобы снизить нагрузку.")

            if disk >= 90:
                msg = f"Диск заполнен на {disk:.1f}%. Освободите место."
                print(msg)
                self.show_popup("Переполненный диск", f"'{msg}' \nСОВЕТ: закройте тяжелые программы или вкладки браузера.")

            if temp_size > 25000:
                msg = f"Временные файлы занимают {temp_size:.1f} MB. Очистите Temp."
                print(msg)
                self.show_popup("Переполненный Temp", f" '{msg}' \nСОВЕТ: освободите место, удалив ненужные файлы или очистив временные папки.")

            if cpu_temp and cpu_temp >= 90:
                msg = f"Температура CPU {cpu_temp}°C. Совет: снизьте нагрузку или улучшите охлаждение."
                print(msg)
                self.show_popup("Перегрев CPU", msg)

            if ssd_temp and ssd_temp >= 90:
                msg = f"Контроллер SSD разогрелся до {ssd_temp}°C. Совет: снизьте нагрузку."
                print(msg)
                self.show_popup("Перегрев SSD", msg)

            if disk_latency and disk_latency >= 500:
                msg = f"Время отклика диска {disk_latency:.0f} мс. \nСовет: возможно, диск требует замены."
                print(msg)
                self.show_popup("Медленный диск", msg)

            if net_sent > 950000 or net_recv > 950000:  # больше 500 KB/s
                msg = f"Сеть сильно нагружена (отправка {net_sent:.1f} KB/s, приём {net_recv:.1f} KB/s)."
                print(msg)
                self.show_popup("Высокая нагрузка сети", msg)

            # --- Idle процессы ---
            bg_proc, idle_time = self.get_most_idle_background_process()
            if bg_proc and idle_time > 300 and (cpu >= 80 or ram >= 75) and (proc_mem>=15 or proc_cpu>=10):
                msg = f"Процесс '{bg_proc.info['name']}' бездействует {int(idle_time)} сек.\nСовет: завершите его."
                print(msg)
                self.show_popup("Бездействующий процесс", f"Фоновое приложение, '{bg_proc.info['name']}'\nСОВЕТ: фоновое приложение '{bg_proc}' бездействует больше 5 минут, но использует много ресурсов. Закройте его, по возможности.")

            time.sleep(self.interval)

    def start(self):
        self.thread = threading.Thread(target=self.analyze, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.thread.join()


# ------------------ Запуск ------------------

if __name__ == "__main__":
    analyzer = RealTimeAnalyzer(interval=5)
    analyzer.start()
    print("Фоновый анализатор работает. Ctrl+C для остановки.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Остановка анализатора...")
        analyzer.stop()
        print("Анализатор завершил работу.")
