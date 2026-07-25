#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

echo "🚀 Starting environment setup..."

# 1. Ensure Docker is installed
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed. Installing official Docker engine..."
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
    echo "✅ Docker is already installed."
fi

# 2. Add user to docker group if not already there
if ! groups $USER | grep &>/dev/null '\bdocker\b'; then
    echo "🔑 Adding $USER to the docker group..."
    sudo usermod -aG docker $USER
    echo "⚠️ Note: Group permissions will apply completely on your next terminal session."
else
    echo "✅ User is already in the docker group."
fi

# 3. Ensure Docker service is running in WSL
echo "🐳 Checking Docker service status..."
sudo service docker start

# 4. Generate a default .env file if it doesn't exist
if [ ! -f .env ]; then
    echo "📝 Creating default .env file..."
    echo "DATABASE_URL=postgresql://postgres_user:postgres_password@db:5432/gdc_materials_db" > .env
else
    echo "✅ .env file already exists."
fi

echo "✨ Setup complete! Spinning up containers..."
echo "------------------------------------------------"

# 5. Automatically run the app using the new group context (avoids needing a reboot)
docker compose up --build
