// Part 4 quality gate, C# track. Counterpart to ../../python/eval_pipeline.py:
// same golden dataset (../../golden_dataset.json), same error taxonomy as
// Part 2, same pass/fail contract (exit code 1 fails the build).
//
// For every golden case this:
//   1. asks the local Foundry Local model to call book_appointment_strict,
//   2. executes the call against Part 2's own C# MCP server, so SERVER_REJECTED
//      means what it meant in Part 2 (System.Text.Json refused the arguments),
//   3. classifies the outcome with the Part 2 taxonomy (deterministic, free),
//   4. optionally (--judge) asks Microsoft.Extensions.AI.Evaluation's
//      ToolCallAccuracyEvaluator whether the call was accurate.
//
// One difference to Python worth knowing before comparing numbers: the .NET
// ToolCallAccuracyEvaluator returns a BooleanMetric (accurate or not), while
// Python's returns a 1 to 5 score. That is why the dataset carries a separate
// quality_gate.min_judge_pass_rate for this track.
//
// Usage (from this folder):
//   dotnet run                                  // qwen3.5-4b
//   dotnet run -- --model qwen2.5-1.5b          // the cross-language finding: 0/10
//   dotnet run -- --judge                       // + LLM judge (AZURE_OPENAI_ENDPOINT, az login)
//
// Environment variables:
//   FOUNDRY_LOCAL_MODEL    default model alias (overridden by --model)
//   AZURE_OPENAI_ENDPOINT  https://<account>.openai.azure.com, only for --judge
//   JUDGE_MODEL            judge deployment (default gpt-5-mini)

using System.ClientModel.Primitives;
using System.Text.Json;
using System.Text.RegularExpressions;
using Azure.Identity;
using Betalgo.Ranul.OpenAI.ObjectModels.RequestModels;
using Betalgo.Ranul.OpenAI.ObjectModels.SharedModels;
using Microsoft.AI.Foundry.Local;
using Microsoft.Extensions.AI.Evaluation;
using Microsoft.Extensions.AI.Evaluation.Quality;
using Microsoft.Extensions.Logging.Abstractions;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
// Aliased: Microsoft.Extensions.AI and the Foundry Local SDK (Betalgo) both
// define ChatMessage. Betalgo types talk to the local model, AI.* types feed
// the judge.
using AI = Microsoft.Extensions.AI;

// qwen3.5-4b, not qwen2.5-1.5b as in Python: with the hand-authored C# schema,
// qwen2.5-1.5b drops attendee.name in every case (0/10, the Part 2 finding),
// while qwen3.5-4b scores 9/10. See ../../README.md ("Verified results").
const string defaultModelAlias = "qwen3.5-4b";
const string appName = "frezz_tech_edge_agent";
const string judgeScope = "https://ai.azure.com/.default";
CancellationToken ct = CancellationToken.None;

// Same system prompt as Part 2's schema-hardening-eval.
const string systemPrompt =
    "You are a scheduling assistant. Use the booking tool to schedule the " +
    "requested meeting. Always call the tool - do not just describe what " +
    "you would do.";

var modelAlias = GetArg("--model") ?? NullIfEmpty(Environment.GetEnvironmentVariable("FOUNDRY_LOCAL_MODEL")) ?? defaultModelAlias;
var useJudge = args.Contains("--judge");

// Relative to this project folder, like Part 2's samples: run it from here.
var datasetPath = Path.GetFullPath(Path.Combine("..", "..", "golden_dataset.json"));
var dataset = JsonSerializer.Deserialize<GoldenDataset>(
        File.ReadAllText(datasetPath),
        new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower, PropertyNameCaseInsensitive = true })
    ?? throw new InvalidOperationException($"Could not read {datasetPath}.");

// Hand-authored to mirror book_appointment_strict in Part 2's C# MCP server
// (camelCase, PascalCase enum values), exactly as schema-hardening-eval does.
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

// Used by ParseFallbackToolCall() below. Declared before the loop because
// top-level local functions that capture locals need them assigned first.
Regex fallbackToolCallRegex = new(
    @"<tool_call>\s*<function=(?<name>[^>]+)>(?<body>.*?)</function>\s*</tool_call>", RegexOptions.Singleline);
Regex fallbackParamRegex = new(@"<parameter=(?<key>[^>]+)>(?<value>.*?)</parameter>", RegexOptions.Singleline);

