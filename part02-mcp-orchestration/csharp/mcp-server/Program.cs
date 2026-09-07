// A minimal local MCP server exposing tools over stdio - the C# counterpart
// to ../../python/mcp_server.py. Deliberately independent of it: every
// language track in this repo runs standalone, so a C# client never needs
// Python installed (and vice versa). The two servers expose conceptually
// matching tools, not byte-identical JSON schemas - each uses the field
// naming convention idiomatic to its language (snake_case in Python via
// Pydantic, camelCase/PascalCase in C# via System.Text.Json's defaults).
//
// get_weather / calculate are the quickstart pair used by mcp-agent.
// book_appointment_loose / book_appointment_strict are a matched pair used
// by schema-hardening-eval: same conceptual task, two schemas of different
// rigor, so the difference in how often a small model calls them correctly
// is attributable to the schema, not the task.
//
// IMPORTANT: never write to stdout here except through the MCP server
// itself - stdout *is* the JSON-RPC channel for stdio transport, and any
// stray Console.WriteLine would corrupt the protocol stream.
//
// Run standalone for a smoke test:
//   dotnet run
// Normally it is spawned automatically by mcp-agent / schema-hardening-eval.

using System.ComponentModel;
using System.Data;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using ModelContextProtocol.Server;

var builder = Host.CreateApplicationBuilder(args);
builder.Services
    .AddMcpServer()
    .WithStdioServerTransport()
    .WithToolsFromAssembly();

await builder.Build().RunAsync();

[McpServerToolType]
public static class WeatherTools
{
    [McpServerTool(Name = "get_weather"), Description("Get the current weather for a location.")]
    public static object GetWeather(
        [Description("The city or location to look up.")] string location,
        [Description("Temperature unit, either \"celsius\" or \"fahrenheit\".")] string unit = "celsius")
    {
        // Simulated lookup - swap this for a real weather API call.
        var temperature = unit == "celsius" ? 18 : 64;
        return new { location, temperature, unit, condition = "Partly cloudy" };
    }
}

[McpServerToolType]
public static class CalculatorTools
{
    [McpServerTool(Name = "calculate"), Description("Evaluate a simple arithmetic expression, e.g. \"42 * 17\".")]
    public static object Calculate(
        [Description("An arithmetic expression using +, -, *, /, parentheses and numbers.")] string expression)
    {
        const string allowed = "0123456789+-*/(). ";
        if (expression.Any(c => !allowed.Contains(c)))
            return new { error = "Invalid expression" };

        try
        {
            var result = new DataTable().Compute(expression, null);
            return new { expression, result };
        }
        catch (Exception ex) // surfaced back to the model as a tool result
        {
            return new { error = ex.Message };
        }
    }
}

// `required` on both properties matters, not just documentation: a plain
// positional record (Attendee(string Name, string Email)) has no equivalent
// to Pydantic's "fields without a default are required" - System.Text.Json
// silently deserializes a JSON object missing "name" into Name = null
// instead of throwing, so a model that drops the field gets a *successful*
// booking with a blank name instead of the loud rejection the strict schema
// is supposed to guarantee (verified live: without `required`, this let a
// call with a nested attendee missing "name" through as IsError=false).
// `required` on an init-only property is respected by System.Text.Json's
// deserializer by default (since .NET 7) and produces exactly the missing-
// field error this tool's schema promises.
public record Attendee
{
    [Description("Full name of the meeting attendee")]
    public required string Name { get; init; }

    [Description("Attendee's email address")]
    public required string Email { get; init; }
}

public enum Priority { Low, Medium, High }

[McpServerToolType]
public static class BookingTools
{
    [McpServerTool(Name = "book_appointment_strict")]
    [Description(
        "Book a meeting with a fully-specified, structured request. The nested attendee " +
        "object, the array of topics, the priority enum and the integer duration are all " +
        "validated by the MCP server before this method ever runs - a call with the wrong " +
        "shape never reaches here, it comes back to the caller as a tool error instead.")]
    public static object BookAppointmentStrict(
        Attendee attendee,
        [Description("List of discussion topics, one per item")] string[] topics,
        [Description("Meeting priority")] Priority priority,
        [Description("Meeting duration in minutes")] int durationMinutes)
    {
        return new { status = "booked", attendee, topics, priority, durationMinutes };
    }

    [McpServerTool(Name = "book_appointment_loose"), Description("Book a meeting.")]
    public static object BookAppointmentLoose(
        [Description("who the meeting is with")] string attendeeName,
        [Description("their contact")] string attendeeEmail,
        [Description("what to discuss")] string topics,
        [Description("how urgent")] string priority,
        [Description("how long, in minutes")] int durationMinutes)
    {
        return new { status = "booked", attendeeName, attendeeEmail, topics, priority, durationMinutes };
    }
}
