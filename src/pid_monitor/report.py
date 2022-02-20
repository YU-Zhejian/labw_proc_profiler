"""
This module generates R reports.
"""

import logging
import os
import subprocess
import threading
from typing import Set

from pid_monitor import parallel_helper

_LOG_HANDLER = logging.getLogger()
"""The Logger Handler"""

_FILE_DIR = os.path.dirname(__file__)
"""Current directory, used for calling R processes"""

_R_FILE_DIR = os.path.join(_FILE_DIR, 'R')
"""Current directory, used for calling R processes"""

_RENV_CWD = os.path.dirname(os.path.dirname(_FILE_DIR))
"""Directory of renv, used for calling R processes"""


class _MakeIndividualReportThread(threading.Thread):
    def __init__(self, this_pid: int, report_basename: str):
        super().__init__()
        self.this_pid = this_pid
        self.report_basename = report_basename
        self.logger = logging.getLogger()

    def run(self) -> None:
        if self.this_pid == 0:
            log_filename = f'{self.report_basename}_report_system.log'
        else:
            log_filename = f'{self.report_basename}_report_{self.this_pid}_.log'
        log_writer = open(log_filename, "wt")
        if self.this_pid == 0:
            report_process = subprocess.Popen((
                'Rscript',
                os.path.join(_R_FILE_DIR, 'make_system_report.R'),
                '--basename', self.report_basename,
                '--rmd', os.path.join(_R_FILE_DIR, 'system_report.Rmd')
            ),
                cwd=_RENV_CWD,
                stdout=log_writer,
                stderr=log_writer
            )
        else:
            report_process = subprocess.Popen((
                'Rscript',
                os.path.join(_R_FILE_DIR, 'make_process_report.R'),
                '--pid', str(self.this_pid),
                '--basename', self.report_basename,
                '--rmd', os.path.join(_R_FILE_DIR, 'process_report.Rmd')
            ),
                cwd=_RENV_CWD,
                stdout=log_writer,
                stderr=log_writer
            )

        self.logger.debug(f"{' '.join(report_process.args)} ADD")
        retv = report_process.wait()
        log_writer.close()
        if retv == 0:
            self.logger.debug(f"{' '.join(report_process.args)} FIN")
            os.remove(log_filename)
        else:
            self.logger.error(f"{' '.join(report_process.args)} ERR")


def make_all_report(all_pids: Set[int], report_basename: str):
    """
    Generate all report for both system and process asynchronously
    """
    parallel_job_queue = parallel_helper.ParallelJobQueue(pool_name="Compiling HTMLs")
    all_pids.add(0)
    for this_pid in all_pids:
        parallel_job_queue.append(_MakeIndividualReportThread(this_pid=this_pid, report_basename=report_basename))
    parallel_job_queue.start()
    parallel_job_queue.join()
    _LOG_HANDLER.info("All finished")