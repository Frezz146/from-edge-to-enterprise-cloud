// Latency and throughput benchmark: local Foundry Local inference vs. a
// cloud-hosted Azure OpenAI deployment, for the same prompt. Mirrors
// ../../python/benchmark.py so both languages produce a comparable table.
//
// Measures time to first token (TTFT), total wall-clock time, and an
// approximate tokens/sec (whitespace-split, not a real tokenizer count -
// good enough to compare relative throughput between the two backends).
//
// The cloud comparison only runs when AZURE_OPENAI_ENDPOINT,
// AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT are set, so this also
// works completely offline against the local model alone.

using System.ClientModel;
using System.Diagnostics;
using System.Text;
using Azure.AI.OpenAI;
using Microsoft.AI.Foundry.Local;
using Microsoft.Extensions.Logging.Abstractions;
using OpenAI.Chat;
// Both Foundry Local (via Betalgo.Ranul.OpenAI) and the official OpenAI SDK
// define a ChatMessage type - alias the one we mean to avoid CS0104.
using FoundryChatMessage = Betalgo.Ranul.OpenAI.ObjectModels.RequestModels.ChatMessage;

const string modelAlias = "qwen2.5-0.5b";
const string appName = "frezz_tech_edge_agent";
const string prompt = "Explain in two sentences why hybrid edge/cloud AI architectures matter.";
CancellationToken ct = CancellationToken.None;

var results = new List<RunResult> { await RunLocalAsync() };

var cloudResult = RunCloud();
if (cloudResult is not null) results.Add(cloudResult);

Console.WriteLine("\n=== Latency & throughput comparison ===");
PrintComparison(results);

async Task<RunResult> RunLocalAsync()
{
    await FoundryLocalManager.CreateAsync(new Configuration { AppName = appName }, NullLogger.Instance);
    var manager = FoundryLocalManager.Instance;
    try
    {
        await manager.DownloadAndRegisterEpsAsync((_, _) => { });

        var catalog = await manager.GetCatalogAsync();
        var model = await catalog.GetModelAsync(modelAlias)
            ?? throw new InvalidOperationException($"Model '{modelAlias}' not found in the catalog.");
        await model.DownloadAsync(_ => { });
        await model.LoadAsync();
        var chatClient = await model.GetChatClientAsync();

        Console.WriteLine("[Local | Foundry Local] streaming response:");
        List<FoundryChatMessage> messages = new() { new FoundryChatMessage { Role = "user", Content = prompt } };

        var sw = Stopwatch.StartNew();
        double? ttft = null;
        int approxTokens = 0;
        var text = new StringBuilder();

        await foreach (var chunk in chatClient.CompleteChatStreamingAsync(messages, ct))
        {
            var content = chunk.Choices[0].Message.Content;
            if (string.IsNullOrEmpty(content)) continue;
            ttft ??= sw.Elapsed.TotalSeconds;
            text.Append(content);
            approxTokens += Math.Max(1, content.Split(' ', StringSplitOptions.RemoveEmptyEntries).Length);
        }
        sw.Stop();
        Console.WriteLine($"  {text}\n");

        await model.UnloadAsync();
        return new RunResult("local (Foundry Local)", ttft ?? sw.Elapsed.TotalSeconds, sw.Elapsed.TotalSeconds, approxTokens);
    }
    finally
    {
        manager.Dispose();
    }
}

RunResult? RunCloud()
{
    var endpoint = Environment.GetEnvironmentVariable("AZURE_OPENAI_ENDPOINT");
    var apiKey = Environment.GetEnvironmentVariable("AZURE_OPENAI_API_KEY");
    var deployment = Environment.GetEnvironmentVariable("AZURE_OPENAI_DEPLOYMENT");

    if (string.IsNullOrEmpty(endpoint) || string.IsNullOrEmpty(apiKey) || string.IsNullOrEmpty(deployment))
    {
        Console.WriteLine(
            "[Cloud | Azure OpenAI] skipped - set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY " +
            "and AZURE_OPENAI_DEPLOYMENT to include it.\n");
        return null;
    }

    var azureClient = new AzureOpenAIClient(new Uri(endpoint), new ApiKeyCredential(apiKey));
    var chatClient = azureClient.GetChatClient(deployment);

    Console.WriteLine("[Cloud | Azure OpenAI] streaming response:");
    var sw = Stopwatch.StartNew();
    double? ttft = null;
    int approxTokens = 0;
    var text = new StringBuilder();

    foreach (var update in chatClient.CompleteChatStreaming(new UserChatMessage(prompt)))
    {
        foreach (var part in update.ContentUpdate)
        {
            if (string.IsNullOrEmpty(part.Text)) continue;
            ttft ??= sw.Elapsed.TotalSeconds;
            text.Append(part.Text);
            approxTokens += Math.Max(1, part.Text.Split(' ', StringSplitOptions.RemoveEmptyEntries).Length);
        }
    }
    sw.Stop();
    Console.WriteLine($"  {text}\n");

    return new RunResult("cloud (Azure OpenAI)", ttft ?? sw.Elapsed.TotalSeconds, sw.Elapsed.TotalSeconds, approxTokens);
}

void PrintComparison(List<RunResult> results)
{
    Console.WriteLine("Backend               | TTFT (s) | Total (s) | ~tokens | ~tokens/s");
    Console.WriteLine("-----------------------------------------------------------------");
    foreach (var r in results)
    {
        Console.WriteLine(
            $"{r.Label,-22} | {r.TimeToFirstTokenS,8:F3} | {r.TotalTimeS,9:F3} | " +
            $"{r.ApproxTokens,7:D} | {r.ApproxTokensPerSec,9:F1}");
    }
}

record RunResult(string Label, double TimeToFirstTokenS, double TotalTimeS, int ApproxTokens)
{
    public double ApproxTokensPerSec => TotalTimeS > 0 ? ApproxTokens / TotalTimeS : 0;
}
