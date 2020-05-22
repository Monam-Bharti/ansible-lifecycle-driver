import json
import logging
import time
import os
import tempfile
from datetime import datetime
from tempfile import NamedTemporaryFile
from collections import namedtuple
from ansible.parsing.dataloader import DataLoader
from ansible.vars.manager import VariableManager
from ansible.inventory.manager import InventoryManager
from ansible.executor.playbook_executor import PlaybookExecutor
from ansible.plugins.callback import CallbackBase
from ansible.plugins.callback.json import CallbackModule
from ansible.plugins.loader import connection_loader
from ansible.inventory.host import Host
from jinja2 import Environment, FileSystemLoader
from ignition.model.lifecycle import LifecycleExecution, STATUS_COMPLETE, STATUS_FAILED, STATUS_IN_PROGRESS
from ignition.model.failure import FailureDetails, FAILURE_CODE_INFRASTRUCTURE_ERROR, FAILURE_CODE_INTERNAL_ERROR, FAILURE_CODE_RESOURCE_NOT_FOUND
from ignition.service.config import ConfigurationPropertiesGroup
from ignition.service.framework import Service, Capability, interface
from ignition.utils.propvaluemap import PropValueMap
from ansibledriver.model.kubeconfig import KubeConfig

INVENTORY = "inventory"

logger = logging.getLogger(__name__)

class AnsibleProperties(ConfigurationPropertiesGroup):
    def __init__(self):
        super().__init__('ansible')
        # apply defaults (correct settings will be picked up from config file or environment variables)
        self.unreachable_sleep_seconds = 5 # in seconds
        self.max_unreachable_retries = 1000
        self.output_prop_prefix = 'output__'
        self.tmp_dir = '.'


class AnsibleClientCapability(Capability):

    @interface
    def run_lifecycle_playbook(self, request):
      pass


