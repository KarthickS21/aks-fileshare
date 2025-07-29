# Infrastructure Setup for File Processor on AKS

## 1. Create Resource Group

```bash
az group create \
    --name fileprocess-rg1 \
    --location eastus \
    --subscription fcf78033-3ec8-4642-8ea2-78e14f07e5e3
```

## 2. Create Storage Account and File Share

> **Note:** Storage account name must be globally unique.

```bash
az storage account create \
    --name fileprocessorsk01 \
    --resource-group fileprocess-rg1 \
    --location eastus \
    --sku Standard_LRS \
    --kind StorageV2

# Create a file share for HTML processing
az storage share-rm create \
    --resource-group fileprocess-rg1 \
    --storage-account fileprocessorsk01 \
    --name file-share
```

## 3. Create Azure AI Search Service

```bash
az search service create \
    --name fileprocesssearch \
    --resource-group fileprocess-rg1 \
    --sku basic \
    --partition-count 1 \
    --replica-count 1 \
    --location eastus
```

**Fetch the admin key:**

```bash
az search admin-key show \
    --service-name fileprocesssearch \
    --resource-group fileprocess-rg1
```

**Create the index using `index.json`:**

```bash
az rest --method PUT \
    --uri "https://fileprocesssearch.search.windows.net/indexes/reports-index?api-version=2023-11-01" \
    --headers "Content-Type=application/json" "api-key:<primary-key>" \
    --body @index.json
```

## 4. Create Service Principal and Assign Roles

**Create SP for AKS to access Storage:**

```bash
az ad sp create-for-rbac \
    --name fileprocess-sp \
    --role "Storage Blob Data Contributor" \
    --scopes /subscriptions/fcf78033-3ec8-4642-8ea2-78e14f07e5e3/resourceGroups/fileprocess-rg1/providers/Microsoft.Storage/storageAccounts/fileprocessorsk01
```

**Grant additional permission to retrieve account keys (for SAS generation):**

```bash
az role assignment create \
    --assignee <SP_APP_ID> \
    --role "Storage Account Key Operator Service Role" \
    --scope /subscriptions/fcf78033-3ec8-4642-8ea2-78e14f07e5e3/resourceGroups/fileprocess-rg1/providers/Microsoft.Storage/storageAccounts/fileprocessorsk01
```

## 5. Container Registry & AKS Setup

**Create ACR:**

```bash
az acr create \
    --resource-group fileprocess-rg1 \
    --name fileprocessacr \
    --sku Basic
```

**Create AKS cluster and integrate with ACR:**

```bash
az aks create \
    --resource-group fileprocess-rg1 \
    --name fileprocess-aks \
    --node-count 1 \
    --enable-managed-identity \
    --attach-acr fileprocessacr

az aks get-credentials --resource-group fileprocess-rg1 --name fileprocess-aks
```

## 6. Build & Push Docker Image

**For macOS (ensuring amd64 build):**

```bash
docker buildx build \
    --platform linux/amd64 \
    -t fileprocessacr.azurecr.io/fileprocessor:latest \
    --push .
```

**Verify the image is pushed:**

```bash
az acr repository list --name fileprocessacr
az acr repository show-tags --name fileprocessacr --repository fileprocessor
```

## 7. Create Kubernetes Secrets for Service Principal

```bash
kubectl create secret generic sp-secrets \
    --from-literal=TENANT_ID=58541453-4e85-4f05-9032-7b95cb17fd33 \
    --from-literal=CLIENT_ID=5f8b60b9-7c43-482a-875e-603e8e3a7b91 \
    --from-literal=CLIENT_SECRET=xxxxx \
    --from-literal=SUBSCRIPTION_ID=fcf78033-3ec8-4642-8ea2-78e14f07e5e3 \
    --from-literal=RESOURCE_GROUP=fileprocess-rg1 \
    --from-literal=STORAGE_ACCOUNT=fileprocessorsk01 \
    --from-literal=SEARCH_ENDPOINT=https://fileprocesssearch.search.windows.net \
    --from-literal=SEARCH_INDEX=reports-index \
    --from-literal=SEARCH_KEY=<your-search-key>
```

**Confirm:**

```bash
kubectl get secret sp-secrets -o yaml
```

## 8. Deploy Python App

```bash
kubectl apply -f deployment.yaml
kubectl get deployments
kubectl get pods
```

**Restart if needed:**

```bash
kubectl rollout restart deployment/fileprocessor
kubectl get pods
```

## 9. Debugging & Logs

**Check logs:**

```bash
kubectl logs -f <pod-name>
```

**If you see ImagePullBackOff, re-attach ACR:**

```bash
az aks update \
    --name fileprocess-aks \
    --resource-group fileprocess-rg1 \
    --attach-acr fileprocessacr

kubectl rollout restart deployment/fileprocessor
```