## docker


docker build -t expo-assistant .

docker run -d --name expo-assistant --restart unless-stopped -p 8090:8090 expo-assistant