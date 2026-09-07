// Connects a Foundry Local chat model to tools exposed over MCP by the
// Python server in this part (../../python/mcp_server.py) - a concrete
// demonstration that MCP doesn't care what language either side is
// written in.
//
// Discovers the server's tools, then runs one tool-calling turn against the
// local model using a hand-declared tool schema mirroring get_weather() -
// see mcp_client.py for the fuller local-vs-cloud reliability comparison.

using System.Text.Json;
using Betalgo.Ranul.OpenAI.ObjectModels.RequestModels;
using Betalgo.Ranul.OpenAI.ObjectModels.ResponseModels;
using Betalgo.Ranul.OpenAI.ObjectModels.SharedModels;
using Microsoft.AI.Foundry.Local;
using Microsoft.Extensions.Logging.Abstractions;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
// Both Foundry Local (via Betalgo.Ranul.OpenAI) and the MCP SDK define a
// ToolChoice type - alias the one we mean to avoid CS0104 ambiguity.
using FoundryToolChoice = Betalgo.Ranul.OpenAI.ObjectModels.RequestModels.ToolChoice;

const string modelAlias = "qwen2.5-0.5b";
const string appName = "from_edge_to_enterprise_mcp_agent";
CancellationToken ct = CancellationToken.None;

// Path is relative to the project directory (`dotnet run`'s working directory).
var transport = new StdioClientTransport(new StdioClientTransportOptions
{
    Name = "edge-to-enterprise-tools",
    Command = "python3",
    Arguments = ["../../python/mcp_server.py"],
});

await using var mcpClient = await McpClient.CreateAsync(transport);

var mcpTools = await mcpClient.ListToolsAsync();
Console.WriteLine("[MCP] Discovered tool(s):");
foreach (var tool in mcpTools)
{
    Console.WriteLine($"  - {tool.Name}: {tool.Description}");
}

// --- Load the local model (same lifecycle as Part 1) --------------------
Console.WriteLine("\n[System] Initializing in-process Foundry Local runtime...");
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
    await model.DownloadAsync(progress => Console.Write($"\rDownloading model: {progress:F1}%"));
    Console.WriteLine();
    await model.LoadAsync();

    var chatClient = await model.GetChatClientAsync();
    chatClient.Settings.ToolChoice = FoundryToolChoice.Required;

    // The schema below mirrors get_weather() in mcp_server.py by hand -
    // there is no automatic MCP-schema-to-Foundry-ToolDefinition conversion
    // in this sample, so keep the two definitions in sync if you change one.
    List<ToolDefinition> tools =
    [
        new ToolDefinition
        {
            Type = "function",
            Function = new FunctionDefinition
            {
                Name = "get_weather",
                Description = "Get the current weather for a location.",
                Parameters = new PropertyDefinition
                {
                    Type = "object",
                    Properties = new Dictionary<string, PropertyDefinition>
                    {
                        { "location", new PropertyDefinition { Type = "string", Description = "The city or location" } },
                        { "unit", new PropertyDefinition { Type = "string", Description = "celsius or fahrenheit" } },
                    },
                    Required = ["location"],
                },
            },
        },
    ];

    List<ChatMessage> messages =
    [
        new ChatMessage { Role = "system", Content = "You are a helpful assistant. Use tools when needed." },
        new ChatMessage { Role = "user", Content = "What's the weather in Berlin?" },
    ];

    Console.WriteLine("\n[User]: What's the weather in Berlin?");
    Console.Write("[Assistant]: ");

    var toolCallChunks = new List<ChatCompletionCreateResponse>();
    await foreach (var chunk in chatClient.CompleteChatStreamingAsync(messages, tools, ct))
    {
        Console.Write(chunk.Choices[0].Message.Content);
        if (chunk.Choices[0].FinishReason == "tool_calls")
        {
            toolCallChunks.Add(chunk);
        }
    }
    Console.WriteLine();

    foreach (var chunk in toolCallChunks)
    {
        var call = chunk.Choices[0].Message.ToolCalls?[0].FunctionCall;
        if (call?.Name != "get_weather") continue;

        var toolArgs = JsonSerializer.Deserialize<Dictionary<string, object?>>(call.Arguments!)!;
        Console.WriteLine($"\n  -> tool call: get_weather({call.Arguments})");

        var mcpResult = await mcpClient.CallToolAsync("get_weather", toolArgs, cancellationToken: ct);
        var resultText = mcpResult.Content.OfType<TextContentBlock>().First().Text;
        Console.WriteLine($"  -> tool result: {resultText}");

        messages.Add(new ChatMessage
        {
            Role = "tool",
            ToolCallId = chunk.Choices[0].Message.ToolCalls![0].Id,
            Content = resultText,
        });
    }

    chatClient.Settings.ToolChoice = FoundryToolChoice.Auto;
    Console.Write("\n[Assistant]: ");
    await foreach (var chunk in chatClient.CompleteChatStreamingAsync(messages, tools, ct))
    {
        Console.Write(chunk.Choices[0].Message.Content);
    }
    Console.WriteLine();

    await model.UnloadAsync();
}
finally
{
    manager.Dispose();
}