// --- Optional LLM judge (Entra ID, no key) ---------------------------------
ToolCallAccuracyEvaluator? judge = null;
ChatConfiguration? judgeConfiguration = null;
ToolCallAccuracyEvaluatorContext? judgeContext = null;
if (useJudge)
{
    var endpoint = NullIfEmpty(Environment.GetEnvironmentVariable("AZURE_OPENAI_ENDPOINT"))
        ?? throw new InvalidOperationException("--judge needs AZURE_OPENAI_ENDPOINT (Bicep output openAiEndpoint).");
    var judgeDeployment = NullIfEmpty(Environment.GetEnvironmentVariable("JUDGE_MODEL")) ?? "gpt-5-mini";

    var openAiChatClient = new global::OpenAI.Chat.ChatClient(
        judgeDeployment,
        new BearerTokenPolicy(new DefaultAzureCredential(), judgeScope),
        new global::OpenAI.OpenAIClientOptions { Endpoint = new Uri($"{endpoint.TrimEnd('/')}/openai/v1/") });

    judgeConfiguration = new ChatConfiguration(
        new ReasoningModelCompatibleChatClient(AI.OpenAIClientExtensions.AsIChatClient(openAiChatClient)));
    judge = new ToolCallAccuracyEvaluator();
    judgeContext = new ToolCallAccuracyEvaluatorContext(
        AI.AIFunctionFactory.CreateDeclaration(
            strictTool.Function!.Name!,
            strictTool.Function.Description,
            JsonSerializer.SerializeToElement(strictTool.Function.Parameters)));
    Console.WriteLine($"[Judge] ToolCallAccuracyEvaluator on deployment '{judgeDeployment}'");
}

// --- Part 2's C# MCP server, spawned over stdio ----------------------------
var transport = new StdioClientTransport(new StdioClientTransportOptions
{
    Name = "edge-to-enterprise-tools",
    Command = "dotnet",
    Arguments = ["run", "--project", "../../../part02-mcp-orchestration/csharp/mcp-server/mcp-server.csproj"],
});
await using var mcpClient = await McpClient.CreateAsync(transport);

// --- Local model (same lifecycle as Parts 1 and 2) -------------------------
Console.WriteLine("[System] Initializing in-process Foundry Local runtime...");
await FoundryLocalManager.CreateAsync(new Configuration { AppName = appName }, NullLogger.Instance);
var manager = FoundryLocalManager.Instance;
var results = new List<CaseResult>();

try
{
    await manager.DownloadAndRegisterEpsAsync((epName, percent) => Console.Write($"\r  {epName,-30}  {percent,6:F1}%"));
    Console.WriteLine();

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
    // A gate has to be repeatable: the same commit should get the same verdict.
    chatClient.Settings.Temperature = 0.0f;

    Console.WriteLine($"\n=== local ({modelAlias}) | {dataset.Cases.Count} golden cases | tool {dataset.Tool} ===");
    foreach (var goldenCase in dataset.Cases)
    {
        var result = await RunCaseAsync(chatClient, goldenCase);
        if (judge is not null)
        {
            var (judgedAccurate, judgedReason) = await JudgeAsync(goldenCase, result);
            result = result with { JudgeAccurate = judgedAccurate, JudgeReason = judgedReason };
        }
        results.Add(result);

        var judgeText = result.JudgeAccurate is bool accurate ? $" | judge {(accurate ? "accurate" : "inaccurate")}" : "";
        Console.WriteLine($"  [{goldenCase.Id}] {string.Join("+", result.Outcome)}{judgeText}");
        if (result.JudgeAccurate == false && result.JudgeReason is { Length: > 0 } judgeReason)
        {
            // Most useful when the judge disagrees with the taxonomy (outcome OK,
            // judge inaccurate): the reason says what the judge objected to.
            Console.WriteLine($"    judge said: {(judgeReason.Length > 400 ? judgeReason[..400] + " ..." : judgeReason)}");
        }
        if (result.ServerMessage is not null)
        {
            // The C# server only says "An error occurred invoking ...", so the
            // arguments are what tells you *which* field was wrong (as in Part 2).
            Console.WriteLine($"    rejected args: {JsonSerializer.Serialize(result.Arguments)}");
            Console.WriteLine($"    server said: {result.ServerMessage}");
        }
    }

    await model.UnloadAsync();
}
finally
{
    manager.Dispose();
}

// --- Gate ------------------------------------------------------------------
var cleanRate = results.Count(r => r.IsClean) / (double)results.Count;
var passed = cleanRate >= dataset.QualityGate.MinCleanCallRate;
var errorCounts = results.Where(r => !r.IsClean).SelectMany(r => r.Outcome)
    .GroupBy(code => code).Select(g => $"{g.Key} x{g.Count()}");

Console.WriteLine("\n=== Quality gate summary ===");
Console.WriteLine($"  backend          local ({modelAlias})");
Console.WriteLine($"  clean call rate  {cleanRate:P0} (min {dataset.QualityGate.MinCleanCallRate:P0})");
Console.WriteLine($"  errors           {(errorCounts.Any() ? string.Join(", ", errorCounts) : "-")}");