class AnsibleClient(Service, AnsibleClientCapability):
  def __init__(self, configuration, **kwargs):
    self.ansible_properties = configuration.property_groups.get_property_group(AnsibleProperties)
    if 'render_context_service' not in kwargs:
      raise ValueError('render_context_service argument not provided')
    self.render_context_service = kwargs.get('render_context_service')
    if 'templating' not in kwargs:
      raise ValueError('templating argument not provided')
    self.templating = kwargs.get('templating')

  # create a kubeconfig file based on the deployment location that can be consumed by the Python Kubernetes library
  def create_kube_config(self, deployment_location):
    return KubeConfig(deployment_location, self.ansible_properties).write()

  def run_playbook(self, request_id, connection_type, inventory_path, playbook_path, lifecycle, all_properties):
    Options = namedtuple('Options', ['connection',
                                     'forks',
                                     'become',
                                     'become_method',
                                     'become_user',
                                     'listhosts',
                                     'listtasks',
                                     'listtags',
                                     'syntax',
                                     'module_path',
                                     'check',
                                     'diff'])
    # initialize needed objects
    loader = DataLoader()
    options = Options(connection=connection_type,
                      listhosts=None,
                      listtasks=None,
                      listtags=None,
                      syntax=None,
                      module_path=None,
                      become=None,
                      become_method='sudo',
                      become_user='root',
                      check=False,
                      diff=False,
                      forks=20)
    passwords = {'become_pass': ''}

    # create inventory and pass to var manager
    inventory = InventoryManager(loader=loader, sources=inventory_path)
    variable_manager = VariableManager(loader=loader, inventory=inventory)
    variable_manager.extra_vars = all_properties
    # Setup playbook executor, but don't run until run() called
    pbex = PlaybookExecutor(
        playbooks=[playbook_path],
        inventory=inventory,
        variable_manager=variable_manager,
        loader=loader,
        options=options,
        passwords=passwords
    )

    callback = ResultCallback(self.ansible_properties, request_id, lifecycle)
    pbex._tqm._stdout_callback = callback

    logger.debug("Running playbook {0} with properties {1}, system_properties {2}".format(playbook_path, all_properties['properties'].get_props(), all_properties['system_properties'].get_props()))
    pbex.run()
    logger.debug("Playbook finished {0}".format(playbook_path))

    return callback

  def run_find_playbook(self, request):
    try:
      driver_files = request['driver_files']
      instance_name = request['instance_name']
      deployment_location = request['deployment_location']
      if not isinstance(deployment_location, dict):
        return LifecycleExecution(request_id, STATUS_FAILED, FailureDetails(FAILURE_CODE_INTERNAL_ERROR, "Deployment Location must be an object"), {})
      dl_properties = PropValueMap(deployment_location.get('properties', {}))

      infrastructure_type = deployment_location.get('type', None)
      if infrastructure_type is None:
        return ValueError("Deployment Location type must be set")

      config_path = driver_files.get_directory_tree('config')
      scripts_path = driver_files.get_directory_tree('scripts')

      all_properties = {
        'instance_name': instance_name,
        'dl_properties': dl_properties
      }

      process_templates(config_path, all_properties)

      playbook_path = get_lifecycle_playbook_path(scripts_path, 'Find')
      if playbook_path is not None:
        if not os.path.exists(playbook_path):
          raise ValueError('Find playbook not found')

        inventory_path = self.get_inventory(self, driver_files, infrastructure_type)

        # always retry on unreachable
        num_retries = self.ansible_properties.max_unreachable_retries

        for i in range(0, num_retries):
          if i>0:
            logger.debug('Playbook {0}, unreachable retry attempt {1}/{2}'.format(playbook_path, i+1, num_retries))
          start_time = datetime.now()
          ret = self.run_playbook(request_id, connection_type, inventory_path, playbook_path, lifecycle, all_properties)
          if not ret.host_unreachable:
            break
          end_time = datetime.now()
          if self.ansible_properties.unreachable_sleep_seconds > 0:
            # Factor in that the playbook may have taken some time to determine is was unreachable
            # by using the unreachable_sleep_seconds value as a minimum amount of time for the delay 
            delta = end_time - start_time
            retry_seconds = max(0, self.ansible_properties.unreachable_sleep_seconds-int(delta.total_seconds()))
            time.sleep(retry_seconds)

        return ret.get_find_result()
      else:
        raise ValueError('Find playbook not found')
    except InvalidRequestException as ire:
      return ValueError(f'Unexpected exception executing find playbook {ire.msg}')
    except Exception as e:
      logger.exception(f'Unexpected exception running playbook {e.msg}')
      return ValueError(f'Unexpected exception executing find playbook {e.msg}')
    finally:
      if key_property_processor is not None:
        key_property_processor.clear_key_files()

      keep_files = request.get('keep_files', False)
      if not keep_files and driver_files is not None:
        try:
          logger.debug('Attempting to remove lifecycle scripts at {0}'.format(driver_files.root_path))
          driver_files.remove_all()
        except Exception as e:
          logger.exception('Encountered an error whilst trying to clear out lifecycle scripts directory {0}: {1}'.format(driver_files.root_path, str(e)))

  def run_lifecycle_playbook(self, request):
    driver_files = request['driver_files']
    key_property_processor = None
    kube_location = None

    try:
      request_id = request['request_id']
      lifecycle = request['lifecycle_name']
      resource_properties = request['resource_properties']
      system_properties = request['system_properties']
      request_properties = request['request_properties']
      deployment_location = request['deployment_location']
      associated_topology = request['associated_topology']
      if not isinstance(deployment_location, dict):
        return LifecycleExecution(request_id, STATUS_FAILED, FailureDetails(FAILURE_CODE_INTERNAL_ERROR, "Deployment Location must be an object"), {})
      infrastructure_type = deployment_location.get('type', None)
      if infrastructure_type is None:
        return LifecycleExecution(request_id, STATUS_FAILED, FailureDetails(FAILURE_CODE_INTERNAL_ERROR, "Deployment Location type must be set"), {})
      dl_properties = PropValueMap(deployment_location.get('properties', {}))
      connection_type = dl_properties.get('connection_type')
      if connection_type is None:
        connection_type = 'ssh'

      config_path = driver_files.get_directory_tree('config')
      scripts_path = driver_files.get_directory_tree('scripts')

      key_property_processor = KeyPropertyProcessor(resource_properties, system_properties, dl_properties)

      playbook_path = get_lifecycle_playbook_path(scripts_path, lifecycle)
      if playbook_path is not None:
        if not os.path.exists(playbook_path):
          return LifecycleExecution(request_id, STATUS_FAILED, FailureDetails(FAILURE_CODE_INTERNAL_ERROR, "Playbook path does not exist"), {})

        inventory_path = self.get_inventory(self, driver_files, infrastructure_type)

        if connection_type == 'k8s':
          kube_location = KubeDeploymentLocation.from_dict(deployment_location)
          dl_properties['kubeconfig_path'] = kube_location.write_config_file()

        # process key properties by writing them out to a temporary file and adding an
        # entry to the property dictionary that maps the "[key_name].path" to the key file path
        key_property_processor.process_key_properties()

        logger.debug('config_path = ' + config_path.get_path())
        logger.debug('driver_files = ' + scripts_path.get_path())
        logger.debug("playbook_path=" + playbook_path)
        logger.debug("inventory_path=" + inventory_path)

        all_properties = self.render_context_service.build(system_properties, resource_properties, request_properties, kube_location.to_dict())

        process_templates(config_path, self.templating, all_properties)

        # always retry on unreachable
        num_retries = self.ansible_properties.max_unreachable_retries

        for i in range(0, num_retries):
          if i>0:
            logger.debug('Playbook {0}, unreachable retry attempt {1}/{2}'.format(playbook_path, i+1, num_retries))
          start_time = datetime.now()
          ret = self.run_playbook(request_id, connection_type, inventory_path, playbook_path, lifecycle, all_properties)
          if not ret.host_unreachable:
            break
          end_time = datetime.now()
          if self.ansible_properties.unreachable_sleep_seconds > 0:
            # Factor in that the playbook may have taken some time to determine is was unreachable
            # by using the unreachable_sleep_seconds value as a minimum amount of time for the delay 
            delta = end_time - start_time
            retry_seconds = max(0, self.ansible_properties.unreachable_sleep_seconds-int(delta.total_seconds()))
            time.sleep(retry_seconds)

        return ret.get_result()
      else:
        msg = "No playbook to run at {0} for lifecycle {1} for request {2}".format(playbook_path, lifecycle, request_id)
        logger.debug(msg)
        return LifecycleExecution(request_id, STATUS_FAILED, FailureDetails(FAILURE_CODE_INTERNAL_ERROR, msg), {})
    except InvalidRequestException as ire:
      return LifecycleExecution(request_id, STATUS_FAILED, FailureDetails(FAILURE_CODE_INTERNAL_ERROR, ire.msg), {})
    except Exception as e:
      logger.exception("Unexpected exception running playbook")
      return LifecycleExecution(request_id, STATUS_FAILED, FailureDetails(FAILURE_CODE_INTERNAL_ERROR, "Unexpected exception: {0}".format(e)), {})
    finally:
      try:
          if kube_location is not None:
            logger.debug(f'Attempting to clean up deployment location related files')
            kube_location.clear_config_files()
      except Exception as e:
          logger.exception(f'Encountered an error whilst trying to clean up deployment location related files: {e}')

      if key_property_processor is not None:
        key_property_processor.clear_key_files()

      keep_files = request.get('keep_files', False)
      if not keep_files and driver_files is not None:
        try:
          logger.debug('Attempting to remove lifecycle scripts at {0}'.format(driver_files.root_path))
          driver_files.remove_all()
        except Exception as e:
          logger.exception('Encountered an error whilst trying to clear out lifecycle scripts directory {0}: {1}'.format(driver_files.root_path, str(e)))

    def get_inventory(self, driver_files, infrastructure_type):
      config_path = driver_files.get_directory_tree('config')
      inventory_path = config_path.get_file_path(f'{INVENTORY}.{infrastructure_type}')
      if not os.path.exists(inventory_path):
        if infrastructure_type == 'Kubernetes':
          # try alternative path (backwards compatibility)
          inventory_path = config_path.get_file_path(f'{INVENTORY}.k8s')
        if not os.path.exists(inventory_path):
          # default to 'INVENTORY'
          inventory_path = config_path.get_file_path(f'{INVENTORY}')

      if not os.path.exists(inventory_path):
        # create temporary inventory file
        with open(inventory_path, "w") as inventory_file:
          inventory_file = NamedTemporaryFile(delete=False)
          inventory_file.write(b'[run_hosts]\n')
          inventory_file.write(b'localhost ansible_connection=local ansible_python_interpreter="/usr/bin/env python3" host_key_checking=False')
          inventory_file.write(private_key_value)
          inventory_file.close()

      return inventory_path


