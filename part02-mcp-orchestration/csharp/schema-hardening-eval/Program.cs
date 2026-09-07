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
//
// --model <alias...> overrides the model(s) under test (default:
// qwen2.5-0.5b) - one or more aliases, run one after another, e.g.
// `dotnet run -- --model qwen2.5-1.5b qwen2.5-7b qwen3.5-0.8b` to check
// whether the SERVER_REJECTED-heavy result documented in ../../README.md is
// specific to this model size or a smaller-model phenomenon in general. Run
// `foundry model list` to see what's actually available on your hardware.

using System.Text.Json;
using System.Text.RegularExpressions;
using Betalgo.Ranul.OpenAI.ObjectModels.RequestModels;
using Betalgo.Ranul.OpenAI.ObjectModels.SharedModels;
using Microsoft.AI.Foundry.Local;
using Microsoft.Extensions.Logging.Abstractions;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;

const string defaultModelAlias = "qwen2.5-0.5b";
const string appName = "frezz_tech_edge_agent";
CancellationToken ct = CancellationToken.None;

// --model takes one or more aliases, consuming every argument after it -
// there's no other flag to stop at, so the rest of args is the list.
List<string> modelAliases = [defaultModelAlias];
var modelArgIndex = Array.IndexOf(args, "--model");
if (modelArgIndex >= 0 && modelArgIndex + 1 < args.Length)
{
    modelAliases = args.Skip(modelArgIndex + 1).ToList();
}
Console.WriteLine($"[System] Testing model alias(es): {string.Join(", ", modelAliases)}" + (modelAliases is [var only] && only == defaultModelAlias ? " (default)" : ""));

const string systemPrompt =
    "You are a scheduling assistant. Use the booking tool to schedule the " +
    "requested meeting. Always call the tool - do not just describe what " +
    "you would do.";

// Same ten prompts and expected values as schema_hardening_eval.py, so the
// two language tracks measure the exact same thing. A small n (like the
// original 3) only gives coarse 0/33/67/100% clean-rate buckets; ten cases
// gives 10% resolution, which matters for a table or chart meant to show a
// real gap, not just a hunch.
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
    new Case(
        "Book a high-priority one-hour meeting with Morgan Lee (morgan.lee@example.com) about the security audit.",
        "Morgan Lee", "morgan.lee@example.com", "high", 60, ["security audit"]),
    new Case(
        "Arrange a medium-priority 45-minute session with Taylor Brooks (taylor.brooks@example.com) to go over hiring, budget planning, and vendor contracts.",
        "Taylor Brooks", "taylor.brooks@example.com", "medium", 45, ["hiring", "budget planning", "vendor contracts"]),
    new Case(
        "This is urgent - set up a high-priority 20-minute call with Priya Patel (priya.patel@example.com) about the production outage.",
        "Priya Patel", "priya.patel@example.com", "high", 20, ["production outage"]),
    new Case(
        "Just a quick ten-minute, no-rush chat with Devon Clarke (devon.clarke@example.com) about the intern onboarding checklist.",
        "Devon Clarke", "devon.clarke@example.com", "low", 10, ["intern onboarding checklist"]),
    new Case(
        // "standard" has no exact enum match - medium is the reasonable read.
        "Schedule a standard-priority 30-minute sync with Riley Nguyen (riley.nguyen@example.com) covering the Q4 planning doc.",
        "Riley Nguyen", "riley.nguyen@example.com", "medium", 30, ["Q4 planning doc"]),
    new Case(
        "Set up a low-priority 15-minute check-in with Casey Morgan (casey.morgan@example.com) about desk setup and badge access.",
        "Casey Morgan", "casey.morgan@example.com", "low", 15, ["desk setup", "badge access"]),
    new Case(
        "This is critical - schedule a high-priority 60-minute review with Jordan Ellis (jordan.ellis@example.com) covering the compliance audit and the incident report.",
        "Jordan Ellis", "jordan.ellis@example.com", "high", 60, ["compliance audit", "incident report"]),
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
                { "durationMinutes", new PropertyDefinition { Type = "integer", Description = "how long, in minutes" } },
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

