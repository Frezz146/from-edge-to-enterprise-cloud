"""A minimal local MCP server exposing two tools over stdio.

This stands in for "the external systems your agent needs to call" - in a
real project this would be a server wrapping your ticketing system, your
internal API, a database, etc. Anything that speaks MCP can plug into the
tool-calling loop in mcp_client.py without changing how the model is prompted.

Run standalone for a smoke test:
    python mcp_server.py
Normally it is spawned automatically by mcp_client.py over stdio.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("edge-to-enterprise-tools")


@mcp.tool()
def get_weather(location: str, unit: str = "celsius") -> dict:
    """Get the current weather for a location.

    Args:
        location: The city or location to look up.
        unit: Temperature unit, either "celsius" or "fahrenheit".
    """
    # Simulated lookup - swap this for a real weather API call.
    temperature = 18 if unit == "celsius" else 64
    return {
        "location": location,
        "temperature": temperature,
        "unit": unit,
        "condition": "Partly cloudy",
    }


@mcp.tool()
def calculate(expression: str) -> dict:
    """Evaluate a simple arithmetic expression, e.g. "42 * 17".

    Args:
        expression: An arithmetic expression using +, -, *, /, parentheses and numbers.
    """
    allowed = set("0123456789+-*/(). ")
    if not all(c in allowed for c in expression):
        return {"error": "Invalid expression"}
    try:
        return {"expression": expression, "result": eval(expression)}
    except Exception as exc:  # noqa: BLE001 - surfaced back to the model as a tool result
        return {"error": str(exc)}


if __name__ == "__main__":
    mcp.run()
