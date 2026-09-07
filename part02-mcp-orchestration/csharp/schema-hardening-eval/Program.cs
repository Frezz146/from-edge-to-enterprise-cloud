// Schema hardening study: does tightening a tool's JSON schema actually make
// a small local model call it more reliably? Conceptual counterpart to
// ../../python/schema_hardening_eval.py - same cases, same idea, same error
// taxonomy - but built against the independent C# server in ../mcp-server,
// not the Python one. The two tracks measure the same question with their
// own idiomatic tool schemas (snake_case field names in Python, camelCase
// in C#), not byte-identical requests, since neither language track depends
// on the other to run.
//
// ../mcp-server exposes the same conceptual task - "book a meeting" -
// through two tools with different schema rigor:
//   - book_appointment_loose: five flat, untyped string fields, vague
//     one-line descriptions. Nothing stops the model from writing "urgent"
//     instead of "high", or "30 minutes" instead of 30.
//   - book_appointment_strict: a nested attendee object, an array of
//     topics, a priority enum and an integer duration. The MCP server
//     rejects a call that doesn't match this shape before it ever runs.
//
// For each of three prompts (of increasing phrasing difficulty), this
// classifies every tool-call attempt into: NO_TOOL_CALL, WRONG_TOOL,
// MALFORMED_JSON, SERVER_REJECTED (schema violation caught by the MCP
// server itself), WRONG_NAME/WRONG_EMAIL/WRONG_PRIORITY/WRONG_DURATION/
// MISSING_TOPIC (a value that doesn't match the prompt - what the loose
// schema lets slip through silently), or OK.
//
// This is local-model-only - see the Python script for the optional
// --compare-cloud path and the chart export.

using System.Text.Json;
using System.Text.RegularExpressions;
using Betalgo.Ranul.OpenAI.ObjectModels.RequestModels;
using Betalgo.Ranul.OpenAI.ObjectModels.SharedModels;
using Microsoft.AI.Foundry.Local;
using Microsoft.Extensions.Logging.Abstractions;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;

const string modelAlias = "qwen2.5-0.5b";
const string appName = "frezz_tech_edge_agent";
CancellationToken ct = CancellationToken.None;

const string systemPrompt =
    "You are a scheduling assistant. Use the booking tool to schedule the " +
    "requested meeting. Always call the tool - do not just describe what " +
    "you would do.";

// Same three prompts and expected values as schema_hardening_eval.py, so
// the two language tracks measure the exact same thing.
List<Case> cases =
[
    new Case(
        "Schedule a high-priority 30-minute meeting with Jamie Chen (jamie.chen@example.com) to discuss the Q3 roadmap.",
        "Jamie Chen", "jamie.chen@example.com", "high", 30, ["Q3 roadmap"]),
    new Case(
        "Set up a medium-priority, half-hour call with Alex Kim (alex.kim@example.com) to cover the MCP rollout and the Q3 roadmap.",
        "Alex Kim", "alex.kim@example.com", "medium", 30, ["MCP rollout", "Q3 roadmap"]),
    new Case(
        "Set up a quick 15-minute, low-key chat with Sam Rivera (sam.rivera@example.com) about onboarding feedback - nothing urgent.",
        "Sam Rivera", "sam.rivera@example.com", "low", 15, ["onboarding feedback"]),
];

// The MCP server is the same standalone C# one used by mcp-agent.
// `dotnet run --project` builds it on demand if needed, so this works on a
// fresh clone with no separate build step.
var transport = new StdioClientTransport(new StdioClientTransportOptions
{
    Name = "edge-to-enterprise-tools",
    Command = "dotnet",
    Arguments = ["run", "--project", "../mcp-server/mcp-server.csproj"],
});
await using var mcpClient = await McpClient.CreateAsync(transport);

// Hand-authored to mirror the loose/strict schemas in ../mcp-server exactly
// (camelCase field names, matching that server's own convention) - there is
// no automatic MCP-schema-to-Foundry-ToolDefinition conversion in this
// sample, so keep these in sync if you change the C# server's tool defs.
var looseTool = new ToolDefinition
{
    Type = "function",
    Function = new FunctionDefinition
    {
        Name = "book_appointment_loose",
        Description = "Book a meeting.",
        Parameters = new PropertyDefinition
        {
            Type = "object",
            Properties = new Dictionary<string, PropertyDefinition>
            {
                { "attendeeName", new PropertyDefinition { Type = "string", Description = "who the meeting is with" } },
                { "attendeeEmail", new PropertyDefinition { Type = "string", Description = "their contact" } },
                { "topics", new PropertyDefinition { Type = "string", Description = "what to discuss" } },
                { "priority", new PropertyDefinition { Type = "string", Description = "how urgent" } },
                { "durationMinutes", new PropertyDefinition { Type = "string", Description = "how long" } },
            },
            Required = ["attendeeName", "attendeeEmail", "topics", "priority", "durationMinutes"],
        },
    },
};

