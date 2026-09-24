using 'main.bicep'

// Must match part03-cloud-migration/infra/main.bicepparam
param location = 'swedencentral'
param namePrefix = 'e2ecloud'
param projectName = 'part03-cloud-migration'

param githubRepository = 'Frezz146/from-edge-to-enterprise-cloud'
// GitHub presents this repository's OIDC subject in the immutable format
// (owner and repository with their numeric IDs); see main.bicep.
param githubOwnerId = '115118426'
param githubRepositoryId = '1360166622'
param disableLocalAuth = true
param retentionInDays = 30

// Pass your own object ID on the command line instead of committing it:
//   --parameters developerPrincipalId=$(az ad signed-in-user show --query id -o tsv)
param developerPrincipalId = ''