if (judge is not null)
{
    var judged = results.Where(r => r.JudgeAccurate is not null).ToList();
    var judgePassRate = judged.Count == 0 ? 0 : judged.Count(r => r.JudgeAccurate == true) / (double)results.Count;
    Console.WriteLine($"  judge pass rate  {judgePassRate:P0} (min {dataset.QualityGate.MinJudgePassRate:P0})");
    passed = passed && judgePassRate >= dataset.QualityGate.MinJudgePassRate;
}

Console.WriteLine($"\nQuality gate {(passed ? "PASSED" : "FAILED")}.");
return passed ? 0 : 1;

// --- One case ----------------------------------------------------------------

async Task<CaseResult> RunCaseAsync(OpenAIChatClient chatClient, GoldenCase goldenCase)
{
    List<ChatMessage> messages =
    [
        new ChatMessage { Role = "system", Content = systemPrompt },
        new ChatMessage { Role = "user", Content = goldenCase.Query },
    ];

    var response = await chatClient.CompleteChatAsync(messages, [strictTool], ct);
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
            return new CaseResult(goldenCase.Id, ["MALFORMED_JSON"]);
        }
    }
    else
    {
        var paramTypes = strictTool.Function!.Parameters!.Properties!.ToDictionary(p => p.Key, p => p.Value.Type);
        var fallback = ParseFallbackToolCall(message.Content, paramTypes);
        if (fallback is null)
            return new CaseResult(goldenCase.Id, ["NO_TOOL_CALL"]);
        (callName, toolArgs) = fallback.Value;
    }

    if (callName != dataset.Tool)
        return new CaseResult(goldenCase.Id, ["WRONG_TOOL"], callName, toolArgs);

    var result = await mcpClient.CallToolAsync(dataset.Tool, toolArgs, cancellationToken: ct);
    if (result.IsError == true)
    {
        var serverMessage = string.Join(" ", result.Content.OfType<TextContentBlock>().Select(c => c.Text));
        return new CaseResult(goldenCase.Id, ["SERVER_REJECTED"], callName, toolArgs, serverMessage);
    }

    var errors = ClassifySemantic(toolArgs, goldenCase.ExpectedParameters);
    return new CaseResult(goldenCase.Id, errors.Count > 0 ? errors : ["OK"], callName, toolArgs);
}

async Task<(bool? Accurate, string? Reason)> JudgeAsync(GoldenCase goldenCase, CaseResult result)
{
    if (result.CallName is null || result.Arguments is null)
        return (false, "No parseable tool call."); // counted as inaccurate, like Python's floor score

    List<AI.ChatMessage> conversation =
    [
        new(AI.ChatRole.System, systemPrompt),
        new(AI.ChatRole.User, goldenCase.Query),
    ];
    var modelResponse = new AI.ChatResponse(new AI.ChatMessage(
        AI.ChatRole.Assistant,
        [new AI.FunctionCallContent($"call_{goldenCase.Id}", result.CallName, result.Arguments)]));

    var evaluation = await judge!.EvaluateAsync(conversation, modelResponse, judgeConfiguration, [judgeContext!], ct);
    var metric = evaluation.Get<BooleanMetric>(ToolCallAccuracyEvaluator.ToolCallAccuracyMetricName);
    return (metric.Value, metric.Reason);
}

// Part 2's taxonomy, mapped from the dataset's snake_case expectations onto the
// C# server's camelCase arguments. Wrong *types* never get here (the server
// already rejected them); this catches wrong *values*.
List<string> ClassifySemantic(Dictionary<string, object?> toolArgs, ExpectedParameters expected)
{
    var errors = new List<string>();
    if (!GetNestedString(toolArgs, "attendee", "name").Contains(expected.Attendee.Name, StringComparison.OrdinalIgnoreCase)) errors.Add("WRONG_NAME");
    if (!string.Equals(GetNestedString(toolArgs, "attendee", "email").Trim(), expected.Attendee.Email, StringComparison.OrdinalIgnoreCase)) errors.Add("WRONG_EMAIL");
    if (!string.Equals(GetString(toolArgs, "priority").Trim(), expected.Priority, StringComparison.OrdinalIgnoreCase)) errors.Add("WRONG_PRIORITY");
    if (ExtractInt(toolArgs, "durationMinutes") != expected.DurationMinutes) errors.Add("WRONG_DURATION");
    var topicsText = GetTopicsText(toolArgs, "topics");
    if (!expected.Topics.All(t => topicsText.Contains(t, StringComparison.OrdinalIgnoreCase))) errors.Add("MISSING_TOPIC");
    return errors;
}