// Used by ParseFallbackToolCall() below - declared here (not next to that
// function) because top-level statements require local variables to be
// assigned in program order before use, and RunCaseAsync (a local function
// that closes over these) is invoked from the loop further down.
Regex fallbackToolCallRegex = new(
    @"<tool_call>\s*<function=(?<name>[^>]+)>(?<body>.*?)</function>\s*</tool_call>", RegexOptions.Singleline);
Regex fallbackParamRegex = new(@"<parameter=(?<key>[^>]+)>(?<value>.*?)</parameter>", RegexOptions.Singleline);

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

    // Results keyed by (backend label, schema variant, case) so a --model
    // sweep across several sizes shows up as extra rows in the same summary
    // table instead of overwriting each other.
    var results = new Dictionary<(string Backend, string Variant, string Case), List<string>>();
    var backendLabels = new List<string>();

    // One model at a time, loaded then unloaded before the next - a sweep
    // across several sizes would otherwise try to hold every model's
    // weights in memory simultaneously.
    foreach (var modelAlias in modelAliases)
    {
        var backendLabel = $"local ({modelAlias})";
        backendLabels.Add(backendLabel);

        Console.WriteLine($"\n[System] Loading model '{modelAlias}'...");
        var model = await catalog.GetModelAsync(modelAlias)
            ?? throw new InvalidOperationException($"Model '{modelAlias}' not found in the catalog.");

        // The catalog manifest tags each model with capabilities (e.g.
        // "chat,tool-calling") and an explicit SupportsToolCalling flag. A
        // model cataloged as not tool-capable would produce the exact same
        // NO_TOOL_CALL result as one that's simply too weak to use the
        // tool - printing this up front turns that ambiguity into a
        // checkable fact instead of a guess (see ../../python/schema_hardening_eval.py's
        // load_local_model() for the finding that motivated this).
        Console.WriteLine($"  capabilities={model.Info.Capabilities ?? "(null)"} supportsToolCalling={model.Info.SupportsToolCalling?.ToString() ?? "(null)"}");

        if (!await model.IsCachedAsync())
        {
            await model.DownloadAsync(progress => Console.Write($"\rDownloading model: {progress:F1}%"));
            Console.WriteLine();
        }
        await model.LoadAsync();
        var chatClient = await model.GetChatClientAsync();

        foreach (var (variantLabel, toolName, tool) in schemaVariants)
        {
            Console.WriteLine($"\n=== {backendLabel} | {variantLabel} schema ({toolName}) ===");
            foreach (var c in cases)
            {
                var errors = await RunCaseAsync(chatClient, mcpClient, tool, toolName, c, ct);
                Console.WriteLine($"  [{c.ExpectedName}] {string.Join("+", errors)}");
                results[(backendLabel, variantLabel, c.ExpectedName)] = errors;
            }
        }

        await model.UnloadAsync();
    }

    PrintSummary(results, backendLabels, schemaVariants.Select(v => v.Label).ToArray(), cases);
}
finally
{
    manager.Dispose();
}

