import os
import copy
import shutil
import logging
import typing as t
from distutils import dir_util
from omegaconf import DictConfig
from mlcube.errors import ConfigurationError
from mlcube.config import (ParameterType, IOType)


__all__ = ['Shell']

logger = logging.getLogger(__name__)


class Shell(object):
    """ Helper functions to run commands. """

    @staticmethod
    def run(*cmd, die_on_error: bool = True) -> int:
        """Execute shell command.
        Args:
            cmd: Command to execute, e.g. Shell.run('ls', -lh'). This method will just join using whitespaces.
            die_on_error: If true and shell returns non-zero exit status, raise RuntimeError.
        Returns:
            Exit code.
        """
        cmd: t.Text = ' '.join(cmd)
        return_code: int = os.system(cmd)
        if return_code != 0 and die_on_error:
            logger.error("Command = '%s', return_code = %d, die_on_error = %r", cmd, return_code, die_on_error)
            raise RuntimeError("Command failed: {}".format(cmd))
        logger.info("Command = '%s', return_code = %d, die_on_error = %r", cmd, return_code, die_on_error)
        return return_code

    @staticmethod
    def docker_image_exists(docker: t.Optional[t.Text], image: t.Text) -> bool:
        """Check if docker image exists.
        Args:
            docker: Docker executable (docker/sudo docker/podman/nvidia-docker/...).
            image: Name of a docker image.
        Returns:
            True if image exists, else false.
        """
        docker = docker or 'docker'
        return Shell.run(f'{docker} inspect --type=image {image} > /dev/null 2>&1', die_on_error=False) == 0

    @staticmethod
    def ssh(connection_str: t.Text, command: t.Optional[t.Text]) -> None:
        if command:
            Shell.run('ssh', '-o', 'StrictHostKeyChecking=no', connection_str, f"'{command}'")

    @staticmethod
    def rsync_dirs(source: t.Text, dest: t.Text) -> None:
        Shell.run('rsync', '-e', "'ssh'", f"'{source}'", f"'{dest}'")

    @staticmethod
    def generate_mounts_and_args(mlcube: DictConfig, task: t.Text) -> t.Tuple[t.Dict, t.List]:
        """ Generate mount points and arguments for the give task.
        Return:
            A tuple containing two elements:
                -  A mapping from host path to path inside container.
                -  A list of task arguments.
        """
        # First task argument is always the task name.
        mounts, args = {}, [task]

        def _generate(_params: DictConfig, _io: t.Text) -> None:
            """ _params here is a dictionary containing input or output parameters.
            It maps parameter name to DictConfig(type, default)
            """
            if not IOType.is_valid(_io):
                raise ConfigurationError(f"Invalid IO = {_io}")
            for _param_name, _param_def in _params.items():
                if not ParameterType.is_valid(_param_def.type):
                    raise ConfigurationError(f"Invalid task: task={task}, param={_param_name}, "
                                             f"type={_param_def.type}. Type is invalid.")
                _host_path = os.path.join(mlcube.runtime.workspace, _param_def.default)

                if _param_def.type == ParameterType.UNKNOWN:
                    if _io == IOType.OUTPUT:
                        raise ConfigurationError(f"Invalid task: task={task}, param={_param_name}, "
                                                 f"type={_param_def.type}. Type is unknown.")
                    else:
                        if os.path.isdir(_host_path):
                            _param_def.type = ParameterType.DIRECTORY
                        elif os.path.isfile(_host_path):
                            _param_def.type = ParameterType.FILE
                        else:
                            raise ConfigurationError(f"Invalid task: task={task}, param={_param_name}, "
                                                     f"type={_param_def.type}. Type is unknown and unable to identify "
                                                     f"it ({_host_path}).")

                if _param_def.type == ParameterType.DIRECTORY:
                    os.makedirs(_host_path, exist_ok=True)
                    mounts[_host_path] = mounts.get(
                        _host_path,
                        '/mlcube_io{}/{}'.format(len(mounts), os.path.basename(_host_path))
                    )
                    args.append('--{}={}'.format(_param_name, mounts[_host_path]))
                elif _param_def.type == ParameterType.FILE:
                    _host_path, _file_name = os.path.split(_host_path)
                    os.makedirs(_host_path, exist_ok=True)
                    mounts[_host_path] = mounts.get(
                        _host_path,
                        '/mlcube_io{}/{}'.format(len(mounts), _host_path)
                    )
                    args.append('--{}={}'.format(_param_name, mounts[_host_path] + '/' + _file_name))

        params = mlcube.tasks[task].parameters
        _generate(params.inputs, IOType.INPUT)
        _generate(params.outputs, IOType.OUTPUT)

        return mounts, args

    @staticmethod
    def to_cli_args(args: t.Mapping[t.Text, t.Any], sep: t.Text = '=', parent_arg: t.Optional[t.Text] = None) -> t.Text:
        """ Convert dict to CLI arguments.
        Args:
            args: Dictionary with parameters.
            sep: Key-value separator. For build args and environment variables it's '=', for mount points it is ':'.
            parent_arg: If not None, a parent parameter name for each arg in args, e.g. --build-arg
        """
        parent_arg = '' if not parent_arg else parent_arg + ' '
        return ' '.join(f'{parent_arg}{k}{sep}{v}' for k, v in args.items())

    @staticmethod
    def sync_workspace(target_mlcube: DictConfig, task: t.Text) -> None:
        """
        Args:
            target_mlcube: MLCube configuration. Its name (target_) means that this configuration defines actual
                configuration where MLCube is supposed to be executed. If workspaces are different, source_mlcube will
                refer to the MLCube configuration with default (internal) workspace.
            task: Task name to be executed.
        """
        def _storage_not_supported(_uri: t.Text) -> t.Text:
            """ Helper function to guard against unsupported storage. """
            _uri = _uri.strip()
            if _uri.startswith('storage:'):
                raise NotImplementedError
            return _uri

        def _is_inside_workspace(_workspace: t.Text, _artifact: t.Text) -> bool:
            """ Check if artifact is inside this workspace. Workspace directory and artifact must exist. """
            return os.path.commonpath([_workspace]) == os.path.commonpath([_workspace, _artifact])

        def _is_ok(_parameter: t.Text, _kind: t.Text, _workspace: t.Text, _artifact: t.Text, _must_exist: bool) -> bool:
            """ Return true if this artifact needs to be synced. """
            if not _is_inside_workspace(_workspace, _artifact):
                logger.debug("[sync_workspace] task = %s, parameter = %s, artifact is not inside %s workspace "
                             "(workspace = %s, uri = %s)", task, _parameter, _kind, _workspace, _artifact)
                return False
            if _must_exist and not os.path.exists(_artifact):
                logger.debug("[sync_workspace] task = %s, parameter = %s, artifact does not exist in %s workspace "
                             "(workspace = %s, uri = %s)", task, _parameter, _kind, _workspace, _artifact)
                return False
            if not _must_exist and os.path.exists(_artifact):
                logger.debug("[sync_workspace] task = %s, parameter = %s, artifact exists in %s workspace "
                             "(workspace = %s, uri = %s)", task, _parameter, _kind, _workspace, _artifact)
                return False
            return True

        def _is_task_output(_target_artifact: t.Text, _input_parameter: t.Text) -> bool:
            """ Check of this artifact is an output of some task. """
            for _task_name, _task_def in target_mlcube.tasks.items():
                for _output_param_name, _output_param_def in _task_def.parameters.outputs.items():
                    _target_output_artifact: t.Text = os.path.join(
                        target_workspace, _storage_not_supported(_output_param_def.default)
                    )
                    # Can't really use `os.path.samefile` here since files may not exist.
                    # if os.path.samefile(_target_artifact, _target_output_artifact):
                    if _target_artifact == _target_output_artifact:
                        logger.debug("[sync_workspace] task = %s, parameter = %s is an output of task = %s, "
                                     "parameter = %s", task, _input_parameter, _task_name, _output_param_name)
                        return True
            return False

        # Check if actual workspace is not internal one (which is default workspace).
        target_workspace = os.path.abspath(_storage_not_supported(target_mlcube.runtime.workspace))
        os.makedirs(target_workspace, exist_ok=True)

        source_workspace = os.path.abspath(os.path.join(target_mlcube.runtime.root, 'workspace'))
        if not os.path.exists(source_workspace):
            logger.debug("[sync_workspace] source workspace (%s) does not exist, nothing to sync.", source_workspace)
            return
        if os.path.samefile(target_workspace, source_workspace):
            logger.debug("[sync_workspace] target workspace (%s) is the same as source workspace (%s).",
                         target_workspace, source_workspace)
            return

        if task not in target_mlcube.tasks:
            raise ValueError(f"Task does not exist: {task}")

        # Deep copy of the MLCube config with workspace set to internal workspace (we need this to resolve artifact
        # paths).
        source_mlcube: DictConfig = copy.deepcopy(target_mlcube)
        source_mlcube.runtime.workspace = source_workspace
        source_mlcube.workspace = source_workspace

        inputs: t.Mapping[t.Text, DictConfig] = target_mlcube.tasks[task].parameters.inputs
        for input_name, input_def in inputs.items():
            # TODO: add support for storage protocol. Idea is to be able to retrieve actual storage specs from
            #       system settings file. It should be possible to also specify paths within that storage (see
            #       https://en.wikipedia.org/wiki/Uniform_Resource_Identifier). For instance, the `storage:home/${name}`
            #       means that the `storage` section defines some storage labelled as `home`, and MLCube needs to use
            #       ${name} path within that storage.

            source_uri: t.Text = os.path.join(source_workspace, _storage_not_supported(input_def.default))
            if not _is_ok(input_name, 'source', source_workspace, source_uri, _must_exist=True):
                continue

            target_uri: t.Text = os.path.join(target_workspace, _storage_not_supported(input_def.default))
            if not _is_ok(input_name, 'target', target_workspace, target_uri, _must_exist=False):
                continue

            if _is_task_output(target_uri, input_name):
                continue

            if os.path.isfile(source_uri):
                os.makedirs(os.path.dirname(target_uri), exist_ok=True)
                shutil.copy(source_uri, target_uri)
            elif os.path.isdir(source_uri):
                dir_util.copy_tree(source_uri, target_uri)
            else:
                raise RuntimeError(f"Unknown artifact type (%s)", source_uri)
            logger.debug("[sync_workspace] task = %s, parameter = %s, source (%s) copied to target (%s).",
                         task, input_name, source_uri, target_uri)
