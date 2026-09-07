"""A minimal local MCP server exposing tools over stdio.

This stands in for "the external systems your agent needs to call" - in a
real project this would be a server wrapping your ticketing system, your
internal API, a database, etc. Anything that speaks MCP can plug into the
tool-calling loop in mcp_client.py without changing how the model is prompted.

get_weather / calculate are the quickstart pair used by mcp_client.py.
book_appointment_loose / book_appointment_strict are a matched pair used by
schema_hardening_eval.py: same conceptual task, two schemas of different
rigor, so the difference in how often a small model calls them correctly is
attributable to the schema, not the task.

Run standalone for a smoke test:
    python mcp_server.py
Normally it is spawned automatically by mcp_client.py / schema_hardening_eval.py
over stdio.
"""

from typing import Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

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


class Attendee(BaseModel):
    name: str = Field(description="Full name of the meeting attendee")
    email: str = Field(description="Attendee's email address")


@mcp.tool()
def book_appointment_strict(
    attendee: Attendee,
    topics: list[str] = Field(description="List of discussion topics, one per item"),
    priority: Literal["low", "medium", "high"] = Field(description="Meeting priority"),
    duration_minutes: int = Field(description="Meeting duration in minutes"),
) -> dict:
    """Book a meeting with a fully-specified, structured request.

    The nested attendee object, the array of topics, the priority enum and
    the integer duration are all validated by the MCP server before this
    function ever runs - a call with the wrong shape never reaches here,
    it comes back to the caller as a tool error instead.
    """
    return {
        "status": "booked",
        "attendee": attendee.model_dump(),
        "topics": topics,
        "priority": priority,
        "duration_minutes": duration_minutes,
    }


@mcp.tool()
def book_appointment_loose(
    attendee_name: str = Field(description="who the meeting is with"),
    attendee_email: str = Field(description="their contact"),
    topics: str = Field(description="what to discuss"),
    priority: str = Field(description="how urgent"),
    duration_minutes: str = Field(description="how long"),
) -> dict:
    """Book a meeting."""
    return {
        "status": "booked",
        "attendee_name": attendee_name,
        "attendee_email": attendee_email,
        "topics": topics,
        "priority": priority,
        "duration_minutes": duration_minutes,
    }


if __name__ == "__main__":
    mcp.run()
