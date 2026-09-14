"""The same tool-calling agent from Part 2, now running on Foundry Agent Service's
Conversations-era Agent Framework stack instead of the Assistants-style
threads/runs API this part used before.

What actually changes moving from local to cloud:
  - The model is a Foundry project deployment name, not a local alias - no more
    download/load/unload lifecycle, the service manages that.
  - Identity: DefaultAzureCredential (Microsoft Entra) replaces "nothing", since
    there was no auth boundary to cross when everything ran locally.
  - Tools are still plain Python functions with type hints and docstrings -
    Agent Framework inspects them to build the same kind of JSON schema MCP tools
    expose, so the *shape* of "what a tool is" doesn't change.
  - New in this part: a Foundry Toolbox. `create_toolbox.py` registers Code
    Interpreter and Web Search behind one governed, versioned endpoint; this
    script's agent uses that toolbox exactly the way it uses its own Python
    functions, through `FoundryToolbox` (a thin MCP client wrapper), not through
    a separate integration per tool.
  - New in this part: server-managed conversation state. `agent.create_session()`
    stores nothing but an opaque ID locally (`session.service_session_id`); the
    actual message history lives in the Foundry project, addressed by that ID.
  - New in this part: declarative, versioned agent definitions. `to_prompt_agent`
    converts the same in-process `Agent` into a `PromptAgentDefinition` and
    `client.agents.create_version` publishes it - the definition this script runs
    against and the one Foundry can serve as a standalone hosted agent are the
    same artifact.

Required environment variables:
  FOUNDRY_PROJECT_ENDPOINT  e.g. https://<account>.services.ai.azure.com/api/projects/<project>
  FOUNDRY_MODEL             e.g. gpt-5-mini
  TOOLBOX_NAME              e.g. part03-tools (see create_toolbox.py)
"""

import asyncio
import os

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient, FoundryToolbox, to_prompt_agent
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

AGENT_NAME = "from-edge-to-enterprise-agent"


def get_weather(location: str, unit: str = "celsius") -> str:
    """Get the current weather for a location.

    Args:
        location: The city or location to look up.
        unit: Temperature unit, either "celsius" or "fahrenheit".
    """
    temperature = 18 if unit == "celsius" else 64
    return f'{{"location": "{location}", "temperature": {temperature}, "unit": "{unit}", "condition": "Partly cloudy"}}'


def calculate(expression: str) -> str:
    """Evaluate a simple arithmetic expression, e.g. "42 * 17".

    Args:
        expression: An arithmetic expression using +, -, *, /, parentheses and numbers.
    """
    allowed = set("0123456789+-*/(). ")
    if not all(c in allowed for c in expression):
        return '{"error": "Invalid expression"}'
    try:
        return f'{{"expression": "{expression}", "result": {eval(expression)}}}'  # noqa: S307
    except Exception as exc:  # noqa: BLE001 - surfaced back to the model as a tool result
        return f'{{"error": "{exc}"}}'


async def main() -> None:
    credential = DefaultAzureCredential()
    client = FoundryChatClient(credential=credential)

    # The toolbox is attached per-call, not baked into the agent: the published,
    # declarative definition below stays limited to this agent's own function
    # tools, which is what a versioned Foundry agent can currently express. A
    # toolbox reference is either a local MCP client (works today, as here) or a
    # server-executed hosted MCP tool via `client.get_mcp_tool(...)`, which needs
    # a Foundry connection resource for server-to-server auth - one more step
    # than this demo's scope covers.
    agent = Agent(
        client=client,
        name=AGENT_NAME,
        instructions=(
            "You are a helpful assistant with access to tools. "
            "Use them when needed to answer questions accurately."
        ),
        tools=[get_weather, calculate],
    )

    async with FoundryToolbox(credential) as toolbox:
        # Server-managed conversation: the app only ever holds this opaque ID.
        session = agent.create_session()

        question = "What's the weather in Tokyo, and what is 42 * 17?"
        print(f"[User]: {question}")
        result = await agent.run(question, session=session)
        print(f"[{AGENT_NAME}]: {result.text}")
        print(f"Server-side conversation ID: {session.service_session_id}")

        follow_up = "Use your web search tool to find one recent Microsoft Foundry announcement."
        print(f"[User]: {follow_up}")
        result2 = await agent.run(follow_up, session=session, tools=[toolbox])
        print(f"[{AGENT_NAME}]: {result2.text}")

    # Declarative, versioned agent lifecycle: publish the same function-tool
    # definition this script just ran, so Foundry can serve it directly (see
    # part03's README) - independent of the per-call toolbox above.
    definition = to_prompt_agent(agent)
    project_client = AIProjectClient(
        endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        credential=credential,
    )
    version = project_client.agents.create_version(
        agent_name=AGENT_NAME,
        definition=definition,
        description="Part 3 cloud migration demo agent.",
    )
    print(f"Published agent definition: {version.id} (version {version.version})")


if __name__ == "__main__":
    asyncio.run(main())