class ResultCallback(CallbackBase):
    """A sample callback plugin used for performing an action as results come in

    If you want to collect all results into a single object for processing at
    the end of the execution, look into utilizing the ``json`` callback plugin
    or writing your own custom callback plugin
    """
    def __init__(self, ansible_properties, request_id, lifecycle, display=None):
        super(ResultCallback, self).__init__(display)
        self.ansible_properties = ansible_properties
        self.request_id = request_id
        self.facts = {}
        self.results = []
        self.lifecycle = lifecycle

        self.playbook_failed = False

        self.host_unreachable = False
        self.host_failed = False
        self.host_unreachable_log = []
        self.host_failed_log = []

        self.resource_id = None
        self.properties = {}
        self.internal_properties = {}
        self.instance_id = None
        self.associated_topology = {}
        self.failure_code = ''
        self.failure_reason = ''

    def _new_play(self, play):
        return {
            'play': {
                'name': play.name,
                'id': str(play._uuid)
            },
            'tasks': []
        }

    def _new_task(self, task):
        return {
            'task': {
                'name': task.name,
                'id': str(task._uuid)
            },
            'hosts': {}
        }

    def v2_playbook_on_play_start(self, play):
        logger.debug('v2_playbook_on_play_start ok {0}'.format(play))
        self.results.append(self._new_play(play))

    def v2_playbook_on_task_start(self, task, is_conditional):
        logger.debug('v2_playbook_on_task_start ok {0} {1}'.format(task, is_conditional))

    def v2_playbook_on_handler_task_start(self, task):
        logger.debug('v2_playbook_on_handler_task_start ok {0}'.format(task))

    def v2_playbook_on_stats(self, stats):
        """Display info about playbook statistics"""

        hosts = sorted(stats.processed.keys())

        summary = {}
        for h in hosts:
            s = stats.summarize(h)
            summary[h] = s

        output = {
            'plays': self.results,
            'stats': summary
        }

        logger.debug('v2_playbook_on_stats {0}'.format(json.dumps(output, indent=4, sort_keys=True)))

    def v2_playbook_on_no_hosts_matched(self):
        logger.debug('v2_playbook_on_no_hosts_matched')

    def v2_runner_on_unreachable(self, result, ignore_errors=False):
        """
        ansible task failed as host was unreachable
        """
        logger.debug('v2_runner_on_unreachable {0}'.format(result))
        self.__handle_unreachable(result)
        logger.error('task: \'' + self.failed_task + '\' UNREACHABLE: ' + ' ansible playbook task ' + self.failed_task + ' host unreachable: ' + str(self.host_unreachable_log))

    def v2_playbook_on_vars_prompt(self, varname, private=True, prompt=None, encrypt=None, confirm=False, salt_size=None, salt=None, default=None, unsafe=None):
        logger.debug('v2_playbook_on_vars_prompt {0}'.format(varname))

    def v2_runner_item_on_ok(self, result):
        logger.debug('v2_runner_item_on_ok {0}'.format(result))

    def v2_runner_item_on_failed(self, result):
        logger.debug('v2_runner_item_on_failed {0}'.format(result))

    def v2_runner_item_on_skipped(self, result):
        logger.debug('v2_runner_item_on_skipped {0}'.format(result))

    def runner_on_no_hosts(self):
        logger.debug('runner_on_no_hosts')

    def v2_runner_retry(self, result):
        logger.debug('v2_runner_retry {0}'.format(result))

    def v2_runner_on_start(self, host, task):
        logger.debug('v2_runner_on_start {0} {1}'.format(host, task))

    def runner_on_failed(self, host, res, ignore_errors=False):
        logger.debug('runner_on_failed {0} {1}'.format(host, res))

    def __handle_unreachable(self, result):
        # TODO do not overwrite if already set
        self.failed_task = result._task.get_name()
        self.host_unreachable_log.append(dict(task=self.failed_task, result=result._result))
        self.host_unreachable = True
        self.failure_reason = 'Resource unreachable (task ' + str(self.failed_task) + ' failed: ' + str(result._result) + ')'
        self.failure_details = FailureDetails(FAILURE_CODE_RESOURCE_NOT_FOUND, self.failure_reason)
        self.playbook_failed = True

    def v2_runner_on_failed(self, result, *args, **kwargs):
        """
        ansible task failed
        """
        logger.debug("v2_runner_on_failed {0} {1} {2}".format(result._task, result._result, result._task_fields))
        self.failed_task = result._task.get_name()
        if 'msg' in result._result and 'Timeout' in result._result['msg'] and 'waiting for privilege escalation prompt' in result._result['msg']:
            logger.debug('Failure to be treated as unreachable:  task ' + str(self.failed_task) + ' failed: ' + str(result._result))
            self.__handle_unreachable(result)
        elif 'module_stderr' in result._result and result._result['module_stderr'].startswith('ssh:') and 'Host is unreachable' in result._result['module_stderr']:
            logger.debug('Failure to be treated as unreachable: task ' + str(self.failed_task) + ' failed: ' + str(result._result))
            self.__handle_unreachable(result)
        else:
          self.host_failed = True
          self.failure_reason = 'task ' + str(self.failed_task) + ' failed: ' + str(result._result)
          self.host_failed_log.append(dict(task=self.failed_task, result=result._result))
          self.failure_details = FailureDetails(FAILURE_CODE_INFRASTRUCTURE_ERROR, self.failure_reason)
          self.playbook_failed = True

    def v2_runner_on_skipped(self, result):
        logger.debug('v2_runner_on_skipped {0}'.format(result))

    def runner_on_ok(self, host, res):
        self._display.display('runner_on_ok {0} {1}'.format(host, res))
        logger.debug('runner_on_ok {0} {1}'.format(host, res))

    def v2_runner_on_ok(self, result, *args, **kwargs):
        """Print a json representation of the result

        This method could store the result in an instance attribute for retrieval later
        """
        logger.debug('v2_runner_on_ok {0}'.format(result))

        if 'results' in result._result.keys():
            self.facts = result._result['results']
        else:
            self.facts = result._result

        if 'ansible_facts' in self.facts:
            props = self.facts['ansible_facts']

            output_props = { key[8:]:value for key, value in props.items() if key.startswith(self.ansible_properties.output_prop_prefix) }
            logger.debug(f'output props = {output_props}')
            self.properties.update(output_props)

            instances = { key[10:]:value for key, value in props.items() if key.startswith('instance__') }
            instance_id = None
            if len(instances) > 0:
              for key, value in self.instances.items():
                instance_id = key
                break
            logger.debug(f'instance_id = {instance_id}')
            self.instance_id = instance_id

            associated_topology = { key[21:]:value for key, value in props.items() if key.startswith('associated_topology__') }
            logger.debug(f'associated_topology = {associated_topology}')
            self.associated_topology.update(associated_topology)

    def get_find_result(self):
      if self.playbook_failed:
        return ValueError("Find failed")
      else:
        if len(self.instances) > 0:
          return FindReferenceResult(self.instance_id, self.associated_topology, self.outputs)
        else:
          # TODO

    def get_result(self):
      if self.playbook_failed:
        return LifecycleExecution(self.request_id, STATUS_FAILED, self.failure_details, self.properties)
      else:
        return LifecycleExecution(self.request_id, STATUS_COMPLETE, None, self.properties)


