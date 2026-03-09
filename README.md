# Chronos Cinema

Immersive AI Documentary Agent.

## Overview

Chronos Cinema is a multimodal AI agent that generates a real-time, cinematic, and interactive educational experience.

## Tech Stack

- **Frontend:** Next.js, Tailwind CSS
- **Backend:** Python FastAPI, WebSockets
- **AI:** Google Gemini (Pro & Live)

## Setup

### Prerequisites

- Node.js (v18+)
- Python (v3.10+)
- Google GenAI API Key

### Backend

1.  Navigate to `backend/`:
    ```bash
    cd backend
    ```
2.  Create a virtual environment:
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```
3.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
4.  Create a `.env` file with your API key:
    ```
    GOOGLE_API_KEY=your_api_key_here
    ```
5.  Run the server:
    ```bash
    uvicorn main:app --reload
    ```

### Frontend

1.  Navigate to `frontend/`:
    ```bash
    cd frontend
    ```
2.  Install dependencies:
    ```bash
    npm install
    ```
3.  Run the development server:
    ```bash
    npm run dev
    ```

## Usage

1.  Open `http://localhost:3000`.
2.  Click "Connect to Agent".
3.  Type a message to chat with the agent.

## License

MIT
