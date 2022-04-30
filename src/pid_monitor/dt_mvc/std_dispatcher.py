import statistics
import threading
from time import sleep
from typing import Dict

import psutil

from pid_monitor import _ALL_PIDS, PSUTIL_NOTFOUND_ERRORS, DEFAULT_REFRESH_INTERVAL, DEFAULT_SYSTEM_INDICATOR_PID
from pid_monitor import get_timestamp, get_total_cpu_time
from pid_monitor.dt_mvc.base_dispatcher_class import BaseTracerDispatcherThread
from pid_monitor.dt_mvc.dispatcher_controller import register_dispatcher, \
    get_current_active_pids, remove_dispatcher

_REG_MUTEX = threading.Lock()
"""Mutex for writing registries"""

_DISPATCHER_MUTEX = threading.Lock()
"""Mutex for creating dispatchers for new process/thread"""


def _to_human_readable(num: int, base: int = 1024) -> str:
    """
    Make an integer to 1000- or 1024-based human-readable form.
    """
    if base == 1024:
        dc_list = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB']
    elif base == 1000:
        dc_list = ['B', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB']
    else:
        raise ValueError("base should be 1000 or 1024")
    step = 0
    dc = dc_list[step]
    while num > base and step < len(dc_list) - 1:
        step = step + 1
        num /= base
        dc = dc_list[step]
    num = round(num, 2)
    return str(num) + dc


class ProcessTracerDispatcherThread(BaseTracerDispatcherThread):
    """
    The dispatcher, use to monitor whether a process have initiated a sub-process
    and attach a dispatcher to it if it does so.

    Also initializes and monitors task monitors like :py:class:`TraceIOThread`.
    """

    _cached_last_cpu_time: float

    _frontend_cache: Dict[str, str]

    def collect_information(self) -> Dict[str, str]:
        return self._frontend_cache

    def __init__(self, trace_pid: int, basename: str, loaded_tracers=None):
        """
        The constructor of the class will do following things:

        - Detech whether this process exists. If not exists, will raise an error.
        - Add PID to all recorded PIDS.
        - Write system-level registry.
        - Write initializing environment variables of this process.
        - Write mapfile information.
        - Start load_tracers.
        """
        super().__init__(str(trace_pid), basename)
        register_dispatcher(trace_pid, self)
        self.loaded_tracers = loaded_tracers
        if self.loaded_tracers is None:
            self.loaded_tracers = (
                "ProcessIOTracerThread",
                "ProcessFDTracerThread",
                "ProcessMEMTracerThread",
                "ProcessChildTracerThread",
                "ProcessCPUTracerThread",
                "ProcessSTATTracerThread",
                "ProcessSyscallTracerThread"
            )
        self.trace_pid = trace_pid

        try:
            self.process = psutil.Process(self.trace_pid)
            self._setup_cache()
        except PSUTIL_NOTFOUND_ERRORS as e:
            self.log_handler.error(f"TRACEE={self.trace_pid}: {e.__class__.__name__} encountered!")
            raise e

        _ALL_PIDS.add(trace_pid)

        self._write_registry()
        self._write_env()
        self._write_mapfile()

        self.tracer_kwargs["trace_pid"] = trace_pid
        self.start_tracers()

    def run_body(self):
        """
        The major running part of this function performs following things:

        - Refresh CPU time.
        - Detect whether the process have forked sub-processes.
        - Detect whether the process have created new threads.
        """
        while not self.should_exit:
            self._update_cache()
            try:
                with _DISPATCHER_MUTEX:
                    self._detect_process()
            except PSUTIL_NOTFOUND_ERRORS:
                break
            sleep(DEFAULT_REFRESH_INTERVAL)
        self.sigterm()

    def _write_registry(self):
        """
        Write registry information with following information:

        - Process ID
        - Commandline
        - Executable path
        - Current working directory
        """
        try:
            with _REG_MUTEX:
                with open(f"{self.basename}.reg.tsv", mode='a') as writer:
                    writer.write('\t'.join((
                        get_timestamp(),
                        str(self.trace_pid),
                        " ".join(self.process.cmdline()),
                        self.process.exe(),
                        self.process.cwd()
                    )) + '\n')
                    writer.flush()
        except PSUTIL_NOTFOUND_ERRORS as e:
            self.log_handler.error(f"TRACEE={self.trace_pid}: {e.__class__.__name__} encountered!")
            raise e

    def _write_env(self):
        """
        Write initializing environment variables.

        If the process changes its environment variable during execution, it will NOT be recorded!
        """
        try:
            with open(f"{self.basename}.{self.trace_pid}.env.tsv", mode='w') as writer:
                writer.write('\t'.join(("NAME", "VALUE")) + '\n')
                for env_name, env_value in self.process.environ().items():
                    writer.write('\t'.join((env_name, env_value)) + '\n')
        except PSUTIL_NOTFOUND_ERRORS as e:
            self.log_handler.error(f"TRACEE={self.trace_pid}: {e.__class__.__name__} encountered!")
            raise e

    def _write_mapfile(self):
        """
        Write mapfile information.

        Mapfile information shows how files, especially libraries are stored in memory.
        """
        try:
            with open(f"{self.basename}.{self.trace_pid}.mapfile.tsv", mode='w') as writer:
                writer.write('\t'.join(("PATH", "RESIDENT", "VIRT", "SWAP")) + '\n')
                for item in self.process.memory_maps():
                    writer.write('\t'.join((
                        item.path,
                        str(item.rss),
                        str(item.size),
                        str(item.swap)
                    )) + '\n')
        except PSUTIL_NOTFOUND_ERRORS as e:
            self.log_handler.error(f"TRACEE={self.trace_pid}: {e.__class__.__name__} encountered!")
            raise e

    def _detect_process(self):
        """
        Detect and start child process dispatcher.
        """
        try:
            for process in self.process.children():
                if process.pid not in get_current_active_pids():
                    self.log_handler.info(f"Sub-process {process.pid} detected.")
                    new_thread = ProcessTracerDispatcherThread(process.pid, self.basename)
                    new_thread.start()
                    self.append_threadpool(new_thread)
        except PSUTIL_NOTFOUND_ERRORS as e:
            self.log_handler.error(f"TRACEE={self.trace_pid}: {e.__class__.__name__} encountered!")
            raise e

    def before_ending(self):
        """
        This function performs following things:

        - Record CPU time.
        """
        with open(f"{self.basename}.{self.trace_pid}.cputime", mode='w') as writer:
            writer.write(str(self._cached_last_cpu_time) + '\n')
        remove_dispatcher(self.trace_pid)

    def _setup_cache(self):
        """
        Setup cached values
        """
        self._cached_last_cpu_time = 0
        self._frontend_cache = {
            "PID": str(self.trace_pid),
            "PPID": "NA",
            "NAME": "NA",
            "CPU%": "NA",
            "STAT": "NA",
            "CPU_TIME": str(self._cached_last_cpu_time),
            "RESIDENT_MEM": "NA",
            "NUM_THREADS": "NA",
            "NUM_CHILD_PROCESS": "NA"
        }

        try:
            self._frontend_cache["NAME"] = self.process.name()
            self._frontend_cache["PPID"] = str(self.process.ppid())
        except PSUTIL_NOTFOUND_ERRORS as e:
            raise e

    def _update_cache(self):
        self._cached_last_cpu_time = max(
            self._cached_last_cpu_time,
            get_total_cpu_time(self.process)
        )
        self._frontend_cache["CPU_TIME"] = str(
            round(self._cached_last_cpu_time, 2)
        )
        self._frontend_cache["CPU%"] = str(
            self.thread_pool["ProcessCPUTracerThread"].get_cached_cpu_percent()
        )
        self._frontend_cache["RESIDENT_MEM"] = _to_human_readable(
            self.thread_pool["ProcessMEMTracerThread"].get_cached_resident_mem()
        )
        thread_child_process_num = \
            self.thread_pool["ProcessChildTracerThread"].get_cached_thread_child_process_num()

        self._frontend_cache["NUM_THREADS"] = str(
            thread_child_process_num[0]
        )
        self._frontend_cache["NUM_CHILD_PROCESS"] = str(
            thread_child_process_num[1]
        )
        self._frontend_cache["STAT"] = self.thread_pool["ProcessSTATTracerThread"].get_cached_stat()


class SystemTracerDispatcherThread(BaseTracerDispatcherThread):
    """
    The dispatcher, use to monitor whether a process have initiated a sub-process
    and attach a dispatcher to it if it does so.

    Also initializes and monitors task monitors like :py:class:`TraceIOThread`.
    """

    _frontend_cache: Dict[str, str]

    def _setup_cache(self):
        self._frontend_cache = {
            "CPU%": "NA",
            "VM_AVAIL": "NA",
            "VM_TOTAL": "NA",
            "VM_PERCENT": "NA",
            "BUFFERED": "NA",
            "SHARED": "NA",
            "SWAP_AVAIL": "NA",
            "SWAP_TOTAL": "NA",
            "SWAP_PERCENT": "NA"
        }

    def before_ending(self):
        """Disabled"""
        pass

    def collect_information(self) -> Dict[str, str]:
        return self._frontend_cache

    def __init__(self, basename: str, loaded_tracers=None):
        super().__init__(dispatchee="sys", basename=basename)
        register_dispatcher(DEFAULT_SYSTEM_INDICATOR_PID, self)
        self._setup_cache()
        self.loaded_tracers = loaded_tracers
        if self.loaded_tracers is None:
            self.loaded_tracers = (
                "SystemMEMTracerThread",
                "SystemCPUTracerThread",
                "SystemSWAPTracerThread"
            )
        self._write_mnt()
        self.start_tracers()

    def run_body(self):
        while not self.should_exit:
            self._update_cache()
            sleep(self.interval)

    def _write_mnt(self):
        """
        Write mounted volumes to ``mnt.csv``.
        """
        with open(f"{self.basename}.mnt.tsv", mode='w') as writer:
            writer.write('\t'.join((
                "DEVICE",
                "MOUNT_POINT",
                "FSTYPE",
                "OPTS",
                "TOTAL",
                "USED"
            )) + '\n')
            for item in psutil.disk_partitions():
                disk_usage = psutil.disk_usage(item.mountpoint)
                writer.write('\t'.join((
                    item.device,
                    item.mountpoint,
                    item.fstype,
                    item.opts,
                    str(disk_usage.total),
                    str(disk_usage.used)
                )) + '\n')

    def _update_cache(self):
        self._frontend_cache["CPU%"] = str(round(
            statistics.mean(self.thread_pool["SystemCPUTracerThread"].get_cached_cpu_percent()), 2
        )) + "%"
        mem_info = self.thread_pool["SystemMEMTracerThread"].get_cached_vm_info()
        self._frontend_cache["VM_AVAIL"] = _to_human_readable(mem_info[0])
        self._frontend_cache["VM_TOTAL"] = _to_human_readable(mem_info[1])
        if mem_info[1] != 0:
            self._frontend_cache["VM_PERCENT"] = str(round(mem_info[0]/mem_info[1] * 100, 2)) + "%"
        else:
            self._frontend_cache["VM_PERCENT"] = "0.00%"
        self._frontend_cache["BUFFERED"] = _to_human_readable(mem_info[2])
        self._frontend_cache["SHARED"] = _to_human_readable(mem_info[3])
        swap_info = self.thread_pool["SystemSWAPTracerThread"].get_cached_swap_info()
        self._frontend_cache["SWAP_AVAIL"] = _to_human_readable(swap_info[0])
        self._frontend_cache["SWAP_TOTAL"] = _to_human_readable(swap_info[1])
        if swap_info[1] != 0:
            self._frontend_cache["SWAP_PERCENT"] = str(round(swap_info[0]/swap_info[1] * 100, 2)) + "%"
        else:
            self._frontend_cache["SWAP_PERCENT"] = "0.00%"