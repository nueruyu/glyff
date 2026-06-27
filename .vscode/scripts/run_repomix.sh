#!/bin/bash

# SCRIPT DESCRIPTION:
# This script runs repomix with a specified config file and copies the output to the clipboard.
# It is designed to be called from a VS Code task in a WSL/Linux/macOS environment.

# --- Prerequisites ---
# For JSON parsing, 'jq' must be installed.
#   sudo apt-get install jq
# For clipboard access on Linux Desktop, 'xclip' must be installed.
#   sudo apt-get install xclip
# On WSL, clip.exe is available by default.
# On macOS, pbcopy is available by default.

# Function to check for the presence of a command.
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Acknowledge the config file path passed as an argument.
if [ -z "$1" ]; then
    echo "ERROR: Config file path is not provided."
    echo "Usage: ./run_repomix.sh <path_to_config>"
    exit 1
fi

CONFIG_PATH="$1"

# Determine the project's root directory based on the script's location.
# Assumes this script is in a subdirectory like .vscode/scripts.
if command_exists realpath; then
    SCRIPT_PATH=$(realpath "$0")
else
    SCRIPT_DIR_FALLBACK=$(cd "$(dirname "$0")" && pwd -P)
    SCRIPT_PATH="$SCRIPT_DIR_FALLBACK/$(basename "$0")"
fi

SCRIPT_DIR=$(dirname "$SCRIPT_PATH")
PROJECT_ROOT=$(dirname "$(dirname "$SCRIPT_DIR")")

echo "--------------------------------------------------"
echo "Starting Repomix Task..."
echo "Using config file: $CONFIG_PATH"

# STEP 1: Read the JSON config file to find the output file name.
if ! command_exists jq; then
    echo "ERROR: 'jq' is not installed. Please install it to parse the JSON config."
    exit 1
fi

# Check if the config file exists before trying to read it.
if [ ! -f "$CONFIG_PATH" ]; then
    echo "ERROR: Config file not found at: $CONFIG_PATH"
    exit 1
fi

OUTPUT_FILE_NAME=$(jq -r '.output.filePath' "$CONFIG_PATH")

if [ -z "$OUTPUT_FILE_NAME" ] || [ "$OUTPUT_FILE_NAME" == "null" ]; then
    echo "ERROR: '.output.filePath' not found in the config file."
    exit 1
fi

OUTPUT_FILE_PATH="$PROJECT_ROOT/$OUTPUT_FILE_NAME"

echo "Output file will be: $OUTPUT_FILE_PATH"

# STEP 2: Execute the repomix command.
echo "Running repomix..."
# Change directory to project root to ensure correct relative path resolution.
(cd "$PROJECT_ROOT" && npx repomix --config "$CONFIG_PATH")

# Check if the command executed successfully.
if [ $? -ne 0 ]; then
    echo "ERROR: repomix command failed."
    exit 1
fi

echo "Repomix execution finished."

# STEP 3: Copy the output file to the clipboard.
if [ -f "$OUTPUT_FILE_PATH" ]; then
    echo "Copying output file to clipboard..."

    # Use clip.exe if on WSL, otherwise fallback to xclip.
    if command_exists clip.exe; then
        cat "$OUTPUT_FILE_PATH" | clip.exe
        echo "SUCCESS: Output file '$OUTPUT_FILE_NAME' has been copied to the Windows clipboard."
    elif command_exists pbcopy; then
        pbcopy < "$OUTPUT_FILE_PATH"
        echo "SUCCESS: Output file '$OUTPUT_FILE_NAME' has been copied to the clipboard."
    elif command_exists xclip; then
        cat "$OUTPUT_FILE_PATH" | xclip -selection clipboard
        echo "SUCCESS: Output file '$OUTPUT_FILE_NAME' has been copied to the clipboard."
    else
        echo "WARNING: Could not find 'clip.exe', 'pbcopy', or 'xclip'. File content not copied to clipboard."
        echo "File content is available at: $OUTPUT_FILE_PATH"
    fi
    echo "--------------------------------------------------"
else
    echo "ERROR: Output file not found at: $OUTPUT_FILE_PATH"
    exit 1
fi
