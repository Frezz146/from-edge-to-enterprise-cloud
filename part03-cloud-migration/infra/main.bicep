// Provisions the Foundry resource this part's agent runs against: an AIServices
// account with project management enabled, one project, one model deployment,
// and a user-assigned managed identity with the "Foundry User" role.
//
// What this template deliberately does NOT create:
//   - A Foundry Toolbox. There is no public Bicep/ARM resource type for one yet -
//     toolboxes are created through the (still-`.beta`) Foundry SDK. See
//     create_toolbox.py in this part, run once after this deployment.
//   - A role assignment for your own developer identity. The auto-grant that
//     happens when you create a project through the Foundry portal UI does not
//     apply to CLI/Bicep deployments, so that is one `az role assignment create`
//     one-liner in this part's README, not infrastructure.
//
// Deploy:
//   az deployment group create \
//     --resource-group rgalexbicep \
//     --template-file main.bicep \
//     --parameters main.bicepparam

targetScope = 'resourceGroup'

@description('Azure region for all resources. Must support Foundry AIServices accounts and the chosen model.')
param location string = 'swedencentral'

@description('Prefix used to derive resource names. A uniqueness suffix is appended automatically.')
param namePrefix string = 'e2ecloud'

@description('Name of the Foundry project created on the account.')
param projectName string = 'part03-cloud-migration'

@description('Model to deploy: format/name/version as returned by `az cognitiveservices account list-models`.')
param modelFormat string = 'OpenAI'
param modelName string = 'gpt-5-mini'
param modelVersion string = '2025-08-07'

@description('GlobalStandard throughput capacity, in units of 1,000 TPM.')
param modelCapacity int = 10

var uniqueSuffix = uniqueString(resourceGroup().id, namePrefix)
var accountName = '${namePrefix}-${uniqueSuffix}'
var identityName = '${namePrefix}-agent-identity'

// Stable across the Azure AI User -> Foundry User rename (role IDs didn't change).
// See https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  sku: {
    name: 'S0'
  }
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    // Required for the account to host Foundry projects at all.
    allowProjectManagement: true
    customSubDomainName: accountName
    disableLocalAuth: false
    dynamicThrottlingEnabled: false
    publicNetworkAccess: 'Enabled'
    restrictOutboundNetworkAccess: false
  }
}

resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: modelName
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: modelFormat
      name: modelName
      version: modelVersion
    }
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'From Edge to Enterprise Cloud - Part 3'
    description: 'Hosted agent project for the cloud migration sample.'
  }
  // The project reads the model through its parent account, so it must exist
  // before anything tries to call the deployment.
  dependsOn: [
    modelDeployment
  ]
}

resource agentIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

// Scoped to the project (least privilege): lets the identity build and run
// agents in this project without granting access to every project on the account.
resource agentIdentityFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, agentIdentity.id, foundryUserRoleId)
  scope: project
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
    principalId: agentIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output accountName string = account.name
output projectName string = project.name
output projectEndpoint string = 'https://${account.properties.customSubDomainName}.services.ai.azure.com/api/projects/${project.name}'
output modelDeploymentName string = modelDeployment.name
output agentIdentityClientId string = agentIdentity.properties.clientId
output agentIdentityPrincipalId string = agentIdentity.properties.principalId
output projectResourceId string = project.id
