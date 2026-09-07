// The C# counterpart to cloud_agent.py: create a hosted agent in Foundry
// Agent Service, run one turn, print the response, and clean up.
//
// Function-tool wiring (mirroring the get_weather/calculate tools from
// Parts 1-2) follows the same FunctionToolDefinition + RequiresAction poll
// loop pattern documented at
// https://learn.microsoft.com/dotnet/api/overview/azure/ai.agents.persistent-readme
// - see cloud_agent.py in this part for the equivalent, fully worked example.
//
// Required environment variables:
//   ProjectEndpoint       e.g. https://<account>.services.ai.azure.com/api/projects/<project>
//   ModelDeploymentName   e.g. gpt-4o-mini

using Azure.AI.Agents.Persistent;
using Azure.Identity;

var projectEndpoint = Environment.GetEnvironmentVariable("ProjectEndpoint")
    ?? throw new InvalidOperationException("ProjectEndpoint is not set.");
var modelDeploymentName = Environment.GetEnvironmentVariable("ModelDeploymentName")
    ?? throw new InvalidOperationException("ModelDeploymentName is not set.");

// Identity is the one thing that has no local equivalent: a local Foundry
// Local process never crossed an auth boundary, but every call here does.
PersistentAgentsClient client = new(projectEndpoint, new DefaultAzureCredential());

PersistentAgent agent = client.Administration.CreateAgent(
    model: modelDeploymentName,
    name: "from-edge-to-enterprise-agent",
    instructions: "You are a helpful assistant.");
Console.WriteLine($"Created agent, ID: {agent.Id}");

PersistentAgentThread thread = client.Threads.CreateThread();
Console.WriteLine($"Created thread, ID: {thread.Id}");

const string question = "In two sentences, what changes when you move an AI agent from local inference to a hosted cloud agent service?";
client.Messages.CreateMessage(thread.Id, MessageRole.User, question);
Console.WriteLine($"[User]: {question}");

ThreadRun run = client.Runs.CreateRun(thread.Id, agent.Id);
do
{
    Thread.Sleep(TimeSpan.FromMilliseconds(500));
    run = client.Runs.GetRun(thread.Id, run.Id);
}
while (run.Status == RunStatus.Queued || run.Status == RunStatus.InProgress || run.Status == RunStatus.RequiresAction);

if (run.Status == RunStatus.Failed)
{
    Console.WriteLine($"Run failed: {run.LastError?.Message}");
}

foreach (PersistentThreadMessage message in client.Messages.GetMessages(threadId: thread.Id, order: ListSortOrder.Ascending))
{
    foreach (MessageContent content in message.ContentItems)
    {
        if (content is MessageTextContent textItem)
        {
            Console.WriteLine($"[{message.Role}]: {textItem.Text}");
        }
    }
}

// Clean up - comment these out if you want to inspect the agent/thread in the portal.
client.Threads.DeleteThread(threadId: thread.Id);
client.Administration.DeleteAgent(agentId: agent.Id);
Console.WriteLine("Cleaned up thread and agent.");
