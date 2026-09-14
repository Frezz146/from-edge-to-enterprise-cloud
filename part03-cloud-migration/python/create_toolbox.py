"""One-time control-plane step: register a Foundry Toolbox on the project.

There is no public Bicep/ARM resource type for a toolbox (see infra/main.bicep) -
it is created through this SDK call instead, which is why this is a script you
run once after `az deployment group create`, not part of the infrastructure.

Run again after changing the `tools` list below: toolbox versions are immutable,
so this creates a new version and points the toolbox's "default" alias at it.

Required environment variables:
  FOUNDRY_PROJECT_ENDPOINT  e.g. https://<account>.services.ai.azure.com/api/projects/<project>
  TOOLBOX_NAME              e.g. part03-tools
"""

import os

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import CodeInterpreterToolboxTool, WebSearchToolboxTool
from azure.identity import DefaultAzureCredential


def main() -> None:
    project_endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    toolbox_name = os.environ.get("TOOLBOX_NAME", "part03-tools")

    client = AIProjectClient(endpoint=project_endpoint, credential=DefaultAzureCredential())

    # Every tool in a toolbox needs a unique `name` - the service rejects a
    # version where more than one tool is missing an identifier.
    version = client.toolboxes.create_version(
        name=toolbox_name,
        tools=[
            CodeInterpreterToolboxTool(name="code_interpreter"),
            WebSearchToolboxTool(name="web_search"),
        ],
        description="Built-in tools for the Part 3 cloud migration demo agent.",
    )
    print(f"Toolbox '{version.name}' version {version.version} ready.")
    print(f"MCP endpoint: {project_endpoint.rstrip('/')}/toolboxes/{toolbox_name}/mcp?api-version=v1")


if __name__ == "__main__":
    main()
