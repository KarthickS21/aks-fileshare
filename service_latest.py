import os, uuid, time, traceback, logging, json, datetime
from azure.identity import ClientSecretCredential
from azure.storage.fileshare import ShareClient,ShareServiceClient, ResourceTypes, AccountSasPermissions, generate_account_sas
from azure.mgmt.storage import StorageManagementClient
from bs4 import BeautifulSoup
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
import re

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ENV variables (from AKS secret)
TENANT_ID = os.getenv("TENANT_ID",'58541453-4e85-4f05-9032-7b95cb17fd33')
CLIENT_ID = os.getenv("CLIENT_ID",'5f8b60b9-7c43-482a-875e-603e8e3a7b91')
CLIENT_SECRET = os.getenv("CLIENT_SECRET",'')
SUBSCRIPTION_ID = os.getenv("SUBSCRIPTION_ID",'fcf78033-3ec8-4642-8ea2-78e14f07e5e3')  # Needed to get storage key
RESOURCE_GROUP = os.getenv("RESOURCE_GROUP",'fileprocess-rg1')    # Needed to get storage key
STORAGE_ACCOUNT = os.getenv("STORAGE_ACCOUNT",'fileprocessorsk01')
FILE_SHARE = "testshare"
SEARCH_ENDPOINT = os.getenv("SEARCH_ENDPOINT",'https://fileprocesssearch.search.windows.net')
SEARCH_INDEX = os.getenv("SEARCH_INDEX",'reports-index')
SEARCH_KEY = os.getenv("SEARCH_KEY",'')
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))

def get_storage_key():
    """Fetch the primary storage key using the Service Principal."""
    cred = ClientSecretCredential(TENANT_ID, CLIENT_ID, CLIENT_SECRET)
    mgmt_client = StorageManagementClient(cred, SUBSCRIPTION_ID)
    keys = mgmt_client.storage_accounts.list_keys(RESOURCE_GROUP, STORAGE_ACCOUNT)
    return keys.keys[0].value

def get_storage_client():
    """Generate a SAS token using the storage account key and create ShareServiceClient."""
    logging.info("Generating SAS token for Azure File Share.")
    account_key = get_storage_key()
    sas_token = generate_account_sas(
        account_name=STORAGE_ACCOUNT,
        account_key=account_key,
        resource_types=ResourceTypes(service=True, container=True, object=True),
        permission=AccountSasPermissions(read=True, write=True, list=True, create=True, delete=True),
        expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    )
    return ShareServiceClient(
        account_url=f"https://{STORAGE_ACCOUNT}.file.core.windows.net/",
        credential=sas_token
    )

def process_file(file_path, file_client):
    import logging, datetime, uuid, traceback, json, re
    from bs4 import BeautifulSoup

    logging.info(f"Processing file: {file_path}")
    try:
        data = file_client.download_file().readall().decode("utf-8")
        env = {}

        # Try using BeautifulSoup first
        soup = BeautifulSoup(data, "html.parser")
        container = soup.find("div", {"id": "data-container"})

        json_blob = None

        if container:
            json_blob = container.get("data-jsonblob")

        if not json_blob or json_blob.strip() == "{":
            # Fallback: Use regex to find the data-jsonblob manually
            logging.warning("Falling back to regex to extract data-jsonblob")
            match = re.search(r'<div[^>]*id=["\']data-container["\'][^>]*data-jsonblob=["\'](.*?)}["\']', data, re.DOTALL)
            if match:
                json_blob = match.group(1) + "}"
            else:
                logging.warning("Regex failed to extract data-jsonblob")

        if json_blob:
            try:
                env = json.loads(json_blob)
            except Exception:
                logging.warning("Extracted data-jsonblob is not valid JSON.")
        else:
            logging.warning("data-jsonblob not found or empty.")

        timestamp = datetime.datetime.utcnow().isoformat()
        result = {
            "id": str(uuid.uuid4()),
            "timestamp": timestamp,
            "python_version": get_value(env, "Python"),
            "platform": get_value(env, "Platform"),
            "packages": [f"{k}: {v}" for k, v in get_dict(env, "Packages").items()],
            "plugins": [f"{k}: {v}" for k, v in get_dict(env, "plugins").items()],
            "playwright_platform": get_value(env, "PLATFORM"),
        }

        push_to_search(result)
        logging.info(f"File processed successfully: {file_path}")
        return "processed"

    except Exception:
        logging.error(f"Error processing file {file_path}: {traceback.format_exc()}")
        return "error"

