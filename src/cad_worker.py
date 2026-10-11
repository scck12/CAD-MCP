r"""COM 전용 작업 스레드.

문제
    CAD의 COM 호출은 동기 블로킹이다. 캐디안이 명령 대기 상태이거나 대화상자를
    띄우고 있으면 호출이 반환되지 않는다. COM은 STA 규칙을 따르므로 COM 객체는
    만든 스레드에서만 써야 한다.

구조
    작업 스레드 하나가 CADController(=COM 연결)를 평생 소유하고, 모든 CAD 작업은
    Job으로 큐에 넣어 그 스레드에서만 실행한다. 호출자는 결과를 기다리되 시간
    제한을 건다.

시간 제한 규칙 (2026-10-12 재설계)
    - 실행 시간은 "작업이 실제로 시작된 시점"부터 잰다. 앞 작업 때문에 큐에서
      기다린 시간은 포함하지 않는다. 예전에는 큐 대기까지 포함해서, 느린 작업
      뒤에 줄 선 정상 작업이 시간 초과로 처리되며 스레드 전체를 막힘(stuck)으로
      표시하는 일이 생길 수 있었다.
    - 시작 전에 포기한 작업은 취소 표시를 남겨, 나중에 스레드가 꺼내더라도
      실행하지 않는다. (호출자는 실패를 받았는데 나중에 도형이 생기는 일 방지)
    - 이미 시작한 작업이 시간을 넘기면 스레드를 막힘으로 표시하고 CADBusy를
      올린다. 그 작업이 나중에라도 끝나면 막힘을 자동으로 풀고, "늦게 완료됨"
      알림을 남겨 다음 도구 응답에 붙인다.

상태 확인
    status()는 COM을 전혀 건드리지 않고 작업 스레드가 기록해 둔 문자열 상태만
    읽는다. 그래서 다른 작업이 막혀 있어도 즉시 답할 수 있다.

종료 순서
    스레드를 끝낼 때는 컨트롤러가 쥔 COM 참조를 먼저 놓고 CoUninitialize를
    부른다. 순서가 반대면 이미 내려간 아파트먼트에서 Release가 불려 프로세스가
    비정상 종료할 수 있다.
"""
import logging
import queue
import threading
import time
from collections import deque
from typing import Any, Callable, List, Optional

import pythoncom

from cad_controller import CADController

logger = logging.getLogger('cad_worker')

DEFAULT_TIMEOUT = 30.0
CONNECT_TIMEOUT = 90.0    # 연결이 없을 때(캐디안 기동 포함)
LONG_TIMEOUT = 120.0      # 도면 전체 스캔, 대량 배치
QUEUE_WAIT_LIMIT = 120.0  # 앞 작업이 끝나길 기다리는 최대 시간


class CADBusy(RuntimeError):
    """CAD가 응답하지 않아 작업을 포기했을 때"""


class Job:
    """작업 스레드에 넘기는 작업 하나"""

    def __init__(self, func: Callable[[CADController], Any], label: str,
                 connect: bool):
        self.func = func
        self.label = label
        self.connect = connect
        self.lock = threading.Lock()
        self.started = threading.Event()
        self.done = threading.Event()
        self.cancelled = False
        self.timed_out = False
        self.t_start: Optional[float] = None
        self.value: Any = None
        self.error: Optional[BaseException] = None

    def elapsed(self) -> float:
        return 0.0 if self.t_start is None else time.monotonic() - self.t_start


