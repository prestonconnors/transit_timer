import os
import requests
import zipfile
from datetime import datetime

# URLs of GTFS files
GTFS_URLS = [
    "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip",
    "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_m.zip"
]

# Function to get remote file's last modified time
def get_remote_last_modified(url):
    print(f"Checking Last-Modified header for {url}")
    response = requests.head(url)
    if "Last-Modified" in response.headers:
        last_modified = datetime.strptime(response.headers["Last-Modified"], "%a, %d %b %Y %H:%M:%S %Z")
        print(f"Last-Modified for {url}: {last_modified}")
        return last_modified
    return None

# Function to get local file's last modified time
def get_local_last_modified(file_path):
    if os.path.exists(file_path):
        return datetime.utcfromtimestamp(os.path.getmtime(file_path))
    return None

# Function to download and extract file
def download_and_extract(url, file_name):
    print(f"Downloading {file_name}...")
    response = requests.get(url, stream=True)
    with open(file_name, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    print(f"Downloaded {file_name}.")

    # Create a directory for the extracted files
    extract_dir = os.path.splitext(file_name)[0]  # Remove .zip extension
    os.makedirs(extract_dir, exist_ok=True)

    # Extract the ZIP file into its own directory
    with zipfile.ZipFile(file_name, "r") as zip_ref:
        zip_ref.extractall(extract_dir)
    print(f"Extracted {file_name} to {extract_dir}.")

# Main script execution
for url in GTFS_URLS:
    file_name = os.path.basename(url)
    remote_time = get_remote_last_modified(url)
    local_time = get_local_last_modified(file_name)
    
    if remote_time and (local_time is None or remote_time > local_time):
        download_and_extract(url, file_name)
    else:
        print(f"{file_name} is already up to date.")
