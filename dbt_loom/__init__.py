from dataclasses import dataclass
import os
import re
from pathlib import Path
from typing import Callable, Dict, Optional, Set, List

import yaml
from dbt.plugins.contracts import PluginArtifacts
from dbt.contracts.graph.node_args import ModelNodeArgs
from dbt.contracts.graph.nodes import ModelNode

from dbt.plugins.manager import dbt_hook, dbtPlugin
from dbt.plugins.manifest import PluginNodes, Manifest
from dbt.config.project import VarProvider

from dbt_loom.shims import is_invalid_private_ref, is_invalid_protected_ref

try:
    from dbt.artifacts.resources.types import NodeType
except ModuleNotFoundError:
    from dbt.node_types import NodeType  # type: ignore


from dbt_loom.config import dbtLoomConfig
from dbt_loom.logging import fire_event
from dbt_loom.manifests import ManifestLoader, ManifestNode

import importlib.metadata


@dataclass
class LoomModelNodeArgs(ModelNodeArgs):
    """A dbt-loom extension of ModelNodeArgs to preserve resource types across lineages."""

    resource_type: NodeType = NodeType.Model
    group: Optional[str] = None

    def __init__(self, **kwargs):
        super().__init__(
            **{
                key: value
                for key, value in kwargs.items()
                if key not in ("resource_type", "group")
            }
        )
        self.resource_type = kwargs.get("resource_type", NodeType.Model)
        self.group = kwargs.get("group")

    @property
    def unique_id(self) -> str:
        unique_id = f"{self.resource_type}.{self.package_name}.{self.name}"
        if self.version:
            unique_id = f"{unique_id}.v{self.version}"

        return unique_id


def identify_node_subgraph(manifest, node_type=ManifestNode) -> Dict[str, ManifestNode]:
    """
    Identify all nodes that should be selected from the manifest, and return ManifestNodes.
    """

    output = {}

    # We're going to temporarily allow all nodes here.
    for unique_id in manifest["nodes"].keys():
        if unique_id.split(".")[0] in (NodeType.Test.value, NodeType.Macro.value):
            continue

        node = manifest.get("nodes", {}).get(unique_id)

        if not node:
            continue

        if node.get("access") is None:
            node["access"] = node.get("config", {}).get("access", "protected")

        # Versions may be floats or strings. Standardize on strings for compatibility.
        for key in ("version", "latest_version"):
            if node.get(key):
                node[key] = str(node[key])

        if node_type == ModelNode and node.get("root_path"):
            node.pop("root_path") # unsure why this is necessary, but it is

        output[unique_id] = node_type(**(node))
    return output

def convert_model_nodes_to_model_node_args(
    selected_nodes: Dict[str, ManifestNode],
) -> Dict[str, LoomModelNodeArgs]:
    """Generate a dictionary of ModelNodeArgs based on a dictionary of ModelNodes"""
    return {
        unique_id: LoomModelNodeArgs(
            schema=node.schema_name,
            identifier=node.identifier,
            **(node.dump()),
        )
        for unique_id, node in selected_nodes.items()
        if node is not None
    }


@dataclass
class LoomRunnableConfig:
    """A shim class to allow is_invalid_*_ref functions to correctly handle access for loom-injected models."""

    restrict_access: bool = True
    vars: VarProvider = VarProvider(vars={})