// Same tag-format fallback as Part 2 (observed with the qwen3.5 family): the
// model writes the call as text instead of an OpenAI-style tool call.
(string Name, Dictionary<string, object?> Args)? ParseFallbackToolCall(string? content, Dictionary<string, string?> paramTypes)
{
    if (string.IsNullOrEmpty(content)) return null;
    var match = fallbackToolCallRegex.Match(content);
    if (!match.Success) return null;

    var toolArgs = new Dictionary<string, object?>();
    foreach (Match param in fallbackParamRegex.Matches(match.Groups["body"].Value))
    {
        var key = param.Groups["key"].Value.Trim();
        var rawValue = param.Groups["value"].Value.Trim();
        if (paramTypes.TryGetValue(key, out var type) && type == "string")
        {
            toolArgs[key] = JsonSerializer.SerializeToElement(rawValue);
            continue;
        }
        try
        {
            toolArgs[key] = JsonDocument.Parse(rawValue).RootElement;
        }
        catch (JsonException)
        {
            toolArgs[key] = JsonSerializer.SerializeToElement(rawValue);
        }
    }
    return (match.Groups["name"].Value.Trim(), toolArgs);
}

string GetString(Dictionary<string, object?> toolArgs, string key) =>
    toolArgs.TryGetValue(key, out var v) && v is JsonElement { ValueKind: JsonValueKind.String } je ? je.GetString() ?? "" : "";

string GetNestedString(Dictionary<string, object?> toolArgs, string parentKey, string childKey) =>
    toolArgs.TryGetValue(parentKey, out var v) && v is JsonElement { ValueKind: JsonValueKind.Object } parent
        && parent.TryGetProperty(childKey, out var child) && child.ValueKind == JsonValueKind.String
        ? child.GetString() ?? ""
        : "";

int? ExtractInt(Dictionary<string, object?> toolArgs, string key)
{
    if (!toolArgs.TryGetValue(key, out var v) || v is not JsonElement je) return null;
    if (je.ValueKind == JsonValueKind.Number && je.TryGetInt32(out var n)) return n;
    if (je.ValueKind == JsonValueKind.String)
    {
        var digits = Regex.Match(je.GetString() ?? "", @"\d+");
        if (digits.Success) return int.Parse(digits.Value);
    }
    return null;
}

string GetTopicsText(Dictionary<string, object?> toolArgs, string key)
{
    if (!toolArgs.TryGetValue(key, out var v) || v is not JsonElement je) return "";
    return je.ValueKind switch
    {
        JsonValueKind.Array => string.Join(" ", je.EnumerateArray().Select(e => e.GetString() ?? "")),
        JsonValueKind.String => je.GetString() ?? "",
        _ => "",
    };
}

string? GetArg(string name)
{
    var index = Array.IndexOf(args, name);
    return index >= 0 && index + 1 < args.Length ? args[index + 1] : null;
}

static string? NullIfEmpty(string? value) => string.IsNullOrEmpty(value) ? null : value;

// --- Types -------------------------------------------------------------------

record GoldenDataset(int Version, string Tool, QualityGate QualityGate, List<GoldenCase> Cases);
record QualityGate(double MinCleanCallRate, double MinToolCallAccuracy, double MinJudgePassRate);
record GoldenCase(string Id, string Query, string ExpectedTool, ExpectedParameters ExpectedParameters);
record ExpectedParameters(ExpectedAttendee Attendee, string[] Topics, string Priority, int DurationMinutes);
record ExpectedAttendee(string Name, string Email);

record CaseResult(
    string Id,
    List<string> Outcome,
    string? CallName = null,
    Dictionary<string, object?>? Arguments = null,
    string? ServerMessage = null,
    bool? JudgeAccurate = null,
    string? JudgeReason = null)
{
    public bool IsClean => Outcome is ["OK"];
}

// gpt-5 family and o-series judges reject non-default sampling settings. A
// tight output budget can also be used up by reasoning tokens before the
// verdict is written. ToolCallAccuracyEvaluator sets both, so this strips them for the judge.
sealed class ReasoningModelCompatibleChatClient(AI.IChatClient innerClient) : AI.DelegatingChatClient(innerClient)
{
    public override Task<AI.ChatResponse> GetResponseAsync(
        IEnumerable<AI.ChatMessage> messages, AI.ChatOptions? options = null, CancellationToken cancellationToken = default)
    {
        if (options is not null)
        {
            options = options.Clone();
            options.Temperature = null;
            options.TopP = null;
            options.PresencePenalty = null;
            options.FrequencyPenalty = null;
            options.MaxOutputTokens = null;
        }
        return base.GetResponseAsync(messages, options, cancellationToken);
    }
}
