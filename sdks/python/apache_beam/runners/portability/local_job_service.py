#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# pytype: skip-file

import concurrent.futures
import itertools
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from typing import Any
from typing import List
from typing import Mapping
from typing import Optional

import grpc
from google.protobuf import json_format
from google.protobuf import struct_pb2
from google.protobuf import text_format

from apache_beam import pipeline
from apache_beam.metrics import monitoring_infos
from apache_beam.options import pipeline_options
from apache_beam.portability.api import beam_artifact_api_pb2_grpc
from apache_beam.portability.api import beam_fn_api_pb2_grpc
from apache_beam.portability.api import beam_job_api_pb2
from apache_beam.portability.api import beam_job_api_pb2_grpc
from apache_beam.portability.api import beam_provision_api_pb2
from apache_beam.portability.api import beam_runner_api_pb2
from apache_beam.portability.api import endpoints_pb2
from apache_beam.runners.job import utils as job_utils
from apache_beam.runners.portability import abstract_job_service
from apache_beam.runners.portability import artifact_service
from apache_beam.runners.portability import portable_runner
from apache_beam.runners.portability.fn_api_runner import fn_runner
from apache_beam.runners.portability.fn_api_runner import worker_handlers
from apache_beam.runners.worker.log_handler import LOGENTRY_TO_LOG_LEVEL_MAP
from apache_beam.transforms import environments
from apache_beam.utils import thread_pool_executor

_LOGGER = logging.getLogger(__name__)


def _iter_queue(q):
  while True:
    yield q.get(block=True)


class LocalJobServicer(abstract_job_service.AbstractJobServiceServicer):
  """Manages one or more pipelines, possibly concurrently.
    Experimental: No backward compatibility guaranteed.
    Servicer for the Beam Job API.

    This JobService uses a basic local implementation of runner to run the job.
    This JobService is not capable of managing job on remote clusters.

    By default, this JobService executes the job in process but still uses GRPC
    to communicate pipeline and worker state.  It can also be configured to use
    inline calls rather than GRPC (for speed) or launch completely separate
    subprocesses for the runner and worker(s).
    """
  def __init__(self, staging_dir=None, beam_job_type=None):
    super().__init__()
    self._cleanup_staging_dir = staging_dir is None
    self._staging_dir = staging_dir or tempfile.mkdtemp()
    self._artifact_service = artifact_service.ArtifactStagingService(
        artifact_service.BeamFilesystemHandler(self._staging_dir).file_writer)
    self._artifact_staging_endpoint: Optional[
        endpoints_pb2.ApiServiceDescriptor] = None
    self._beam_job_type = beam_job_type or BeamJob

  def create_beam_job(
      self,
      preparation_id,  # stype: str
      job_name: str,
      pipeline: beam_runner_api_pb2.Pipeline,
      options: struct_pb2.Struct) -> 'BeamJob':
    self._artifact_service.register_job(
        staging_token=preparation_id,
        dependency_sets=_extract_dependency_sets(
            pipeline.components.environments))
    provision_info = fn_runner.ExtendedProvisionInfo(
        beam_provision_api_pb2.ProvisionInfo(pipeline_options=options),
        self._staging_dir,
        job_name=job_name)
    return self._beam_job_type(
        preparation_id,
        pipeline,
        options,
        provision_info,
        self._artifact_staging_endpoint,
        self._artifact_service)

  def get_bind_address(self):
    """Return the address used to open the port on the gRPC server.

    This is often, but not always the same as the service address.  For
    example, to make the service accessible to external machines, override this
    to return '[::]' and override `get_service_address()` to return a publicly
    accessible host name.
    """
    return self.get_service_address()

  def get_service_address(self):
    """Return the host name at which this server will be accessible.

    In particular, this is provided to the client upon connection as the
    artifact staging endpoint.
    """
    return 'localhost'

  def start_grpc_server(self, port=0):
    options = [("grpc.max_receive_message_length", -1),
               ("grpc.max_send_message_length", -1),
               ("grpc.http2.max_pings_without_data", 0),
               ("grpc.http2.max_ping_strikes", 0)]
    self._server = grpc.server(
        thread_pool_executor.shared_unbounded_instance(), options=options)
    port = self._server.add_insecure_port(
        '%s:%d' % (self.get_bind_address(), port))
    beam_job_api_pb2_grpc.add_JobServiceServicer_to_server(self, self._server)
    beam_artifact_api_pb2_grpc.add_ArtifactStagingServiceServicer_to_server(
        self._artifact_service, self._server)
    hostname = self.get_service_address()
    self._artifact_staging_endpoint = endpoints_pb2.ApiServiceDescriptor(
        url='%s:%d' % (hostname, port))
    self._server.start()
    _LOGGER.info('Grpc server started at %s on port %d' % (hostname, port))
    return port

  def stop(self, timeout=1):
    self._server.stop(timeout)
    if os.path.exists(self._staging_dir) and self._cleanup_staging_dir:
      shutil.rmtree(self._staging_dir, ignore_errors=True)

  def GetJobMetrics(self, request, context=None):
    if request.job_id not in self._jobs:
      raise LookupError("Job {} does not exist".format(request.job_id))

    result = self._jobs[request.job_id].result
    if result is None:
      monitoring_info_list = []
    else:
      monitoring_info_list = result.monitoring_infos()

    # Filter out system metrics
    user_monitoring_info_list = [
        x for x in monitoring_info_list
        if monitoring_infos.is_user_monitoring_info(x)
    ]

    return beam_job_api_pb2.GetJobMetricsResponse(
        metrics=beam_job_api_pb2.MetricResults(
            committed=user_monitoring_info_list))