class InvalidRequestException(Exception):
  """Raised when a REST request is invalid
     Attributes:
       msg - failure message
  """

  def __init__(self, msg):
    self.msg = msg

def get_lifecycle_playbook_path(root_path, transition_name):
    try:
      return root_path.get_file_path(transition_name + ".yaml")
    except ValueError as e:
      # no playbook
      try:
        return root_path.get_file_path(transition_name + ".yml")
      except ValueError as e:
        # no playbook
        return None

def process_templates(parent_dir, templating, all_properties):
  path = parent_dir.get_path()
  logger.debug('Process templates: walking {0}'.format(path))

  for root, dirs, files in os.walk(path):
    logger.debug('Process templates: files = '.format(files))
    for file in files:
        j2_env = Environment(loader=FileSystemLoader(root), trim_blocks=True)
        path = root + '/' + file
        logger.info('PROCESSING ' + str(file) + ' WITH ' + str(all_properties))

        with open(path, "r") as template_file:
          template_content = template_file.read()
          content = templating.render(template_content, all_properties)
          logger.debug('Wrote process template to file {0}'.format(path))
          with open(path, "w") as template_file_write:
              template_file_write.write(content)




class KeyPropertyProcessor():
  def __init__(self, properties, system_properties, dl_properties):
    self.properties = properties
    self.system_properties = system_properties
    self.dl_properties = dl_properties
    self.key_files = []

  """
  Process (input) key properties by writing the private key out to a file so that it can be
  referenced in e.g. inventory files.
  """
  def process_key_properties(self):
    self.process_keys(self.properties)
    self.process_keys(self.system_properties)
    self.process_keys(self.dl_properties)

  def process_keys(self, properties):
    for prop in properties.get_keys().items_with_types():
      self.write_private_key(properties, prop[0], prop[1])

  def write_private_key(self, properties, key_prop_name, private_key):
    with NamedTemporaryFile(delete=False, mode='w') as private_key_file:
      logger.debug('Writing private key file {0}'.format(private_key_file.name))
      private_key_value = private_key.get('privateKey', None)
      private_key_file.write(private_key_value)
      private_key_file.flush()
      self.key_files.append(private_key_file)

      logger.debug('Setting property {0}_path'.format(key_prop_name))
      properties[key_prop_name + '_path'] = private_key_file.name

      logger.debug('Setting property {0}_name'.format(key_prop_name))
      key_name = private_key.get('keyName', None)
      properties[key_prop_name + '_name'] = key_name

  """
  Remove any private key files generated during the Ansible run.
  """
  def clear_key_files(self):
    for key_file in self.key_files:
      logger.debug('Removing private key file {0}'.format(key_file.name))
      os.unlink(key_file.name)