// Some models (observed: the qwen3.5 family via Foundry Local) don't emit
// an OpenAI-style tool call at all - instead they write it as plain text in
// a custom tag format:
//   <tool_call><function=NAME><parameter=KEY>VALUE</parameter>...</function></tool_call>
// message.ToolCalls then comes back empty even though the model attempted
// (and, going by the raw text, largely got right) a real call - counting
// that as NO_TOOL_CALL would conflate "wrong client-side parsing" with
// "model didn't try". This mirrors ../../python/schema_hardening_eval.py's
// parse_fallback_tool_call() so the study measures the model's actual
// argument accuracy instead. See that function's docstring for why each
// value is only JSON-decoded when the tool's own schema says the parameter
// isn't a plain string - this tag format carries no quoting, so a bare `30`
// for a string-typed parameter must stay the string "30", not become an int
// that the MCP server's own validation would then reject.
(string Name, Dictionary<string, object?> Args)? ParseFallbackToolCall(string? content, Dictionary<string, string?> paramTypes)
{
    if (string.IsNullOrEmpty(content)) return null;
    var match = fallbackToolCallRegex.Match(content);
    if (!match.Success) return null;

    var name = match.Groups["name"].Value.Trim();
    var args = new Dictionary<string, object?>();
    foreach (Match param in fallbackParamRegex.Matches(match.Groups["body"].Value))
    {
        var key = param.Groups["key"].Value.Trim();
        var rawValue = param.Groups["value"].Value.Trim();
        if (paramTypes.TryGetValue(key, out var type) && type == "string")
        {
            args[key] = JsonSerializer.SerializeToElement(rawValue);
            continue;
        }
        try
        {
            args[key] = JsonDocument.Parse(rawValue).RootElement;
        }
        catch (JsonException)
        {
            args[key] = JsonSerializer.SerializeToElement(rawValue);
        }
    }
    return (name, args);
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

    string? callName;
    Dictionary<string, object?> toolArgs;

    if (message.ToolCalls is { Count: > 0 })
    {
        var call = message.ToolCalls[0];
        callName = call.FunctionCall?.Name;
        try
        {
            toolArgs = JsonSerializer.Deserialize<Dictionary<string, object?>>(call.FunctionCall!.Arguments!)
                ?? throw new JsonException("Empty arguments");
        }
        catch (JsonException)
        {
            return ["MALFORMED_JSON"];
        }
    }
    else
    {
        var paramTypes = tool.Function!.Parameters!.Properties!.ToDictionary(p => p.Key, p => p.Value.Type);
        var fallback = ParseFallbackToolCall(message.Content, paramTypes);
        if (fallback is null)
        {
            var preview = (message.Content ?? "").Trim().Replace("\n", " ");
            if (preview.Length > 200) preview = preview[..200];
            Console.WriteLine($"    (no tool call - model said: \"{preview}\")");
            return ["NO_TOOL_CALL"];
        }

        (callName, toolArgs) = fallback.Value;
        Console.WriteLine($"    (client didn't parse the tool call - recovered via fallback parser: {callName}(...))");
    }

    if (callName != toolName)
        return ["WRONG_TOOL"];

    var result = await mcpClient.CallToolAsync(toolName, toolArgs, cancellationToken: cancellationToken);
    if (result.IsError == true)
    {
        // Print what was actually sent and the server's own validation
        // message - "SERVER_REJECTED" alone doesn't say *which* field or
        // type was wrong, and that's the difference between "this model
        // can't produce a nested object at all" and "it got everything
        // right except one enum casing". Mirrors
        // ../../python/schema_hardening_eval.py's run_case() diagnostic -
        // this is exactly what found the qwen2.5-7b bug documented in
        // ../../README.md (it drops `attendee.name` on every strict call).
        var errorText = string.Join("; ", result.Content.OfType<TextContentBlock>().Select(c => c.Text));
        Console.WriteLine($"    (rejected args: {JsonSerializer.Serialize(toolArgs)} | server said: {errorText})");
        return ["SERVER_REJECTED"];
    }

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

void PrintSummary(
    Dictionary<(string Backend, string Variant, string Case), List<string>> results,
    List<string> backends, string[] variants, List<Case> allCases)
{
    Console.WriteLine("\n=== Schema hardening summary ===");
    Console.WriteLine($"{"Backend",-22} | {"Schema",-6} | {"Clean calls",-12} | Errors");
    Console.WriteLine(new string('-', 90));
    foreach (var backend in backends)
    {
        foreach (var variant in variants)
        {
            var perCase = allCases.Select(c => results[(backend, variant, c.ExpectedName)]).ToList();
            var clean = perCase.Count(errors => errors.Count == 1 && errors[0] == "OK");
            var errorCounts = perCase
                .SelectMany(errors => errors[0] == "OK" ? [] : errors)
                .GroupBy(e => e)
                .Select(g => $"{g.Key} x{g.Count()}");
            var errorsStr = errorCounts.Any() ? string.Join(", ", errorCounts) : "-";
            Console.WriteLine($"{backend,-22} | {variant,-6} | {clean}/{allCases.Count,-10} | {errorsStr}");
        }
    }
}

record Case(string Prompt, string ExpectedName, string ExpectedEmail, string ExpectedPriority, int ExpectedDuration, string[] ExpectedTopics);
