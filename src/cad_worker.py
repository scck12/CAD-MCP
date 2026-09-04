r"""COM 전용 작업 스레드.

문제
    CAD의 COM 호출은 동기 블로킹이다. 캐디안이 명령 대기 상태이거나 대화상자를
    띄우고 있으면 호출이 무한정 반환되지 않는데, 이것이 asyncio 이벤트 루프와
    같은 스레드에서 일어나면 MCP 서버 전체가 정지한다.

해결
    COM 연결을 소유하는 전용 스레드를 하나 두고, 모든 CAD 작업을 그 스레드에
    맡긴다. 호출자는 타임아웃을 걸고 기다린다. 시간이 초과되면 작업 스레드는
    막힌 채로 두고 호출자만 빠져나와, 서버는 계속 응답할 수 있다.

    COM 아파트먼트 규칙상 객체는 만든 스레드에서만 안전하게 쓸 수 있으므로,
    도면을 읽는 코드도 이 스레드 안에서 실행해야 한다. run()에 함수를 넘기면
    컨트롤러를 인자로 받아 작업 스레드에서 실행된다.
"""
import logging
import queue
import threading
from typing import Any, Callable, Optional

import pythoncom

from cad_controller import CADController

logger = logging.getLogger('cad_worker')

DEFAULT_TIMEOUT = 45.0
CONNECT_TIMEOUT = 90.0   # CAD 최초 실행은 오래 걸릴 수 있다


class CADBusy(RuntimeError):
    """CAD가 응답하지 않아 작업을 포기했을 때"""


class CADWorker:
    """CAD COM 연결을 소유하는 단일 작업 스레드"""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.default_timeout = timeout
        self._queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._alive = threading.Event()
        self._stuck = False
        self._lock = threading.RLock()
        self._controller: Optional[CADController] = None
        self._started = False

    # ------------------------------------------------------------------

    def _loop(self, own_queue: queue.Queue, own_alive: threading.Event):
        """작업 스레드 본체"""
        pythoncom.CoInitialize()
        logger.info("CAD 작업 스레드 시작")
        try:
            controller = CADController()
        except Exception as e:
            logger.error(f"컨트롤러 생성 실패: {e}")
            controller = None

        with self._lock:
            if own_alive.is_set():
                self._controller = controller

        while own_alive.is_set():
            try:
                job = own_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                break

            func, box = job
            try:
                box['value'] = func(controller)
            except Exception as e:
                box['error'] = e
            finally:
                box['event'].set()

        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
        logger.info("CAD 작업 스레드 종료")

    def start(self) -> None:
        """스레드가 없거나 죽었으면 시작"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and not self._stuck:
                return
            if self._stuck:
                logger.warning("막힌 작업 스레드를 버리고 새로 시작합니다")
                self._alive.clear()          # 옛 스레드는 자연 종료되도록
            self._queue = queue.Queue()
            self._alive = threading.Event()
            self._alive.set()
            self._stuck = False
            self._controller = None
            self._thread = threading.Thread(
                target=self._loop, args=(self._queue, self._alive),
                daemon=True, name='cad-com')
            self._thread.start()
            self._started = True

    def restart(self) -> None:
        """막힌 연결을 버리고 새 연결로 교체"""
        with self._lock:
            self._stuck = True
        self.start()

    @property
    def stuck(self) -> bool:
        return self._stuck

    # ------------------------------------------------------------------

    def run(self, func: Callable[[CADController], Any],
            timeout: Optional[float] = None) -> Any:
        """작업 스레드에서 func(controller)를 실행하고 결과를 반환

        시간이 초과되면 CADBusy를 올린다. 작업 스레드는 막힌 채로 남지만
        호출자(=MCP 서버)는 계속 동작한다.
        """
        self.start()

        with self._lock:
            if self._stuck:
                raise CADBusy(
                    "CAD가 이전 요청에 응답하지 않는 상태입니다. "
                    "캐디안 창에서 ESC를 두어 번 눌러 명령을 취소한 뒤 "
                    "다시 시도하세요.")
            work_queue = self._queue

        box = {'event': threading.Event()}
        work_queue.put((func, box))

        limit = timeout if timeout is not None else self.default_timeout
        if not box['event'].wait(limit):
            with self._lock:
                self._stuck = True
            raise CADBusy(
                f"CAD가 {limit:.0f}초 안에 응답하지 않았습니다. "
                "캐디안이 명령 대기 중이거나 대화상자를 띄우고 있을 수 있습니다. "
                "캐디안 창에서 ESC를 두어 번 누른 뒤 다시 시도하세요. "
                "계속 안 되면 캐디안을 재시작하세요.")

        if 'error' in box:
            raise box['error']
        return box.get('value')

    def health_check(self, timeout: float = 5.0) -> dict:
        """가벼운 호출로 응답성 확인"""
        info = {"thread_alive": bool(self._thread and self._thread.is_alive()),
                "stuck": self._stuck}
        try:
            def probe(controller):
                if controller is None:
                    return None
                if not controller.is_running():
                    return "not_connected"
                return controller.doc.Name

            info["document"] = self.run(probe, timeout=timeout)
            info["responsive"] = True
        except CADBusy as e:
            info["responsive"] = False
            info["message"] = str(e)
        except Exception as e:
            info["responsive"] = False
            info["message"] = f"{type(e).__name__}: {e}"
        return info


class ControllerProxy:
    """CADController처럼 보이지만 모든 호출을 작업 스레드로 넘기는 대리자

    기존 CADService 코드를 그대로 두고 self.controller만 교체하면 된다.
    """

    def __init__(self, worker: CADWorker):
        self._worker = worker

    def __getattr__(self, name: str):
        if name.startswith('_'):
            raise AttributeError(name)

        def call(*args, **kwargs):
            timeout = kwargs.pop('_timeout', None)
            return self._worker.run(
                lambda c: getattr(c, name)(*args, **kwargs), timeout=timeout)

        return call
