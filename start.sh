#!/bin/bash

echo "Starting Chronos Cinema..."

# Start backend
echo "Starting backend server..."
cd backend
source .venv/bin/activate
uvicorn main:app --reload --ws websockets-sansio --ws-ping-interval 60 --ws-ping-timeout 60 &
BACKEND_PID=$!

# Start frontend
echo "Starting frontend..."
cd ../frontend
npm run dev &
FRONTEND_PID=$!

echo "Chronos Cinema is running!"
echo "Backend: http://localhost:8000"
echo "Frontend: http://localhost:3000"
echo "Press Ctrl+C to stop all services"

# Wait for both processes
wait $BACKEND_PID $FRONTEND_PID
