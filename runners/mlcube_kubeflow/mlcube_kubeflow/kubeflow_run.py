import logging
import urllib3
import typing as t
import json
import kfp
import kfp.compiler as compiler
import kfp.dsl as dsl
from datetime import datetime
from omegaconf import (DictConfig, OmegaConf)
from mlcube.runner import (RunnerConfig, Runner)
from mlcube.validate import Validate

logger = logging.getLogger(__name__)


class Config(RunnerConfig):
    """ Helper class to manage `kubeflow` environment configuration."""

    DEFAULT = OmegaConf.create({
        'runner': 'kubeflow',
        'image': '${docker.image}',   # Use image name from docker configuration section.
        'pvc': '${name}', 
        'namespace': 'default',
        'pipeline_host': '', # eg: set http://127.0.0.1:8000/pipeline when port forwarded svc/ml-pipeline-ui to port 8000
    })

    @staticmethod
    def validate(mlcube: DictConfig) -> None:
        Validate(mlcube.runner, 'runner')\
            .check_unknown_keys(Config.DEFAULT.keys())\
            .check_values(['image', 'pvc', 'namespace', 'pipeline_host'], str, blanks=False)

  
class KubeflowRun(Runner):

    CONFIG = Config

    def __init__(self, mlcube: t.Union[DictConfig, t.Dict], task: t.Text) -> None:
        super().__init__(mlcube, task)

    def binding_to_volumes(self, params: DictConfig,                    # inputs
                        args: t.List[t.Text], volume_mounts: t.Dict):   # outputs
        pvc_name = self.mlcube.runner.pvc
        vol_mount_prefix = '/mnt/mlcube'
        for param_name, param_def in params.items():
            args.append(f"--{param_name}=" + vol_mount_prefix + pvc_name + "/" + param_def.default)
        volume_mounts[vol_mount_prefix + pvc_name]= dsl.PipelineVolume(pvc=pvc_name)

    def container_op(self, name, task):
        image: t.Text = self.mlcube.runner.image
        container_args: t.List[t.Text] = []
        container_volume_mounts: t.Dict = dict()
        container_args.append(name)
        
        params = task.parameters
        self.binding_to_volumes(params.inputs, container_args, container_volume_mounts)
        self.binding_to_volumes(params.outputs, container_args, container_volume_mounts)
        op = dsl.ContainerOp(
                name = name,
                image = self.mlcube.runner.image,
                arguments = container_args,
                pvolumes = container_volume_mounts
                )
        return op


    @dsl.pipeline(
        name='Mlcube Pipeline',
        description='Pipeline to run mlcubes'
        )
    def mlcube_pipeline(self):
        last_task = None
        for name, task in self.mlcube.tasks.items():
            current_task = self.container_op(name, task)
            if last_task is not None:
                current_task.after(last_task)
            last_task = current_task
        return current_task
 
    def create_kf_pipeline(self) -> t.Any:
        compiler.Compiler().compile(self.mlcube_pipeline, self.mlcube.name + '.tar.gz')
        client = kfp.Client(host=self.mlcube.runner.pipeline_host)
        mlcube_experiment = client.create_experiment(name=self.mlcube.name)
        timestamp = datetime.now().strftime("%d-%m-%y-%H-%M-%S")
        run = client.run_pipeline(mlcube_experiment.id, 'mlcube-pipeline-'+timestamp, pipeline_package_path=self.mlcube.name + '.tar.gz', params={})

    def configure(self) -> None:
        ...

    def run(self) -> None:
        """Run a cube"""
        try:
            logging.info("Configuring MLCube to run in Kubeflow Pipelines...")
            
            _ = self.create_kf_pipeline()
        except urllib3.exceptions.HTTPError:
            print(f"K8S runner failed to run MLCube. The actual error is printed below. "
                  "Your MLCube kubeflow configuration was:")
            print(OmegaConf.to_yaml(self.mlcube.runner, resolve=True))
            raise
