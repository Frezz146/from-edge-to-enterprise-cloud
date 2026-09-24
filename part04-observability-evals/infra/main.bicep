// Extends the Part 3 deployment with everything the Part 4 quality gate and
// tracing need. Part 3's account, project and managed identity are referenced
// as `existing` resources, never recreated: this template derives their names
// with the same uniqueString() formula Part 3 used, so the only thing to keep in
// sync is `namePrefix`.
//
// What it adds:
//   1. A Log Analytics workspace and a workspace-based Application Insights
//      resource with local (ingestion key) authentication disabled, so every
//      span has to arrive with a Microsoft Entra ID token.
//   2. The Application Insights connection on the Foundry account and project.
//      This is what switches on tracing for agents Foundry runs. It is also where
//      FoundryChatClient.configure_azure_monitor() (python/traced_agent.py) and
//      python/telemetry.py read the connection string from.
//   3. Two OIDC federated credentials on the Part 3 identity, so GitHub Actions
//      signs in as that identity without a stored secret.
//   4. Least-privilege role assignments, one per job to be done (see below).
//
// Deploy (same resource group as Part 3):
//   az deployment group create \
//     --resource-group <your-resource-group> \
//     --template-file main.bicep \
//     --parameters main.bicepparam \
//     --parameters developerPrincipalId=$(az ad signed-in-user show --query id -o tsv)

targetScope = 'resourceGroup'

@description('Azure region for the new monitoring resources. Use the Part 3 region.')
param location string = 'swedencentral'

@description('Same prefix as the Part 3 deployment. Used to derive the Part 3 resource names.')
param namePrefix string = 'e2ecloud'

@description('Name of the Part 3 Foundry project.')
param projectName string = 'part03-cloud-migration'

@description('GitHub repository (owner/name) that is allowed to sign in as the agent identity via OIDC.')
param githubRepository string = 'Frezz146/from-edge-to-enterprise-cloud'

@description('Optional object ID of your own Entra ID user, for running the gate and reading traces locally. Leave empty to skip.')
param developerPrincipalId string = ''

@description('Disable ingestion key authentication on Application Insights, so only Entra ID tokens are accepted.')
param disableLocalAuth bool = true

@description('Log Analytics retention in days.')
@minValue(30)
param retentionInDays int = 30

// --- Names (Part 3 formula, do not change independently) -----------------------

var uniqueSuffix = uniqueString(resourceGroup().id, namePrefix)
var accountName = '${namePrefix}-${uniqueSuffix}'
var identityName = '${namePrefix}-agent-identity'
var workspaceName = '${namePrefix}-logs-${uniqueSuffix}'
var appInsightsName = '${namePrefix}-appi-${uniqueSuffix}'

// --- Built-in role definition IDs -----------------------------------------------
// https://learn.microsoft.com/azure/role-based-access-control/built-in-roles

// Write telemetry with an Entra ID token (required once local auth is disabled).
var monitoringMetricsPublisherRoleId = '3913510d-42f4-4e42-8a64-420c390055eb'
// Read and query telemetry. For people and for the project identity, not for CI.
var logAnalyticsReaderRoleId = '73c42c96-874c-492b-b04d-ab87d138a893'
// Read GenAI content in protected tables. ID taken from Microsoft's
// foundry-samples connection-application-insights.bicep.
var privilegedMonitoringDataReaderRoleId = 'dbc9c667-e97f-4491-aee6-90b9cf960190'
// Call model deployments through the Azure OpenAI endpoint with Entra ID
// (cloud backend and LLM judge in python/eval_pipeline.py).
var cognitiveServicesOpenAIUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

// --- Part 3 resources -------------------------------------------------------------

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: account
  name: projectName
}

resource agentIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

// --- 1. Log Analytics and Application Insights ------------------------------------

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    DisableLocalAuth: disableLocalAuth
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// --- 2. Foundry connections (account and project) ----------------------------------
// Same shape as Microsoft's foundry-samples template. The SDKs read the
// connection string from `credentials.key` (azure-ai-projects rejects any other
// auth type for this lookup). With DisableLocalAuth the connection string is
// only an address: it names the resource, but ingestion still needs a token.
// Only one Application Insights connection can be set on a project at a time.

resource accountConnection 'Microsoft.CognitiveServices/accounts/connections@2025-06-01' = {
  parent: account
  name: '${accountName}-appinsights'
  properties: {
    category: 'AppInsights'
    target: appInsights.id
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: appInsights.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: appInsights.id
    }
  }
}

resource projectConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: appInsightsName
  properties: {
    category: 'AppInsights'
    target: appInsights.id
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: appInsights.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: appInsights.id
    }
  }
}

// --- 3. GitHub Actions OIDC federated credentials -----------------------------------
// One subject per trigger: pull requests (the quality gate) and pushes to main.
// Federated credentials on one identity cannot be written in parallel, hence
// the explicit dependsOn.

resource githubPullRequest 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: agentIdentity
  name: 'github-pull-request'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: 'repo:${githubRepository}:pull_request'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

resource githubMain 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: agentIdentity
  name: 'github-main'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: 'repo:${githubRepository}:ref:refs/heads/main'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
  dependsOn: [
    githubPullRequest
  ]
}

// --- 4. Role assignments --------------------------------------------------------------

// Agent identity (the app in production, the CI runner via OIDC):
// write spans, call models for the cloud gate and the judge. No read access
// to telemetry: a pipeline that only writes should not be able to read.
resource agentMetricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, agentIdentity.id, monitoringMetricsPublisherRoleId)
  scope: appInsights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringMetricsPublisherRoleId)
    principalId: agentIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource agentOpenAIUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, agentIdentity.id, cognitiveServicesOpenAIUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAIUserRoleId)
    principalId: agentIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Project managed identity: Foundry's own server-side tracing writes with this
// identity once local auth is disabled. Foundry evaluations read traces back with it.
var projectReaderRoles = [
  monitoringMetricsPublisherRoleId
  logAnalyticsReaderRoleId
  privilegedMonitoringDataReaderRoleId
]

resource projectMonitoringRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in projectReaderRoles: {
    name: guid(appInsights.id, project.id, roleId)
    scope: appInsights
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: project.identity.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

// Optional: you, for running eval_pipeline.py and traced_agent.py locally with
// az login and for browsing traces in the portal.
var developerAppInsightsRoles = [
  monitoringMetricsPublisherRoleId
  logAnalyticsReaderRoleId
]

resource developerMonitoringRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in developerAppInsightsRoles: if (!empty(developerPrincipalId)) {
    name: guid(appInsights.id, developerPrincipalId, roleId)
    scope: appInsights
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: developerPrincipalId
      principalType: 'User'
    }
  }
]

resource developerOpenAIUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(developerPrincipalId)) {
  name: guid(account.id, developerPrincipalId, cognitiveServicesOpenAIUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAIUserRoleId)
    principalId: developerPrincipalId
    principalType: 'User'
  }
}

// --- Outputs ---------------------------------------------------------------------------
// Everything GitHub Actions needs is an ID, not a secret: store these as
// repository *variables*.

output appInsightsName string = appInsights.name
output workspaceName string = workspace.name
output openAiEndpoint string = 'https://${account.properties.customSubDomainName}.openai.azure.com'
output azureClientId string = agentIdentity.properties.clientId
output azureTenantId string = tenant().tenantId
output azureSubscriptionId string = subscription().subscriptionId
