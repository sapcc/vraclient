import os

# Eventlet Best Practices
# https://specs.openstack.org/openstack/openstack-specs/specs/eventlet-best-practices.html
import eventlet
if not os.environ.get('DISABLE_EVENTLET_PATCHING'):
    eventlet.monkey_patch()

import logging
import requests
import json
import time
import constants
from requests.exceptions import HTTPError
from requests.exceptions import ConnectionError
from requests.exceptions import ConnectTimeout
import synchronization as sync

LOG = logging.getLogger(__name__)

RESOURCE_TRACKER_SLEEP = 5.0

class VraUnauthorized(Exception):
    pass


def connection_retry_policy(func):
    def decorator(self, *args, **kwargs):
        method = "{}.{}".format(self.__class__.__name__, func.__name__)
        max_login_attempts = 3
        until = 0
        pause = 60

        pattern = "Retrying connection ({}/{}) with timeout {}s for {}"

        login_attempts = 0
        now = 1
        while True:
            resp = 0
            try:
                resp = func(self, *args, **kwargs)

                LOG.debug(
                    "HTTP Response URL={} Method={} Code={} Reason={}"
                    .format(resp.url, resp.request.method,
                            resp.status_code, resp.reason))

                if resp.status_code == 404:
                    return resp

                if resp.status_code >= 400 and resp.status_code < 500:
                    raise VraUnauthorized(resp.content)

                if resp.status_code >= 300:
                    msg = "HTTP Response URL={} Code={} Reason={} \
                        Content={}".format(resp.url, resp.status_code,
                                           resp.reason, resp.content)
                    raise HttpUnsuccessfulException(msg)
                return resp
            except (HTTPError, ConnectionError, ConnectTimeout) as err:
                LOG.error("Unable to connect. Error: {}".format(err))
            except VraUnauthorized as err:
                if resp.status_code == 401:
                    LOG.error("Unauthorized: {}".format(err))
                    if login_attempts < max_login_attempts:
                        login_attempts += 1
                        self.login()
                        continue
                    else:
                        raise err
                else:
                    LOG.error("Error: {}".format(err))
                    raise err

            now += 1
            if now > until:
                raise Exception("Failed. {}".format(
                    pattern.format(now, until, pause, method)))
            LOG.debug(msg)
            eventlet.sleep(pause)
        return None

    return decorator