class dbtLoom(dbtPlugin):
    """
    dbtLoom is a dbt plugin that loads manifest files, parses a DAG from the manifest,
    and injects public nodes from imported manifest.
    """

    def __init__(self, project_name: str):
        # Log the version of dbt-loom being initialized
        fire_event(
            msg=f'Initializing dbt-loom={importlib.metadata.version("dbt-loom")}'
        )

        configuration_path = Path(
            os.environ.get("DBT_LOOM_CONFIG", "dbt_loom.config.yml")
        )

        self._manifest_loader = ManifestLoader()
        self.manifests: Dict[str, Dict] = {}

        self.config: Optional[dbtLoomConfig] = self.read_config(configuration_path)
        self.models: Dict[str, LoomModelNodeArgs] = {}
        self.manifest_models: Dict[str, ModelNode] = {}

        self._patch_ref_protection()

        if not self.config or (self.config and not self.config.enable_telemetry):
            self._patch_plugin_telemetry()

        super().__init__(project_name)

    def _patch_ref_protection(self) -> None:
        """Patch out the ref protection functions for proper protections"""
        import dbt.contracts.graph.manifest

        fire_event(
            msg="dbt-loom: Patching ref protection methods to support dbt-loom dependencies."
        )

        dbt.contracts.graph.manifest.Manifest.is_invalid_protected_ref = (  # type: ignore
            self.dependency_wrapper(is_invalid_protected_ref)
        )
        dbt.contracts.graph.manifest.Manifest.is_invalid_private_ref = (  # type: ignore
            self.dependency_wrapper(is_invalid_private_ref)
        )

        dbt.parser.manifest.ManifestLoader.check_valid_group_config_node = (  # type: ignore
            self.group_validation_wrapper(
                dbt.parser.manifest.ManifestLoader.check_valid_group_config_node  # type: ignore
            )
        )

        dbt.contracts.graph.nodes.ModelNode.from_args = (  # type: ignore
            self.model_node_wrapper(dbt.contracts.graph.nodes.ModelNode.from_args)  # type: ignore
        )

    def _patch_plugin_telemetry(self) -> None:
        """Patch the plugin telemetry function to prevent tracking of dbt plugins."""
        import dbt.tracking

        dbt.tracking.track = self.tracking_wrapper(dbt.tracking.track)

    def tracking_wrapper(self, function) -> Callable:
        """Wrap the telemetry `track` function and return early if we're tracking plugin actions."""

        def outer_function(*args, **kwargs):
            """Check the context of the snowplow tracker message for references to loom. Return if present."""

            if any(
                [
                    self.__class__.__name__ in str(context_item.__dict__)
                    or "dbt-loom" in str(context_item.__dict__)
                    or "dbt_loom" in str(context_item.__dict__)
                    for context_item in kwargs.get("context", [])
                ]
            ):
                return

            return function(*args, **kwargs)

        return outer_function

    def model_node_wrapper(self, function) -> Callable:
        """Wrap the ModelNode.from_args function and inject extra properties from the LoomModelNodeArgs."""

        def outer_function(args: LoomModelNodeArgs) -> ModelNode:
            model = function(args)
            model.group = args.group
            return model

        return outer_function

    def group_validation_wrapper(self, function) -> Callable:
        """Wrap the check_valid_group_config_node function to inject upstream group names."""

        def outer_function(
            inner_self, groupable_node, valid_group_names: Set[str]
        ) -> bool:
            new_groups: Set[str] = {
                model.group for model in self.models.values() if model.group is not None
            }

            return function(
                inner_self, groupable_node, valid_group_names.union(new_groups)
            )

        return outer_function

    def dependency_wrapper(self, function) -> Callable:
        def outer_function(inner_self, node, target_model, dependencies) -> bool:
            if self.config is not None:
                for manifest_name in self.manifests.keys():
                    if manifest_name in dependencies:
                        continue

                    dependencies[manifest_name] = LoomRunnableConfig()

            return function(inner_self, node, target_model, dependencies)

        return outer_function

    def get_groups(self) -> Set[str]:
        """Get all groups defined in injected models."""

        return {
            model.group for model in self.models.values() if model.group is not None
        }

    def read_config(self, path: Path) -> Optional[dbtLoomConfig]:
        """Read the dbt-loom configuration file."""
        if not path.exists():
            return None

        with open(path) as file:
            config_content = file.read()

        config_content = self.replace_env_variables(config_content)

        return dbtLoomConfig(**yaml.load(config_content, yaml.SafeLoader))

    @staticmethod
    def replace_env_variables(config_str: str) -> str:
        """Replace environment variable placeholders in the configuration string."""
        pattern = r"\$(\w+)|\$\{([^}]+)\}"
        return re.sub(
            pattern,
            lambda match: os.environ.get(
                match.group(1) if match.group(1) is not None else match.group(2), ""
            ),
            config_str,
        )

    def initialize(self) -> None:
        """Initialize the plugin"""

        if self.models != {} or not self.config:
            return

        for manifest_reference in self.config.manifests:
            fire_event(
                msg=f"dbt-loom: Loading manifest for `{manifest_reference.name}`"
                f" from `{manifest_reference.type.value}`"
            )

            manifest = self._manifest_loader.load(manifest_reference)
            if manifest is None:
                continue

            # Find the official project name from the manifest metadata and use that as the manifests key.
            manifest_name = manifest.get("metadata", {}).get(
                "project_name", manifest_reference.name
            )
            self.manifests[manifest_name] = manifest

            selected_nodes = identify_node_subgraph(manifest)

            # Remove nodes from excluded packages.
            filtered_nodes = {
                key: value
                for key, value in selected_nodes.items()
                if value.package_name not in manifest_reference.excluded_packages
            }

            loom_nodes = convert_model_nodes_to_model_node_args(filtered_nodes)
            
            self.models.update(loom_nodes)

            # get complete metadata nodes for updating manifest metadata
            metadata_nodes = {
                key: value
                for key, value in identify_node_subgraph(manifest, node_type=ModelNode).items()
                if value.package_name not in manifest_reference.excluded_packages
            }
            self.manifest_models.update(metadata_nodes)


    @dbt_hook
    def get_nodes(self) -> PluginNodes:
        """
        Inject PluginNodes to dbt for injection into dbt's DAG.
        """
        fire_event(msg="dbt-loom: Injecting nodes")
        return PluginNodes(models=self.models)  # type: ignore

    @dbt_hook
    def get_manifest_artifacts(self, manifest: Manifest) -> PluginArtifacts:
        """
        Inject metadata into the manifest.
        Overwrite the manifest by rewriting it with the injected models from within dbt
        """
        for key, value in self.manifest_models.items():
            if key in manifest.nodes:
                if manifest.nodes.get(key).description == "":
                    manifest.nodes[key].description = value.description
                if manifest.nodes.get(key).tags == []:
                    manifest.nodes[key].tags = value.tags

        return {"target/manifest.json": manifest}

plugins = [dbtLoom]