class CADWorker:
    """CAD COM 연결을 소유하는 단일 작업 스레드"""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.default_timeout = timeout
        self._lock = threading.RLock()
        self._queue: queue.Queue = queue.Queue()
        self._alive = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._generation = 0
        self._stuck_job: Optional[Job] = None
        self._current: Optional[Job] = None
        # 작업 스레드가 매 작업 후 기록하는 평문 상태 (status가 COM 없이 읽음)
        self._connected = False
        self._doc_name: Optional[str] = None
        self._last_error: Optional[str] = None
        self._notices: deque = deque(maxlen=20)

    # ------------------------------------------------------------------
    # 작업 스레드 본체
    # ------------------------------------------------------------------

    def _loop(self, own_queue: queue.Queue, own_alive: threading.Event,
              generation: int):
        pythoncom.CoInitialize()
        logger.info(f"CAD 작업 스레드 시작 (세대 {generation})")
        controller: Optional[CADController] = None
        try:
            controller = CADController()
            while own_alive.is_set():
                try:
                    job = own_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if job is None:
                    break
                with job.lock:
                    if job.cancelled:
                        job.done.set()
                        continue
                    job.t_start = time.monotonic()
                    job.started.set()
                self._execute(controller, job, generation)
        except Exception as e:
            logger.exception(f"작업 스레드 예외로 종료: {e}")
        finally:
            # COM 참조를 먼저 놓고, 그 다음 CoUninitialize
            if controller is not None:
                try:
                    controller.release()
                except Exception:
                    pass
                controller = None
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
            logger.info(f"CAD 작업 스레드 종료 (세대 {generation})")

    def _execute(self, controller: CADController, job: Job, generation: int):
        with self._lock:
            if generation == self._generation:
                self._current = job
        try:
            if job.connect:
                notice = controller.prepare()
                if notice:
                    self._notice(notice)
            job.value = job.func(controller)
        except BaseException as e:   # noqa: BLE001 - 호출자에게 그대로 전달
            job.error = e
        finally:
            with self._lock:
                if generation == self._generation:
                    self._connected = controller.is_running()
                    self._doc_name = controller.doc_name
                    self._current = None
                    if job.error is not None:
                        self._last_error = f"{job.label}: {job.error}"
                    if job.timed_out:
                        outcome = ("실패: " + str(job.error)) if job.error else "성공"
                        self._notices.append(
                            f"앞서 시간 초과로 실패 처리된 '{job.label}' 작업이 "
                            f"{job.elapsed():.0f}초 만에 늦게 끝났습니다({outcome}). "
                            "도면에 그 결과가 반영됐을 수 있으니 확인하세요.")
                        logger.warning(f"늦게 완료된 작업: {job.label} ({outcome})")
                    if self._stuck_job is job:
                        self._stuck_job = None
                        self._notices.append(
                            "막혀 있던 CAD 연결이 스스로 풀렸습니다. 다시 사용할 수 있습니다.")
            job.done.set()

    # ------------------------------------------------------------------
    # 스레드 관리
    # ------------------------------------------------------------------

    def _spawn(self) -> None:
        """새 큐/스레드 생성. self._lock 안에서만 호출"""
        self._generation += 1
        self._queue = queue.Queue()
        self._alive = threading.Event()
        self._alive.set()
        self._stuck_job = None
        self._current = None
        self._connected = False
        self._doc_name = None
        self._thread = threading.Thread(
            target=self._loop, args=(self._queue, self._alive, self._generation),
            daemon=True, name=f'cad-com-{self._generation}')
        self._thread.start()

    def start(self) -> None:
        """스레드가 없거나 죽었으면 시작"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._thread is not None:
                logger.warning("작업 스레드가 죽어 있어 새로 시작합니다")
            self._spawn()

    def restart(self) -> None:
        """현재 스레드를 버리고 새 스레드로 교체 (cad_reconnect)

        대기열에 남아 있던 작업은 실행하지 않고 취소 통보한다. 막혀 있는
        옛 스레드는 COM 호출이 풀리는 순간 스스로 종료한다.
        """
        with self._lock:
            old_queue, old_alive = self._queue, self._alive
            old_alive.clear()
            cancelled = 0
            while True:
                try:
                    job = old_queue.get_nowait()
                except queue.Empty:
                    break
                if job is None:
                    continue
                with job.lock:
                    if not job.started.is_set():
                        job.cancelled = True
                        job.error = CADBusy("연결 재시작으로 취소된 요청입니다. 다시 호출하세요.")
                        job.done.set()
                        cancelled += 1
            old_queue.put(None)   # 쉬고 있던 옛 스레드를 바로 깨워 종료시킨다
            logger.warning(f"작업 스레드 교체 (대기 작업 {cancelled}개 취소)")
            self._spawn()

    # ------------------------------------------------------------------
    # 작업 실행
    # ------------------------------------------------------------------

    def _stuck_message(self) -> str:
        job = self._stuck_job
        what = f"'{job.label}' 작업이 {job.elapsed():.0f}초째 끝나지 않아" if job else "이전 요청 때문에"
        return (f"CAD가 응답하지 않는 상태입니다({what} 막힘). "
                "캐디안 창에 대화상자나 명령 입력 대기가 떠 있는지 확인하고 ESC를 "
                "두어 번 누른 뒤 다시 시도하세요. 작업이 끝나면 자동으로 풀립니다. "
                "계속 안 풀리면 cad_reconnect를 호출하세요.")

    def run(self, func: Callable[[CADController], Any],
            timeout: Optional[float] = None, label: str = "CAD 작업",
            connect: bool = True) -> Any:
        """작업 스레드에서 func(controller)를 실행하고 결과를 반환"""
        self.start()
        with self._lock:
            if self._stuck_job is not None:
                raise CADBusy(self._stuck_message())
            work_queue = self._queue
            generation = self._generation
            limit = timeout if timeout is not None else self.default_timeout
            if connect and not self._connected:
                limit = max(limit, CONNECT_TIMEOUT)

        job = Job(func, label, connect)
        work_queue.put(job)

        # 1) 시작 대기
        waited = 0.0
        while not job.started.wait(0.25):
            waited += 0.25
            if job.done.is_set():          # restart()가 취소함
                break
            with self._lock:
                blocked = self._stuck_job is not None and self._generation == generation
                replaced = self._generation != generation
            if blocked or replaced or waited >= QUEUE_WAIT_LIMIT:
                with job.lock:
                    if not job.started.is_set():
                        job.cancelled = True
                        if replaced:
                            raise CADBusy("연결 재시작으로 취소된 요청입니다. 다시 호출하세요.")
                        if blocked:
                            raise CADBusy(self._stuck_message())
                        raise CADBusy(f"앞선 작업이 {QUEUE_WAIT_LIMIT:.0f}초 넘게 끝나지 않아 "
                                      f"'{label}' 요청을 실행하지 못했습니다.")
                break   # 마지막 순간에 시작됨

        # 2) 실행 대기 (시작 시점부터 limit초)
        if not job.done.wait(limit):
            job.timed_out = True
            with self._lock:
                if self._generation == generation and not job.done.is_set():
                    self._stuck_job = job
            if not job.done.is_set():
                raise CADBusy(f"'{label}' 작업이 {limit:.0f}초 안에 끝나지 않았습니다. "
                              + self._stuck_message())

        if job.error is not None:
            raise job.error
        return job.value

    # ------------------------------------------------------------------
    # 상태 / 알림
    # ------------------------------------------------------------------

    def _notice(self, text: str) -> None:
        with self._lock:
            self._notices.append(text)

    def pop_notices(self) -> List[str]:
        with self._lock:
            items = list(self._notices)
            self._notices.clear()
        return items

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._current is not None

    @property
    def stuck(self) -> bool:
        with self._lock:
            return self._stuck_job is not None

    def status(self) -> dict:
        """COM 호출 없이 현재 상태를 보고"""
        with self._lock:
            thread_alive = bool(self._thread and self._thread.is_alive())
            info = {
                "thread_alive": thread_alive,
                "stuck": self._stuck_job is not None,
                "connected": self._connected,
                "document": self._doc_name,
            }
            if self._current is not None:
                info["busy"] = {"job": self._current.label,
                                "seconds": round(self._current.elapsed(), 1)}
            if self._stuck_job is not None:
                info["stuck_job"] = {"job": self._stuck_job.label,
                                     "seconds": round(self._stuck_job.elapsed(), 1)}
            if not thread_alive:
                info["state"] = "not_started"
                info["note"] = "아직 CAD 작업을 한 번도 안 했습니다. 첫 작업 때 자동 연결됩니다."
            elif self._stuck_job is not None:
                info["state"] = "stuck"
            elif self._current is not None:
                info["state"] = "busy"
            elif not self._connected:
                info["state"] = "idle_not_connected"
                info["note"] = "첫 CAD 작업 때 자동 연결됩니다."
            else:
                info["state"] = "ready"
            if self._last_error:
                info["last_error"] = self._last_error[:300]
        return info