class VraRestClient(object):
    """
    This class is reponsible to orchestrate the Openstack to vRA communication using vRA REST API
    """
    LOGIN_API = "/csp/gateway/am/api/login"
    BLUEPRINT_REQUESTS_API = "/blueprint/api/blueprint-requests"
    BLUEPRINTS_API = "/blueprint/api/blueprints"

    REQUEST_SSL_VERIFY = False
    REQUEST_TIMEOUT = 60

    def __init__(self, api_scheduler, base_url, username, password, domain):
        """Constructor"""
        logging.basicConfig(level=logging.INFO)
        self.api_scheduler = api_scheduler
        self.base_url = base_url
        self.username = username
        self.password = password
        self.domain = domain

        requests.packages.urllib3.disable_warnings()
        self.session = requests.session()

    # START #### A set of methods encapsulating the vRA API queue

    def _get_url(self, path):
        return "{}{}".format(self.base_url, path)

    @connection_retry_policy
    def _post(self, path, json):
        with self.api_scheduler:
            return self.session.post(
                url=self._get_url(path),
                json=json,
                verify=self.REQUEST_SSL_VERIFY,
                timeout=self.REQUEST_TIMEOUT)

    @connection_retry_policy
    def _get(self, path):
        with self.api_scheduler:
            return self.session.get(
                url=self._get_url(path),
                verify=self.REQUEST_SSL_VERIFY,
                timeout=self.REQUEST_TIMEOUT)

    @connection_retry_policy
    def _put(self, path, json):
        with self.api_scheduler:
            return self.session.put(
                url=self._get_url(path),
                json=json,
                verify=self.REQUEST_SSL_VERIFY,
                timeout=self.REQUEST_TIMEOUT)

    @connection_retry_policy
    def _delete(self, path):
        with self.api_scheduler:
            return self.session.delete(
                url=self._get_url(path),
                verify=self.REQUEST_SSL_VERIFY,
                timeout=self.REQUEST_TIMEOUT)

    # END   #### A set of methods encapsulating the vRA API queue
    def login(self):
        """
            Login to vRA
        """
        LOG.info("About to login to {} ...".format(self.base_url))
        r = self._post(
            path=self.LOGIN_API,
            json={"username": self.username,
                  "password": self.password, "domain": self.domain},
        )
        content = json.loads(r.content)
        self.loginToken = content.get("cspAuthToken")
        self.session.headers.update(
            {'Authorization': 'Bearer {}'.format(self.loginToken)})

        LOG.info("Obtained vRA token")

    def blueprintRequest(self, blueprintId, blueprintVersion, deploymentName,
                         inputs, project_id, reason="Blueprint request",
                         plan=False, simulate=False):
        """
        Request vRA Blueprint

        :param blueprintId: vRA Blueprint ID
        :param blueprintVersion: vRA Blueprint version
        :param deploymentName: Deployment name
        :param inputs: Inputs payload dictionary for the request
        :param project_id: vRA Project ID
        :param reason: Reason for request
        :param plan: Plan
        :param simulate: Simulate
        :return: HTTP Response content
        """
        LOG.info("About to request blueprint {} ...".format(blueprintId))

        jsonPayload = {
            "blueprintId": blueprintId,
            "blueprintVersion": blueprintVersion,
            "deploymentName": deploymentName,
            "inputs": inputs,
            "plan": plan,
            "projectId": project_id,
            "reason": reason,
            "simulate": simulate
        }
        LOG.info(json.dumps(jsonPayload))

        r = self._post(
            path=constants.BLUEPRINT_REQUESTS_API,
            json=jsonPayload
        )
        content = json.loads(r.content)

        LOG.info(r)
        LOG.info(content)


    def iaasMachineRequest(self, instance_template):
        """
        Build vRA virtual machine through IAAS API

        :param instance_template: Predefined template for instance HTTP payload
        :return:
        """
        LOG.info("Triggering vRA instance with properties: {}".
                                    format(instance_template))

        r = self._post(
            path=constants.MAHINES_API,
            json=instance_template
        )
        content = json.loads(r.content)

        LOG.info(r)
        LOG.info(content)
        resource_track_id = content['id']
        tracker = self.__track_status_waiter(resource_track_id,
                                             RESOURCE_TRACKER_SLEEP)
        if tracker['status'] == 'FAILED':
            LOG.error(tracker['message'])
            raise Exception(tracker['message'])

    def __track_status(self, request_id):
        """
        Track request status

        :param request_id: vRA Request ID
        :return: HTTP Response content
        """
        LOG.info("Track resource status ...")
        r = self._get(
            path=constants.RESOURCE_TRACKER_API + request_id
        )
        content = json.loads(r.content)
        LOG.debug("Resource tracker info: {}".format(content))
        return content

    def __track_status_waiter(self, id, sleep=5.0):
        """
        Waiter mechanism for pooling vRA request status

        :param id: vRA Request ID
        :param sleep: Pool interval seconds
        :return: vRA current request status
        """
        status = "INPROGRESS"
        curr_status = None
        while True:
            time.sleep(sleep)
            curr_status = self.__track_status(id)
            if curr_status['status'] != status:
                LOG.debug("Current tracker status: {}".format(curr_status))
                break

        return curr_status

    def getBlueprints(self):
        """
        Get all blueprints

        :return: HTTP Response content
        """
        LOG.info("Get Blueprints ...")
        r = self._get(
            path=constants.BLUEPRINTS_API
        )
        content = json.loads(r.content)
        return content["content"]

    def getBlueprint(self, name):
        """
        Get blueprint by name

        :param name: vRA Blueprint name
        :return: HTTP Response content
        """
        blueprints = self.getBlueprints()
        blueprint = filter(lambda x: x["name"] == name, blueprints)
        if len(blueprint) == 1:
            return blueprint[0]
        elif len(blueprint) > 1:
            LOG.error("Multiple blueprints available with name {}".format(name))
            return
        elif len(blueprint) == 0:
            LOG.error("Blueprint with name {} not found!".format(name))
            return

    def fetchVraProjects(self):
        """
        Fetch all available vRA projects

        :return: HTTP Response content
        """
        LOG.info("Fetching vRA Projects...")
        r = self._get(
            path=constants.PROJECTS_GET_API
        )
        content = json.loads(r.content)
        LOG.debug('vRA Projects content: {}'.format(content))
        return content["content"]

    def getVraDeployments(self, search_query=None):
        """
        Fetch vRA deployments

        :param search_query: Search query for match deployment
        :return: HTTP Response content
        """
        LOG.info("Fetching vRA Deployments...")

        path = constants.DEPLOYMENTS_GET_API
        if search_query:
            path = constants.DEPLOYMENTS_GET_API + "?search="+search_query

        r = self._get(
            path=path
        )
        content = json.loads(r.content)
        LOG.debug('vRA Deployments content: {}'.format(content))
        return content["content"]

    def getVraResourceByDeploymentId(self, deployment_id):
        """
        Fetch vRA resource by deployment ID

        :param deployment_id: Deployment ID
        :return: HTTP Response content
        """
        path = constants.DEPLOYMENT_RESOURCES_API.replace("{deployment_id}",
                                                          deployment_id)
        r = self._get(
            path=path
        )
        content = json.loads(r.content)
        LOG.debug('vRA Deployment Resources content: {}'.format(content))
        return content["content"]

    def snapshotRequest(self, deployment_id, resource_id, image_id):
        """
        Create instance snapshot

        :param deployment_id: vRA Deployment ID
        :param resource_id: vRA Resource IF
        :param image_id: Image ID in Openstack
        :return:
        """
        path = constants.DEPLOYMENT_RESOURCE_REQUESTS_API.replace("{deployment_id}",
                                                                  deployment_id)
        resource_path = path.replace("{resource_id}", resource_id)

        json_payload = {
            "actionId": "Cloud.vSphere.Machine.Snapshot.Create",
            "inputs": {
                "name": image_id
            }
        }

        r = self._post(
            path=resource_path,
            json=json_payload
        )
        content = json.loads(r.content)
        LOG.debug('vRA Snapshot create initialized: {}'.format(content))
        return content

    def getVraNetworks(self):
        """
        Fetch vRA available networks

        :return: HTTP Response content
        """

        r = self._get(
            path=constants.NETWORKS_API
        )
        content = json.loads(r.content)
        LOG.debug('vRA Networks content: {}'.format(content))
        return content["content"]

    def get_vra_machine(self, instance):
        """
        Get vRA machine by openstack instance uuid property

        :param instance: Openstack instance
        :return: HTTP Response content
        """
        r = self._get(
            path=constants.MAHINES_API + "?$filter=tags.item.key eq openstack_instance_id" +
                                         " and tags.item.value eq {}".format(instance.uuid)
        )
        content = json.loads(r.content)
        LOG.debug('vRA Machine content: {}'.format(content))
        return content["content"][0]

    def power_on_instance(self, instance_id):
        """
        Power On vRA instance

        :param instance_id: vRA instance ID
        :return:
        """
        url = constants.POWER_ON_API.replace("{id}", instance_id)
        r = self._post(
            path=url,
            json=""
        )
        content = json.loads(r.content)
        resource_track_id = content['id']
        tracker = self.__track_status_waiter(resource_track_id,
                                             RESOURCE_TRACKER_SLEEP)
        if tracker['status'] == 'FAILED':
            LOG.error(tracker['message'])
            raise Exception(tracker['message'])
        LOG.info('vRA Machine Power On initialized')

    def power_off_instance(self, instance_id):
        """
        Power Off vRA instance

        :param instance_id:
        :return:
        """
        url = constants.POWER_OFF_API.replace("{id}", instance_id)
        r = self._post(
            path=url,
            json=""
        )
        content = json.loads(r.content)
        resource_track_id = content['id']
        tracker = self.__track_status_waiter(resource_track_id,
                                             RESOURCE_TRACKER_SLEEP)
        if tracker['status'] == 'FAILED':
            LOG.error(tracker['message'])
            raise Exception(tracker['message'])
        LOG.info('vRA Machine Power Off initialized')

    def destroy(self, instance_id):
        """
        Destroy vRA instance

        :param instance_id: vRA instance id
        :return:
        """
        url = '{}{}'.format(constants.MACHINES_API, instance_id)
        r = self._delete(
            path=url
        )
        content = json.loads(r.content)
        resource_track_id = content['id']
        tracker = self.__track_status_waiter(resource_track_id,
                                             RESOURCE_TRACKER_SLEEP)
        if tracker['status'] == 'FAILED':
            LOG.error(tracker['message'])
            raise Exception(tracker['message'])
        LOG.info('vRA Machine destroy initialized')
