"""The same tool-calling agent from Part 2, now hosted in Foundry Agent
Service instead of running in-process on developer hardware.

What actually changes moving from local to cloud:
  - The model is a Foundry project deployment name, not a local alias -
    no more download/load/unload lifecycle, the service manages that.
  - Identity: DefaultAzureCredential (Microsoft Entra) replaces "nothing",
    since there was no auth boundary to cross when everything ran locally.
  - Tools are still plain Python functions with type hints and docstrings -
    FunctionTool inspects them to build the same kind of JSON schema MCP
    tools expose, so the *shape* of "what a tool is" doesn't change.
  - The tool-calling loop itself disappears: `enable_auto_function_calls`
    lets the SDK poll the run and invoke your functions automatically,
    where Part 1/2 wrote that loop by hand against the native chat client.

Required environment variables:
  PROJECT_ENDPOINT       e.g. https://<account>.services.ai.azure.com/api/projects/<project>
  MODEL_DEPLOYMENT_NAME  e.g. gpt-4o-mini

Note: instead of local Python functions, Foundry Agent Service can also call
a remote MCP server directly via the built-in MCP tool (`azure.ai.agents.models.MCPTool`),
which would let this agent reuse mcp_server.py from Part 2 unchanged - provided
it is reachable over HTTP(S) rather than local stdio. See:
https://learn.microsoft.com/azure/foundry/agents/how-to/tools/model-context-protocol
"""

import json
import os

from azure.ai.projects import AIProjectClient
from azure.ai.agents.models import FunctionTool, ToolSet
from azure.identity import DefaultAzureCredential


def get_weather(location: str, unit: str = "celsius") -> str:
    """Get the current weather for a location.

    :param location: The city or location to look up.
    :param unit: Temperature unit, either "celsius" or "fahrenheit".
    :return: A JSON string with location, temperature, unit and condition.
    """
    temperature = 18 if unit == "celsius" else 64
    return json.dumps(
        {"location": location, "temperature": temperature, "unit": unit, "condition": "Partly cloudy"}
    )


def calculate(expression: str) -> str:
    """Evaluate a simple arithmetic expression, e.g. "42 * 17".

    :param expression: An arithmetic expression using +, -, *, /, parentheses and numbers.
    :return: A JSON string with the expression and its result, or an error.
    """
    allowed = set("0123456789+-*/(). ")
    if not all(c in allowed for c in expression):
        return json.dumps({"error": "Invalid expression"})
    try:
        return json.dumps({"expression": expression, "result": eval(expression)})
    except Exception as exc:  # noqa: BLE001 - surfaced back to the agent as a tool result
        return json.dumps({"error": str(exc)})


def main() -> None:
    project_endpoint = os.environ["PROJECT_ENDPOINT"]
    model_deployment = os.environ["MODEL_DEPLOYMENT_NAME"]

    project_client = AIProjectClient(
        endpoint=project_endpoint,
        credential=DefaultAzureCredential(),
    )

    with project_client:
        toolset = ToolSet()
        toolset.add(FunctionTool({get_weather, calculate}))
        project_client.agents.enable_auto_function_calls(toolset)

        agent = project_client.agents.create_agent(
            model=model_deployment,
            name="from-edge-to-enterprise-agent",
            instructions=(
                "You are a helpful assistant with access to tools. "
                "Use them when needed to answer questions accurately."
            ),
            toolset=toolset,
        )
        print(f"Created agent, ID: {agent.id}")

        thread = project_client.agents.threads.create()
        print(f"Created thread, ID: {thread.id}")

        question = "What's the weather in Tokyo, and what is 42 * 17?"
        project_client.agents.messages.create(thread_id=thread.id, role="user", content=question)
        print(f"[User]: {question}")

        run = project_client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)
        print(f"Run finished with status: {run.status}")
        if run.status == "failed":
            print(f"Run failed: {run.last_error}")

        for message in project_client.agents.messages.list(thread_id=thread.id):
            for content in message.content:
                if content.type == "text":
                    print(f"[{message.role}]: {content.text.value}")

        project_client.agents.delete_agent(agent.id)
        print("Cleaned up agent.")


if __name__ == "__main__":
    main()
