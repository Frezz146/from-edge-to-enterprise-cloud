// The C# counterpart to cloud_agent.py: create a hosted agent in Foundry Agent
// Service, run two turns on a server-managed conversation, and print the
// responses.
//
// Function-tool wiring is no longer C#-specific plumbing to avoid duplicating:
// Microsoft.Agents.AI.AzureAI's AsAIAgent()/RunAsync() puts C# on the same
// Agent Framework surface as agent_framework.foundry in Python, so the same
// get_weather/calculate tools attach here the same way - AIFunctionFactory.Create
// reads the [Description] attributes the same way Python reads docstrings and
// type hints. What Python additionally demonstrates - the Foundry Toolbox and
// declarative, versioned agent publishing - stays Python-only in this part; see
// cloud_agent.py and this part's README for why.
//
// Required environment variables:
//   ProjectEndpoint       e.g. https://<account>.services.ai.azure.com/api/projects/<project>
//   ModelDeploymentName   e.g. gpt-5-mini

using System.ComponentModel;
using Azure.AI.Projects;
using Azure.Identity;
using Microsoft.Agents.AI;
using Microsoft.Extensions.AI;

var projectEndpoint = Environment.GetEnvironmentVariable("ProjectEndpoint")
    ?? throw new InvalidOperationException("ProjectEndpoint is not set.");
var modelDeploymentName = Environment.GetEnvironmentVariable("ModelDeploymentName")
    ?? throw new InvalidOperationException("ModelDeploymentName is not set.");

[Description("Get the current weather for a location.")]
static string GetWeather(
    [Description("The city or location to look up.")] string location,
    [Description("Temperature unit, either celsius or fahrenheit.")] string unit = "celsius")
{
    var temperature = unit == "celsius" ? 18 : 64;
    return $"{{\"location\": \"{location}\", \"temperature\": {temperature}, \"unit\": \"{unit}\", \"condition\": \"Partly cloudy\"}}";
}

[Description("Evaluate a simple arithmetic expression, e.g. \"42 * 17\".")]
static string Calculate(
    [Description("An arithmetic expression using +, -, *, / and numbers.")] string expression)
{
    var allowed = "0123456789+-*/(). ".ToHashSet();
    if (!expression.All(allowed.Contains))
    {
        return "{\"error\": \"Invalid expression\"}";
    }
    try
    {
        var result = new System.Data.DataTable().Compute(expression, null);
        return $"{{\"expression\": \"{expression}\", \"result\": {result}}}";
    }
    catch (Exception ex)
    {
        return $"{{\"error\": \"{ex.Message}\"}}";
    }
}

// Identity is the one thing that has no local equivalent: a local Foundry
// Local process never crossed an auth boundary, but every call here does.
AIProjectClient projectClient = new(new Uri(projectEndpoint), new DefaultAzureCredential());

AIFunction[] tools = [AIFunctionFactory.Create(GetWeather), AIFunctionFactory.Create(Calculate)];

AIAgent agent = projectClient.AsAIAgent(
    modelDeploymentName,
    name: "from-edge-to-enterprise-agent",
    instructions: "You are a helpful assistant with access to tools. Use them when needed to answer questions accurately.",
    tools: tools);

// Server-managed conversation: the app only ever holds this opaque ID.
ChatClientAgentSession session = (ChatClientAgentSession)await agent.CreateSessionAsync();

const string question = "What's the weather in Tokyo, and what is 42 * 17?";
Console.WriteLine($"[User]: {question}");
AgentResponse response = await agent.RunAsync(question, session);
Console.WriteLine($"[{agent.Name}]: {response.Text}");
Console.WriteLine($"Server-side conversation ID: {session.ConversationId}");

const string followUp = "What was the second thing I just asked you to calculate?";
Console.WriteLine($"[User]: {followUp}");
AgentResponse response2 = await agent.RunAsync(followUp, session);
Console.WriteLine($"[{agent.Name}]: {response2.Text}");
