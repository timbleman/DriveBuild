from datetime import datetime
from enum import Enum

from beamngpy import Scenario
from celery.result import AsyncResult
from dataclasses import dataclass
from lxml.etree import _ElementTree

from aiExchangeMessages_pb2 import TestResult


class AIStatus(Enum):
    READY = "READY"
    WAITING = "WAITING"
    REQUESTED = "REQUESTED"


class ExtAsyncResult:
    """
    Wraps an AsyncResult and allows to set the returned status manually.
    """

    _state_to_str = {
        0: "TEST SUCCEEDED",
        1: "TEST FAILED",
        2: "TEST SKIPPED"
    }

    def __init__(self, task: AsyncResult):
        self.task = task
        self._status = None

    def state(self) -> str:
        return self.task.status if self._status is None else ExtAsyncResult._state_to_str[self._status]

    def get_state(self) -> TestResult.Result:
        """
        Returns the manually set state or None if no state was set manually.
        """
        return self._status

    def set_state(self, status: TestResult.Result) -> None:
        """
        Passing None as status makes disables a manually set status.
        """
        self._status = status


@dataclass
class SimulationData:
    scenario: Scenario
    simulation_task: ExtAsyncResult
    criteria: _ElementTree
    environment: _ElementTree
    start_time: datetime = None
    end_time: datetime = None
    result: TestResult.Result = None
