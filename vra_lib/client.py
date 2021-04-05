import json
import os
import requests
import logging
from requests.exceptions import HTTPError
from requests.exceptions import ConnectionError
from requests.exceptions import ConnectTimeout
from vra_lib.synchronization import Scheduler

# Eventlet Best Practices
# https://specs.openstack.org/openstack/openstack-specs/specs/eventlet-best-practices.html
if not os.environ.get('DISABLE_EVENTLET_PATCHING'):
    import eventlet
    eventlet.monkey_patch()


LOG = logging.getLogger(__name__)

class RetryPolicy(object):

    def __call__(self, func):

        def decorator(self, *args, **kwargs):
            function = "{}.{}".format(self.__class__.__name__, func.__name__)
            pattern_retry = "Retrying connection ({}/{}) with timeout {}s for {}"
            pattern_error = "HTTP Response URL={} Code={} Reason={} Content={}"

            until = self._config.connection_retries
            pause = self._config.connection_retries_seconds
            logger = self.logger

            info = "Login"
            if 'path' not in kwargs or 'login' not in kwargs.get('path'):
                info = "Function {} Arguments {}".format(function, str(kwargs))

            last_err = None            
            for attempt in range(1, until + 1):
                try:
                    response = func(self, *args, **kwargs)
                    if 200 <= response.status_code < 300 or response.status_code == 404:
                        return response
                    last_err = pattern_error.format(response.url, response.status_code,
                                                    response.reason, response.content)
                    if response.status_code >= 400 and response.status_code < 500:
                        if "Login" in info:
                            raise HttpUnsuccessfulException(last_err)
                        self.login()
                        continue
                    if response.status_code >= 300:
                        raise HttpUnsuccessfulException(last_err)
                    return response
                except (HTTPError, ConnectionError, ConnectTimeout) as err:
                    last_err = err
                    logger.error("Request={} Response={}".format(info, last_err))

                logger.info(pattern_retry.format(attempt, until, pause, function))
                eventlet.sleep(pause)
            raise Exception(info, last_err)

        return decorator


class VraClientConfig:
    host = None
    port = None
    username = None
    password = None
    domain = None 
    organization = None
    connection_retries = None
    connection_retries_seconds = None
    connection_timeout_seconds = None
    connection_throttling_rate = None
    connection_throttling_limit_seconds = None
    connection_throttling_timeout_seconds = None
    connection_query_limit = None
    connection_certificate_check = None
    cloud_zone = None
    logger = None


class VraClient:
    """
    Client for vRealize Automation 8.x
    """
    LOGIN_API = "/csp/gateway/am/api/login"

    def __init__(self, vraClientConfig):
        self._config = c = vraClientConfig

        self.logger = c.logger if c.logger else LOG
        self.api_scheduler = Scheduler(
            rate=c.connection_throttling_rate,
            limit=c.connection_throttling_limit_seconds,
            timeout=c.connection_throttling_timeout_seconds,
            logger=self.logger)
       
        self.base_url = "https://{}:{}".format(c.host, c.port)
        self.loginDetails = {
            "username": c.username, 
            "password": c.password, 
            "domain": c.domain
        }
        
        self.session = requests.session()
        self.session.verify = c.connection_certificate_check
        if not c.connection_certificate_check:
            requests.packages.urllib3.disable_warnings()
        self.timeout = c.connection_timeout_seconds
        

    def _get_url(self, path):
        return "{}{}".format(self.base_url, path)

    @RetryPolicy()
    def get(self, path):
        with self.api_scheduler:
            return self.session.get(url=self._get_url(path))

    @RetryPolicy()
    def delete(self, path):
        with self.api_scheduler:
            return self.session.delete(url=self._get_url(path))

    @RetryPolicy()
    def post(self, path, json):
        with self.api_scheduler:
            return self.session.post(url=self._get_url(path), json=json)

    @RetryPolicy()
    def put(self, path, json):
        with self.api_scheduler:
            return self.session.put(url=self._get_url(path), json=json)

    def login(self):
        self.logger.info("Acquiring vRA token from {} ...".format(self.base_url))
        content = json.loads(self.post(path=self.LOGIN_API, json=self.loginDetails).content)
        self.session.headers.update({
            'Authorization': 'Bearer {}'.format(content.get("cspAuthToken"))
        })

        self.logger.info("vRA token acquired")
