# **PRD: Project "Chronos Cinema" — Immersive AI Documentary Agent**

## **1\. Project Overview**

**Chronos Cinema** is a multimodal AI agent that generates a real-time, cinematic, and interactive educational experience. It takes a complex scientific or historical topic, generates a narrated documentary with interleaved 4K imagery, and concludes with an interactive voice-driven quiz.

* **Category:** Creative Storyteller ✍️ (Primary) / Live Agent 🗣️ (Secondary)  
* **Core Value:** Transforms passive learning into a high-fidelity, interactive "Documentary-as-a-Service."

---

## **2\. Technical Stack (Mandatory Requirements)**

| Layer | Technology | Specification |
| :---- | :---- | :---- |
| **LLM Brain** | **Gemini 3.1 Pro** | gemini-3.1-pro-preview for high-level scene planning and quiz logic. |
| **Live Interface** | **Gemini Live 2.5 Flash** | gemini-live-2.5-flash-native-audio for real-time narration and barge-in quiz handling. |
| **Visual Engine** | **Nano Banana 2** | gemini-3.1-flash-image-preview for 4K interleaved image generation. |
| **SDK** | **Google GenAI SDK** | Version 2026 (Python/JS) or **ADK (Agent Development Kit)**. |
| **Cloud Hosting** | **Google Cloud Run** | Backend containerization (Mandatory GCP component). |
| **Frontend** | **Next.js (Vercel)** | Real-time streaming UI using Web Audio API for PCM playback. |
| **Database** | **Firebase Firestore** | Session persistence and quiz leaderboards. |

---

## **3\. System Architecture**

### **Data Flow:**

1. **Orchestration:** Backend (Cloud Run) initializes a LiveSession with Gemini.  
2. **Multimodal Stream:** The Agent outputs **Audio** (PCM 24kHz) and **Text** simultaneously.  
3. **Interleaved Generation:** When the script reaches a "Visual Cue," the agent calls the generate\_image tool (Nano Banana 2).  
4. **Frontend Sync:** Next.js receives a JSON event stream. It plays audio while cross-fading images as they arrive.

---

## **4\. Feature Requirements**

### **Phase 1: The "Director" (Documentary Generation)**

* **Scene Planning:** The agent must break the topic into 3-5 distinct "Scenes."  
* **Visual Consistency:** All generated images must share a "Master Style" (e.g., "National Geographic Photography" or "Microscopic 3D Render").  
* **Search Grounding:** Use Google Search tool to ensure scientific facts are current (e.g., "The latest 2026 data on Mars' water ice").

### **Phase 2: The "Tutor" (Interactive Quiz)**

* **Seamless Transition:** Once the documentary ends, the agent must change its persona to "The Inquisitor."  
* **Barge-in Logic:** The user must be able to interrupt the AI during its explanation of a question.  
* **Visual Feedback:** If a user misses a question, the agent should generate a "Recap Diagram" using Nano Banana 2 to explain the concept visually.

---

## **5\. Development Roadmap (3-5 Day Sprint)**

### **Day 1: Foundation & Live API**

* Initialize FastAPI backend and deploy a "Hello World" to **Google Cloud Run**.  
* Establish WebSocket connection between Next.js and Gemini Live API.  
* **Goal:** User says "Hi," and the AI responds with its native voice in the browser.

### **Day 2: Multimodal Interleaving**

* Implement the **Scene Planner** using Gemini 3.1 Pro.  
* Integrate **Nano Banana 2** calls within the Live stream.  
* **Goal:** A 3-scene documentary plays with images appearing in sync with audio.

### **Day 3: Quiz & State Management**

* Develop the "Quiz Mode" state machine.  
* Implement Firebase Firestore to save "Documentary Replays."  
* **Goal:** Fully functional loop: Topic → Documentary → Quiz → Scoreboard.

### **Day 4: Polish & Proof**

* Record the "Proof of GCP Deployment" (Screen record the Cloud Run console).  
* Create the Architecture Diagram (using Mermaid.js or Lucidchart).  
* Finalize README with docker-compose or gcloud deploy instructions.

---

## **6\. Submission Assets Checklist**

* \[ \] **Text Description:** "Chronos Cinema" summary and technical findings.  
* \[ \] **Public Repo:** GitHub link with README.md.  
* \[ \] **GCP Proof:** Short video of Cloud Run logs/deployment.  
* \[ \] **Architecture Diagram:** PDF/PNG of the system flow.  
* \[ \] **Demo Video:** \<4 minutes (Documentary generation \+ live voice quiz).

