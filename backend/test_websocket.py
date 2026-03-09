import asyncio
import websockets
import json

async def test_websocket():
    uri = "ws://localhost:8000/ws"
    async with websockets.connect(uri) as websocket:
        print("Connected to WebSocket")
        
        message = "Hello Chronos"
        await websocket.send(message)
        print(f"Sent: {message}")
        
        response = await websocket.recv()
        print(f"Received: {response}")
        
        data = json.loads(response)
        assert data["type"] == "text"
        assert data["content"] == f"Chronos Agent says: I heard '{message}'"
        print("Test Passed!")

if __name__ == "__main__":
    asyncio.run(test_websocket())