class SubprocessSdkWorker(object):
  """Manages a SDK worker implemented as a subprocess communicating over grpc.
  """
  def __init__(
      self,
      worker_command_line: bytes,
      control_address,
      provision_info,
      worker_id=None):
    # worker_command_line is of bytes type received from grpc. It was encoded in
    # apache_beam.transforms.environments.SubprocessSDKEnvironment earlier.
    # decode it back as subprocess.Popen does not support bytes args in win32.
    self._worker_command_line = worker_command_line.decode('utf-8')
    self._control_address = control_address
    self._provision_info = provision_info
    self._worker_id = worker_id

  def run(self):
    options = [("grpc.http2.max_pings_without_data", 0),
               ("grpc.http2.max_ping_strikes", 0)]
    logging_server = grpc.server(
        thread_pool_executor.shared_unbounded_instance(), options=options)
    logging_port = logging_server.add_insecure_port('[::]:0')
    logging_server.start()
    logging_servicer = BeamFnLoggingServicer()
    beam_fn_api_pb2_grpc.add_BeamFnLoggingServicer_to_server(
        logging_servicer, logging_server)
    logging_descriptor = text_format.MessageToString(
        endpoints_pb2.ApiServiceDescriptor(url='localhost:%s' % logging_port))

    control_descriptor = text_format.MessageToString(
        endpoints_pb2.ApiServiceDescriptor(url=self._control_address))
    pipeline_options = json_format.MessageToJson(
        self._provision_info.provision_info.pipeline_options)

    env_dict = dict(
        os.environ,
        CONTROL_API_SERVICE_DESCRIPTOR=control_descriptor,
        LOGGING_API_SERVICE_DESCRIPTOR=logging_descriptor,
        PIPELINE_OPTIONS=pipeline_options)
    # only add worker_id when it is set.
    if self._worker_id:
      env_dict['WORKER_ID'] = self._worker_id

    with worker_handlers.SUBPROCESS_LOCK:
      p = subprocess.Popen(self._worker_command_line, shell=True, env=env_dict)
    try:
      p.wait()
      if p.returncode:
        raise RuntimeError(
            'Worker subprocess exited with return code %s' % p.returncode)
    finally:
      if p.poll() is None:
        p.kill()
      logging_server.stop(0)


