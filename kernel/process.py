"""
PyOS Process Manager
Manages the process table, spawning, scheduling, and signals.
"""

import time
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Process:
    """Process."""
    pid:   int
    name:  str
    user:  str
    cmd:   str
    state: str  = "S"          # R=running, S=sleeping, Z=zombie, T=stopped
    cpu:   float = 0.0
    mem:   float = 0.0
    start: float = field(default_factory=time.time)
    nice:  int   = 0

    @property
    def runtime(self) -> float:
        """Runtime.


            Returns:
                float: Result.
            """
        return time.time() - self.start


PROTECTED_PIDS = {1, 2, 3, 4}   # kernel processes


class ProcessManager:
    """
    In-process process table.  Processes are pure data; actual execution
    is Python threads managed by the shell / applications.
    """

    def __init__(self):
        """Initialise the instance."""
        self._table: Dict[int, Process] = {}
        self._next_pid = 5

    # ------------------------------------------------------------------ spawn
    def spawn(
        self,
        name: str,
        pid: int = None,
        user: str = "root",
        cmd: str = "",
        state: str = "S",
    ) -> int:
        """Spawn.

            Args:
            name (str): Name.
            pid (int): Pid, defaults to None.
            user (str): User, defaults to 'root'.
            cmd (str): Cmd, defaults to ''.
            state (str): State, defaults to 'S'.


            Returns:
                int: Result.
            """
        if pid is None:
            pid = self._next_pid
            self._next_pid += 1
        proc = Process(
            pid=pid,
            name=name,
            user=user,
            cmd=cmd or f"/bin/{name}",
            state=state,
            cpu=round(random.uniform(0, 0.5), 1),
            mem=round(random.uniform(2, 20), 1),
        )
        self._table[pid] = proc
        return pid

    # ------------------------------------------------------------------ kill
    def kill(self, pid: int, signal: int = 15) -> bool:
        """Kill.

            Args:
            pid (int): Pid.
            signal (int): Signal, defaults to 15.


            Returns:
                bool: Result.
            """
        if pid in PROTECTED_PIDS:
            raise PermissionError(f"kill: ({pid}): Operation not permitted")
        if pid not in self._table:
            raise ProcessLookupError(f"kill: ({pid}): No such process")
        self._table[pid].state = "Z"
        del self._table[pid]
        return True

    # ------------------------------------------------------------------ query
    def get(self, pid: int) -> Optional[Process]:
        """Return the the operation.

            Args:
            pid (int): Pid.


            Returns:
                Optional[Process]: Result.
            """
        return self._table.get(pid)

    def all(self) -> List[Process]:
        """All.


            Returns:
                List[Process]: Result.
            """
        return sorted(self._table.values(), key=lambda p: p.pid)

    def by_user(self, user: str) -> List[Process]:
        """By user.

            Args:
            user (str): User.


            Returns:
                List[Process]: Result.
            """
        return [p for p in self._table.values() if p.user == user]

    def count(self) -> int:
        """Return the number of the operation.


            Returns:
                int: Result.
            """
        return len(self._table)
