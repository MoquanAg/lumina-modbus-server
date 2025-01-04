from typing import Callable, Dict, List
from dataclasses import dataclass
import queue
import threading
import time

@dataclass
class ModbusResponse:
    command_id: str
    data: bytes
    device_type: str
    status: str = 'success'  # success, timeout, error

class ModbusEventEmitter:
    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._response_queue = queue.Queue()
        self._running = True
        self._process_thread = threading.Thread(target=self._process_responses, daemon=True)
        self._process_thread.start()
    
    def subscribe(self, device_type: str, callback: Callable) -> None:
        if device_type not in self._subscribers:
            self._subscribers[device_type] = []
        self._subscribers[device_type].append(callback)
    
    def unsubscribe(self, device_type: str, callback: Callable) -> None:
        if device_type in self._subscribers:
            self._subscribers[device_type].remove(callback)
    
    def emit_response(self, response: ModbusResponse) -> None:
        self._response_queue.put(response)
    
    def _process_responses(self) -> None:
        while self._running:
            try:
                response = self._response_queue.get(timeout=0.1)
                if response.device_type in self._subscribers:
                    for callback in self._subscribers[response.device_type]:
                        callback(response)
            except queue.Empty:
                continue
    
    def stop(self) -> None:
        self._running = False
        if self._process_thread.is_alive():
            self._process_thread.join()