class BeamJob(abstract_job_service.AbstractBeamJob):
  """This class handles running and managing a single pipeline.

    The current state of the pipeline is available as self.state.
    """
  def __init__(
      self,
      job_id: str,
      pipeline,
      options,
      provision_info: fn_runner.ExtendedProvisionInfo,
      artifact_staging_endpoint: Optional[endpoints_pb2.ApiServiceDescriptor],
      artifact_service: artifact_service.ArtifactStagingService,
  ):
    super().__init__(job_id, provision_info.job_name, pipeline, options)
    self._provision_info = provision_info
    self._artifact_staging_endpoint = artifact_staging_endpoint
    self._artifact_service = artifact_service
    self._state_queues: List[queue.Queue] = []
    self._log_queues = JobLogQueues()
    self.daemon = True
    self.result = None

  def pipeline_options(self):
    def from_urn(key):
      assert key.startswith('beam:option:')
      assert key.endswith(':v1')
      return key[12:-3]

    return pipeline_options.PipelineOptions(
        **{
            from_urn(key): value
            for (key, value
                 ) in job_utils.struct_to_dict(self._pipeline_options).items()
        })

  def set_state(self, new_state):
    """Set the latest state as an int enum and notify consumers"""
    timestamp = super().set_state(new_state)
    if timestamp is not None:
      # Inform consumers of the new state.
      for queue in self._state_queues:
        queue.put((new_state, timestamp))

  def prepare(self):
    pass

  def artifact_staging_endpoint(self):
    return self._artifact_staging_endpoint

  def run(self):
    self.set_state(beam_job_api_pb2.JobState.STARTING)
    self._run_thread = threading.Thread(target=self._run_job)
    self._run_thread.start()

  def _run_job(self):
    with JobLogHandler(self._log_queues) as log_handler:
      self._update_dependencies()
      pipeline.Pipeline.merge_compatible_environments(self._pipeline_proto)
      try:
        start = time.time()
        self.result = self._invoke_runner()
        self.result.wait_until_finish()
        _LOGGER.info(
            'Completed job in %s seconds with state %s.',
            time.time() - start,
            self.result.state)
        self.set_state(
            portable_runner.PipelineResult.pipeline_state_to_runner_api_state(
                self.result.state))
      except:  # pylint: disable=bare-except
        self._log_queues.put(
            beam_job_api_pb2.JobMessage(
                message_id=log_handler._next_id(),
                time=time.strftime('%Y-%m-%d %H:%M:%S.'),
                importance=beam_job_api_pb2.JobMessage.JOB_MESSAGE_ERROR,
                message_text=traceback.format_exc()))
        _LOGGER.exception('Error running pipeline.')
        self.set_state(beam_job_api_pb2.JobState.FAILED)
        raise

  def _invoke_runner(self):
    self.set_state(beam_job_api_pb2.JobState.RUNNING)
    return fn_runner.FnApiRunner(
        provision_info=self._provision_info).run_via_runner_api(
            self._pipeline_proto, self.pipeline_options())

  def _update_dependencies(self):
    try:
      _update_dependency_sets(
          self._pipeline_proto.components.environments,
          self._artifact_service.resolved_deps(self._job_id, timeout=0))
      self._provision_info.provision_info.ClearField('retrieval_token')
    except concurrent.futures.TimeoutError:
      # TODO(https://github.com/apache/beam/issues/20267): Require this once
      # all SDKs support it.
      pass

  def cancel(self):
    if not self.is_terminal_state(self.state):
      self.set_state(beam_job_api_pb2.JobState.CANCELLING)
      # TODO(robertwb): Actually cancel...
      self.set_state(beam_job_api_pb2.JobState.CANCELLED)

  def get_state_stream(self):
    # Register for any new state changes.
    state_queue = queue.Queue()
    self._state_queues.append(state_queue)

    for state, timestamp in self.with_state_history(_iter_queue(state_queue)):
      yield state, timestamp
      if self.is_terminal_state(state):
        break

  def get_message_stream(self):
    # Register for any new messages.
    log_queue = queue.Queue()
    self._log_queues.append(log_queue)
    self._state_queues.append(log_queue)

    for msg in itertools.chain(self._log_queues.cache(),
                               self.with_state_history(_iter_queue(log_queue))):
      if isinstance(msg, tuple):
        assert len(msg) == 2 and isinstance(msg[0], int)
        current_state = msg[0]
        yield msg
        if self.is_terminal_state(current_state):
          break
      else:
        yield msg


class BeamFnLoggingServicer(beam_fn_api_pb2_grpc.BeamFnLoggingServicer):
  def Logging(self, log_bundles, context=None):
    for log_bundle in log_bundles:
      for log_entry in log_bundle.log_entries:
        _LOGGER.log(
            LOGENTRY_TO_LOG_LEVEL_MAP[log_entry.severity],
            'Worker: %s',
            str(log_entry).replace('\n', ' '))
    return iter([])


