from typing import Callable, Dict, List, Optional
from dataclasses import dataclass
import queue
import threading
import time
import logging

logger = logging.getLogger(__name__)

@dataclass
class ModbusResponse:
    command_id: str
    data: Optional[bytes]
    device_type: str
    status: str = 'success'  # success, timeout, error, connection_lost
    timestamp: float = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()

class ModbusEventEmitter:
    def __init__(self, max_queue_size: int = 1000):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._response_queue = queue.Queue(maxsize=max_queue_size)
        self._running = True
        self._lock = threading.Lock()
        
        # Start processing thread
        self._process_thread = threading.Thread(
            target=self._process_responses, 
            name="ModbusEventProcessor",
            daemon=True
        )
        self._process_thread.start()
        
        # Add monitoring thread
        self._monitor_thread = threading.Thread(
            target=self._monitor_queue,
            name="ModbusQueueMonitor",
            daemon=True
        )
        self._monitor_thread.start()
    
    def subscribe(self, device_type: str, callback: Callable) -> None:
        """Subscribe a callback to a specific device type."""
        with self._lock:
            if device_type not in self._subscribers:
                self._subscribers[device_type] = []
            if callback not in self._subscribers[device_type]:
                self._subscribers[device_type].append(callback)
                logger.debug(f"Added subscriber for {device_type}")
    
    def unsubscribe(self, device_type: str, callback: Callable) -> None:
        """Unsubscribe a callback from a device type."""
        with self._lock:
            if device_type in self._subscribers:
                try:
                    self._subscribers[device_type].remove(callback)
                    logger.debug(f"Removed subscriber for {device_type}")
                except ValueError:
                    pass
    
    def emit_response(self, response: ModbusResponse) -> None:
        """Emit a response to the queue for processing."""
        try:
            self._response_queue.put(response, timeout=1.0)
        except queue.Full:
            logger.error(f"Response queue full, dropping response for {response.device_type}")
    
    def _process_responses(self) -> None:
        """Process responses from the queue and distribute to subscribers."""
        while self._running:
            try:
                response = self._response_queue.get(timeout=0.1)
                with self._lock:
                    subscribers = self._subscribers.get(response.device_type, []).copy()
                
                for callback in subscribers:
                    try:
                        callback(response)
                    except Exception as e:
                        logger.error(f"Error in callback for {response.device_type}: {str(e)}")
                
                self._response_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error processing response: {str(e)}")
    
    def _monitor_queue(self) -> None:
        """Monitor queue size and log warnings if it gets too large."""
        while self._running:
            queue_size = self._response_queue.qsize()
            if queue_size > self._response_queue.maxsize * 0.8:
                logger.warning(f"Response queue is {queue_size}/{self._response_queue.maxsize} full")
            time.sleep(5)
    
    def get_queue_size(self) -> int:
        """Get current size of response queue."""
        return self._response_queue.qsize()
    
    def get_subscriber_count(self, device_type: str = None) -> Dict[str, int]:
        """Get count of subscribers, optionally for specific device type."""
        with self._lock:
            if device_type:
                return {device_type: len(self._subscribers.get(device_type, []))}
            return {dt: len(subs) for dt, subs in self._subscribers.items()}
    
    def stop(self) -> None:
        """Stop the event emitter and its threads."""
        self._running = False
        if self._process_thread.is_alive():
            self._process_thread.join(timeout=1.0)
        if self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=1.0)
        
        # Clear any remaining items
        while not self._response_queue.empty():
            try:
                self._response_queue.get_nowait()
                self._response_queue.task_done()
            except queue.Empty:
                break