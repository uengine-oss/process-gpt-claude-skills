#!/bin/bash

echo "========================================="
echo "Claude Skills MCP Backend Docker Test"
echo "========================================="
echo ""

# Check if docker-compose is running
echo "1. Checking container status..."
docker-compose ps
echo ""

# Check health endpoint
echo "2. Testing Health Check endpoint..."
curl -s http://localhost:8765/health | python3 -m json.tool
echo ""

# Check recent logs
echo "3. Recent logs (last 20 lines)..."
docker logs --tail=20 claude-skills
echo ""

# Check resource usage
echo "4. Container resource usage..."
docker stats --no-stream claude-skills
echo ""

echo "========================================="
echo "Test completed!"
echo "========================================="
echo ""
echo "Useful commands:"
echo "  - View logs: docker-compose logs -f"
echo "  - Stop: docker-compose down"
echo "  - Restart: docker-compose restart"
echo "  - Rebuild: docker-compose build --no-cache"
echo ""