class JobLogQueues(object):
  def __init__(self):
    self._queues: List[queue.Queue] = []
    self._cache = []
    self._cache_size = 10
    self._lock = threading.Lock()

  def cache(self):
    with self._lock:
      return list(self._cache)

  def append(self, queue):
    with self._lock:
      self._queues.append(queue)

  def put(self, msg):
    with self._lock:
      if len(self._cache) < self._cache_size:
        self._cache.append(msg)
      else:
        min_level = min(m.importance for m in self._cache)
        if msg.importance >= min_level:
          self._cache.append(msg)
          for ix, m in enumerate(self._cache):
            if m.importance == min_level:
              del self._cache[ix]
              break

      for queue in self._queues:
        queue.put(msg)


class JobLogHandler(logging.Handler):
  """Captures logs to be returned via the Beam Job API.

    Enabled via the with statement."""

  # Mapping from logging levels to LogEntry levels.
  LOG_LEVEL_MAP = {
      logging.FATAL: beam_job_api_pb2.JobMessage.JOB_MESSAGE_ERROR,
      logging.CRITICAL: beam_job_api_pb2.JobMessage.JOB_MESSAGE_ERROR,
      logging.ERROR: beam_job_api_pb2.JobMessage.JOB_MESSAGE_ERROR,
      logging.WARNING: beam_job_api_pb2.JobMessage.JOB_MESSAGE_WARNING,
      logging.INFO: beam_job_api_pb2.JobMessage.JOB_MESSAGE_BASIC,
      logging.DEBUG: beam_job_api_pb2.JobMessage.JOB_MESSAGE_DEBUG,
  }

  def __init__(self, log_queues):
    super().__init__()
    self._last_id = 0
    self._logged_thread = None
    self._log_queues = log_queues

  def __enter__(self):
    # Remember the current thread to demultiplex the logs of concurrently
    # running pipelines (as Python log handlers are global).
    self._logged_thread = threading.current_thread()
    logging.getLogger().addHandler(self)
    return self

  def __exit__(self, *args):
    self._logged_thread = None
    self.close()

  def _next_id(self):
    self._last_id += 1
    return str(self._last_id)

  def emit(self, record):
    if self._logged_thread is threading.current_thread():
      msg = beam_job_api_pb2.JobMessage(
          message_id=self._next_id(),
          time=time.strftime(
              '%Y-%m-%d %H:%M:%S.', time.localtime(record.created)),
          importance=self.LOG_LEVEL_MAP[record.levelno],
          message_text=self.format(record))

      # Inform all message consumers.
      self._log_queues.put(msg)


def _extract_dependency_sets(
    envs: Mapping[str, beam_runner_api_pb2.Environment]
) -> Mapping[Any, List[beam_runner_api_pb2.ArtifactInformation]]:
  """Expands the set of environments into a mapping of (opaque) keys to
  dependency sets.  This is not 1:1 in the case of AnyOf environments.

  The values can then be resolved and the mapping passed back to
  _update_dependency_sets to update the dependencies in the original protos.
  """
  def dependencies_iter():
    for env_id, env in envs.items():
      for ix, sub_env in enumerate(environments.expand_anyof_environments(env)):
        yield (env_id, ix), sub_env.dependencies

  return dict(dependencies_iter())


def _update_dependency_sets(
    envs: Mapping[str, beam_runner_api_pb2.Environment],
    resolved_deps: Mapping[Any, List[beam_runner_api_pb2.ArtifactInformation]]):
  """Takes the mapping of beam Environments (originally passed to
  `_extract_dependency_sets`) and a set of (key-wise) updated dependencies,
  and updates the original environment protos to contain the updated
  dependencies.
  """
  for env_id, env in envs.items():
    new_envs = []
    for ix, sub_env in enumerate(environments.expand_anyof_environments(env)):
      # Slice assignment not supported for repeated fields.
      del sub_env.dependencies[:]
      sub_env.dependencies.extend(resolved_deps[env_id, ix])
      new_envs.append(sub_env)
    if len(new_envs) == 1:
      envs[env_id].CopyFrom(new_envs[0])
    else:
      envs[env_id].CopyFrom(
          environments.AnyOfEnvironment.create_proto(new_envs))