def get_value(env: dict, key: str):
    """
    Tries to retrieve `key` from env["environment"] or directly from env.
    """
    if isinstance(env.get("environment"), dict) and key in env["environment"]:
        return env["environment"].get(key)
    return env.get(key)

def get_dict(env: dict, key: str):
    """
    Tries to retrieve a nested dictionary for things like Packages or plugins.
    """
    if isinstance(env.get("environment"), dict) and isinstance(env["environment"].get(key), dict):
        return env["environment"].get(key, {})
    return env.get(key, {})

def push_to_search(doc):
    try:
        logging.info(f"Pushing document to Azure AI Search: {doc['id']}")
        client = SearchClient(endpoint=SEARCH_ENDPOINT, index_name=SEARCH_INDEX,
                              credential=AzureKeyCredential(SEARCH_KEY))
        client.upload_documents(documents=[doc])
        logging.info("Document uploaded to search.")
    except Exception:
        logging.error(f"Failed to push document to search: {traceback.format_exc()}")

def move_file1(file_client, status):
    try:
        logging.info(f"Moving file {file_client.file_name} to {status}/ directory.")
        share_client = file_client.share_client
        dir_client = share_client.get_directory_client(status)

        try:
            dir_client.create_directory()
            logging.info(f"Created directory: {status}")
        except Exception:
            logging.info(f"Directory {status} already exists.")

        file_content = file_client.download_file().readall()
        dest_file = dir_client.get_file_client(file_client.file_name)
        dest_file.upload_file(file_content)
        file_client.delete_file()
        logging.info(f"Moved file {file_client.file_name} to {status}/")
    except Exception:
        logging.error(f"Failed moving file {file_client.file_name}: {traceback.format_exc()}")

def move_file(file_client, status):
    # Generate SAS token again (or reuse a global one)
    account_key = get_storage_key()
    sas_token = generate_account_sas(
        account_name=STORAGE_ACCOUNT,
        account_key=account_key,
        resource_types=ResourceTypes(service=True, container=True, object=True),
        permission=AccountSasPermissions(read=True, write=True, list=True, create=True, delete=True),
        expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    )

    share_client = ShareClient(
        account_url=f"https://{STORAGE_ACCOUNT}.file.core.windows.net/",
        share_name=FILE_SHARE,
        credential=sas_token
    )

    dest_dir_client = share_client.get_directory_client(status)
    try:
        dest_dir_client.create_directory()
        logging.info(f"Created directory: {status}")
    except Exception:
        logging.info(f"Directory {status} already exists.")

    dest_file = dest_dir_client.get_file_client(file_client.file_name)
    file_content = file_client.download_file().readall()
    dest_file.upload_file(file_content)
    logging.info(f"Uploaded file to {status}/ directory.")
    file_client.delete_file()
    logging.info(f"Deleted original file: {file_client.file_name}")


def main():
    logging.info("Starting main process.")
    service_client = get_storage_client()
    share_client = service_client.get_share_client(FILE_SHARE)
    dir_client = share_client.get_directory_client("folder1/folder2")

    try:
        for file in dir_client.list_directories_and_files():
            if file["name"].endswith(".html"):
                logging.info(f"Found HTML file: {file['name']}")
                file_client = dir_client.get_file_client(file["name"])
                status = process_file(f"folder1/folder2/{file['name']}", file_client)
                move_file(file_client, status)
    except Exception:
        logging.error(f"Error listing or processing files: {traceback.format_exc()}")

    logging.info("Main process completed.")

if __name__ == "__main__":
    logging.info("Starting File Processor Service")
    while True:
        main()
        #time.sleep(POLL_INTERVAL)
