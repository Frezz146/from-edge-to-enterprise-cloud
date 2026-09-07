// Local inference loop: download, load, and chat with a small language
// model entirely in-process using the Foundry Local SDK's native chat
// completions API.
//
// No REST server, no cloud endpoint, no per-token cost - every request in
// this file is served by execution providers running on your own machine.

using Betalgo.Ranul.OpenAI.ObjectModels.RequestModels;
using Microsoft.AI.Foundry.Local;
using Microsoft.Extensions.Logging.Abstractions;

const string modelAlias = "qwen2.5-0.5b";
const string appName = "frezz_tech_edge_agent";
const string userPrompt = "Describe the advantages of developer-local AI models in three bullets.";

CancellationToken ct = CancellationToken.None;

// 1. Initialize the singleton runtime for this process.
Console.WriteLine("[System] Initializing in-process Foundry Local runtime...");
await FoundryLocalManager.CreateAsync(
    new Configuration { AppName = appName },
    NullLogger.Instance);

var manager = FoundryLocalManager.Instance;
try
{
    // 2. Discover and register hardware-specific execution providers (NPU/GPU/CPU).
    Console.WriteLine("\n[Hardware] Registering hardware-specific execution providers...");
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

    // 3. Resolve the model alias and cache the hardware-optimized variant.
    var catalog = await manager.GetCatalogAsync();
    var model = await catalog.GetModelAsync(modelAlias)
        ?? throw new InvalidOperationException($"Model '{modelAlias}' not found in the catalog.");

    Console.WriteLine($"\n[Model] Preparing resources for '{modelAlias}'...");
    if (!await model.IsCachedAsync())
    {
        Console.Write($"[Model] Downloading '{modelAlias}' weights from the cloud-hosted catalog...");
        await model.DownloadAsync(progress =>
            Console.Write($"\r[Model] Download Progress: {progress:F1}%"));
        Console.WriteLine();
    }
    else
    {
        Console.WriteLine("[Model] Found cached weights. Skipping download.");
    }

    // 4. Load the model into memory.
    Console.WriteLine("[Model] Loading model into memory...");
    await model.LoadAsync();

    // 5. Get a native chat client and run a streaming completion in-process.
    var chatClient = await model.GetChatClientAsync();
    // Enforce deterministic output so repeated runs are directly comparable.
    chatClient.Settings.Temperature = 0.0f;
    chatClient.Settings.MaxTokens = 256;

    List<ChatMessage> messages = new()
    {
        new ChatMessage { Role = "user", Content = userPrompt }
    };

    Console.WriteLine($"\n[User]: {userPrompt}\n");
    Console.Write("[Assistant]: ");
    await foreach (var chunk in chatClient.CompleteChatStreamingAsync(messages, ct))
    {
        Console.Write(chunk.Choices[0].Message.Content);
    }
    Console.WriteLine("\n");

    // 6. Graceful cleanup.
    Console.WriteLine("[System] Unloading model and releasing system resources...");
    await model.UnloadAsync();
    Console.WriteLine("[System] Local inference loop completed successfully.");
}
finally
{
    manager.Dispose();
}