var strictTool = new ToolDefinition
{
    Type = "function",
    Function = new FunctionDefinition
    {
        Name = "book_appointment_strict",
        Description = "Book a meeting with a fully-specified, structured request.",
        Parameters = new PropertyDefinition
        {
            Type = "object",
            Properties = new Dictionary<string, PropertyDefinition>
            {
                {
                    "attendee", new PropertyDefinition
                    {
                        Type = "object",
                        Properties = new Dictionary<string, PropertyDefinition>
                        {
                            { "name", new PropertyDefinition { Type = "string", Description = "Full name of the meeting attendee" } },
                            { "email", new PropertyDefinition { Type = "string", Description = "Attendee's email address" } },
                        },
                        Required = ["name", "email"],
                    }
                },
                {
                    "topics", new PropertyDefinition
                    {
                        Type = "array",
                        Items = new PropertyDefinition { Type = "string" },
                        Description = "List of discussion topics, one per item",
                    }
                },
                {
                    "priority", new PropertyDefinition
                    {
                        Type = "string",
                        Enum = ["Low", "Medium", "High"],
                        Description = "Meeting priority",
                    }
                },
                { "durationMinutes", new PropertyDefinition { Type = "integer", Description = "Meeting duration in minutes" } },
            },
            Required = ["attendee", "topics", "priority", "durationMinutes"],
        },
    },
};

var schemaVariants = new (string Label, string ToolName, ToolDefinition Tool)[]
{
    ("loose", "book_appointment_loose", looseTool),
    ("strict", "book_appointment_strict", strictTool),
};

// --- Load the local model (same lifecycle as Part 1 / mcp-agent) --------
Console.WriteLine("[System] Initializing in-process Foundry Local runtime...");
await FoundryLocalManager.CreateAsync(new Configuration { AppName = appName }, NullLogger.Instance);
var manager = FoundryLocalManager.Instance;

try
{
    string currentEp = "";
    await manager.DownloadAndRegisterEpsAsync((epName, percent) =>
    {
        if (epName != currentEp)
        {
            if (currentEp != "") Console.WriteLine();
            currentEp = epName;
        }
        Console.Write($"\r  {epName,-30}  {percent,6:F1}%");
    });
    if (currentEp != "") Console.WriteLine();

    var catalog = await manager.GetCatalogAsync();
    var model = await catalog.GetModelAsync(modelAlias)
        ?? throw new InvalidOperationException($"Model '{modelAlias}' not found in the catalog.");
    if (!await model.IsCachedAsync())
    {
        await model.DownloadAsync(progress => Console.Write($"\rDownloading model: {progress:F1}%"));
        Console.WriteLine();
    }
    await model.LoadAsync();
    var chatClient = await model.GetChatClientAsync();

    var results = new Dictionary<(string Variant, string Case), List<string>>();

    foreach (var (variantLabel, toolName, tool) in schemaVariants)
    {
        Console.WriteLine($"\n=== local (qwen2.5-0.5b) | {variantLabel} schema ({toolName}) ===");
        foreach (var c in cases)
        {
            var errors = await RunCaseAsync(chatClient, mcpClient, tool, toolName, c, ct);
            Console.WriteLine($"  [{c.ExpectedName}] {string.Join("+", errors)}");
            results[(variantLabel, c.ExpectedName)] = errors;
        }
    }

    PrintSummary(results, schemaVariants.Select(v => v.Label).ToArray(), cases);

    await model.UnloadAsync();
}
finally
{
    manager.Dispose();
}

async Task<List<string>> RunCaseAsync(
    OpenAIChatClient chatClient, McpClient mcpClient, ToolDefinition tool, string toolName, Case c, CancellationToken cancellationToken)
{
    List<ChatMessage> messages =
    [
        new ChatMessage { Role = "system", Content = systemPrompt },
        new ChatMessage { Role = "user", Content = c.Prompt },
    ];

    var response = await chatClient.CompleteChatAsync(messages, [tool], cancellationToken);
    var message = response.Choices[0].Message;

    if (message.ToolCalls is not { Count: > 0 })
        return ["NO_TOOL_CALL"];

    var call = message.ToolCalls[0];
    if (call.FunctionCall?.Name != toolName)
        return ["WRONG_TOOL"];

    Dictionary<string, object?> toolArgs;
    try
    {
        toolArgs = JsonSerializer.Deserialize<Dictionary<string, object?>>(call.FunctionCall.Arguments!)
            ?? throw new JsonException("Empty arguments");
    }
    catch (JsonException)
    {
        return ["MALFORMED_JSON"];
    }

    var result = await mcpClient.CallToolAsync(toolName, toolArgs, cancellationToken: cancellationToken);
    if (result.IsError == true)
        return ["SERVER_REJECTED"];

    var semanticErrors = ClassifySemantic(toolName, toolArgs, c);
    return semanticErrors.Count > 0 ? semanticErrors : ["OK"];
}

