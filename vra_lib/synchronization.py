"""
Synchronization - classes related concurrent execution scheduling and limits
"""
import os
import time
import functools
import collections
import logging
from enum import Enum
if not os.environ.get('DISABLE_EVENTLET_PATCHING'):
    import eventlet
    eventlet.monkey_patch()

LOG = logging.getLogger(__name__)


class Scheduler(object):
    """ Synchronization.Scheduler.class limits the rate of execution of
        'with' section

        Keyword arguments:
        rate -- the rate of execution
        limit -- the limit of execution
    """

    def __init__(self, rate=1, limit=1.0, timeout=1, logger=None):

        if limit <= 0:
            raise ValueError('Schedule limit "{}" not positive'.format(limit))
        if rate <= 0:
            raise ValueError('Schedule rate "{}" not positive'.format(rate))

        self.schedule = collections.deque()

        self.rate = rate
        self.limit = limit
        self.timeout = timeout
        self.log = logger if logger else LOG

        # Callback reporting the limit was hit
        def callback(seconds):
            LOG.warning('Vra API Limit {:d}/s was hit. Sleeping for {:f}s.'
                        .format(limit, seconds))

        self.callback = callback
        self._semaphore = eventlet.semaphore.Semaphore(value=self.rate)

    def __call__(self, func):
        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            with self:
                return func(*args, **kwargs)
        return wrapped

    def __enter__(self):
        if self._semaphore.acquire(blocking=True, timeout=self.timeout):
            run_time = time.time()
            offset = len(self.schedule) - self.rate

            if offset >= 0 and run_time - self.limit < self.schedule[offset]:
                sleeptime = run_time - self.schedule[offset] + self.limit
                if self.callback:
                    eventlet.spawn(self.callback, sleeptime)
                eventlet.greenthread.sleep(sleeptime)
                run_time = self.schedule[offset] + self.limit
            self.schedule.append(run_time)
            return self
        raise Exception("{} Queue Size={}, Rate={}, Limit={}, Timeout={}"\
            .format("Timeout reached of trying to schedule operation.",
                    len(self.schedule), self.rate, self.limit, self.timeout))

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._semaphore.release()
        now = time.time()
        while self.schedule and self.schedule[0] < now - self.limit:
            self.schedule.popleft()