// Checks the extracted values against what the prompt actually asked for.
// The strict schema already rejects wrong *types* at the server - this
// catches wrong *values* that both schemas would let through, so the two
// variants are compared on equal footing.
List<string> ClassifySemantic(string toolName, Dictionary<string, object?> args, Case c)
{
    string name, email, priority;
    int? duration;
    string topicsText;

    if (toolName == "book_appointment_strict")
    {
        name = GetNestedString(args, "attendee", "name");
        email = GetNestedString(args, "attendee", "email");
        priority = GetString(args, "priority");
        duration = ExtractInt(args, "durationMinutes");
        topicsText = GetTopicsText(args, "topics");
    }
    else
    {
        name = GetString(args, "attendeeName");
        email = GetString(args, "attendeeEmail");
        priority = GetString(args, "priority");
        duration = ExtractInt(args, "durationMinutes");
        topicsText = GetTopicsText(args, "topics");
    }

    var errors = new List<string>();
    if (!name.Contains(c.ExpectedName, StringComparison.OrdinalIgnoreCase)) errors.Add("WRONG_NAME");
    if (!string.Equals(email.Trim(), c.ExpectedEmail, StringComparison.OrdinalIgnoreCase)) errors.Add("WRONG_EMAIL");
    if (!string.Equals(priority.Trim(), c.ExpectedPriority, StringComparison.OrdinalIgnoreCase)) errors.Add("WRONG_PRIORITY");
    if (duration != c.ExpectedDuration) errors.Add("WRONG_DURATION");
    if (!c.ExpectedTopics.All(t => topicsText.Contains(t, StringComparison.OrdinalIgnoreCase))) errors.Add("MISSING_TOPIC");
    return errors;
}

string GetString(Dictionary<string, object?> args, string key) =>
    args.TryGetValue(key, out var v) && v is JsonElement { ValueKind: JsonValueKind.String } je ? je.GetString() ?? "" : "";

string GetNestedString(Dictionary<string, object?> args, string parentKey, string childKey)
{
    if (args.TryGetValue(parentKey, out var v) && v is JsonElement { ValueKind: JsonValueKind.Object } parent
        && parent.TryGetProperty(childKey, out var child) && child.ValueKind == JsonValueKind.String)
        return child.GetString() ?? "";
    return "";
}

int? ExtractInt(Dictionary<string, object?> args, string key)
{
    if (!args.TryGetValue(key, out var v) || v is not JsonElement je) return null;
    if (je.ValueKind == JsonValueKind.Number && je.TryGetInt32(out var n)) return n;
    if (je.ValueKind == JsonValueKind.String)
    {
        var match = Regex.Match(je.GetString() ?? "", @"\d+");
        if (match.Success) return int.Parse(match.Value);
    }
    return null;
}

string GetTopicsText(Dictionary<string, object?> args, string key)
{
    if (!args.TryGetValue(key, out var v) || v is not JsonElement je) return "";
    return je.ValueKind switch
    {
        JsonValueKind.Array => string.Join(" ", je.EnumerateArray().Select(e => e.GetString() ?? "")),
        JsonValueKind.String => je.GetString() ?? "",
        _ => "",
    };
}

void PrintSummary(Dictionary<(string Variant, string Case), List<string>> results, string[] variants, List<Case> allCases)
{
    Console.WriteLine("\n=== Schema hardening summary ===");
    Console.WriteLine($"{"Schema",-6} | {"Clean calls",-12} | Errors");
    Console.WriteLine(new string('-', 70));
    foreach (var variant in variants)
    {
        var perCase = allCases.Select(c => results[(variant, c.ExpectedName)]).ToList();
        var clean = perCase.Count(errors => errors.Count == 1 && errors[0] == "OK");
        var errorCounts = perCase
            .SelectMany(errors => errors[0] == "OK" ? [] : errors)
            .GroupBy(e => e)
            .Select(g => $"{g.Key} x{g.Count()}");
        var errorsStr = errorCounts.Any() ? string.Join(", ", errorCounts) : "-";
        Console.WriteLine($"{variant,-6} | {clean}/{allCases.Count,-10} | {errorsStr}");
    }
}

record Case(string Prompt, string ExpectedName, string ExpectedEmail, string ExpectedPriority, int ExpectedDuration, string[] ExpectedTopics);
