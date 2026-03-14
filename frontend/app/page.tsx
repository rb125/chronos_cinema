"use client";

import {
  useEffect,
  useRef,
  useState,
  useCallback,
} from "react";
import { AudioEngine } from "./lib/audioEngine";

// ─── Types ───────────────────────────────────────────────────────────────────

type Phase = "idle" | "loading" | "playing" | "quiz" | "done";
type VideoSize = "default" | "theater";

interface Visual {
  type: "video" | "image";
  data: string; // base64
  mimeType: string;
  beatIndex: number;
}

interface QuizQuestion {
  question: string;
  options: string[];
  correct: string; // "A" | "B" | "C" | "D"
  explanation: string;
}

interface QuizState {
  questions: QuizQuestion[];
  currentIdx: number;
  answers: (string | null)[];
  score: number;
  showExplanation: boolean;
  topic: string;
}

interface HistoryEntry {
  id: string;
  topic: string;
  date: string; // ISO string
  score?: number;
  total?: number;
}

// ─── Constants ───────────────────────────────────────────────────────────────

const WS_URL = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000/ws";
const HISTORY_KEY = "chronos_history";

const BEAT_LABELS = [
  "Hook",
  "Foundation",
  "Mechanism",
  "Scale",
  "Counterintuitive",
  "Human Connection",
  "Frontier",
  "Reflection",
];

// ─── Main Component ──────────────────────────────────────────────────────────

export default function ChronosCinema() {
  // Core state
  const [phase, setPhase] = useState<Phase>("idle");
  const [topic, setTopic] = useState("");
  const [userName, setUserName] = useState("");
  const [docTitle, setDocTitle] = useState("");
  const [beatDuration, setBeatDuration] = useState(35);

  // Cinema state
  const [currentBeat, setCurrentBeat] = useState(-1);
  const [totalBeats, setTotalBeats] = useState(8);
  const [subtitles, setSubtitles] = useState<string[]>([]);
  const [statusLog, setStatusLog] = useState<string[]>([]);
  const [currentVisual, setCurrentVisual] = useState<Visual | null>(null);
  const [bgmActive, setBgmActive] = useState(false);
  const [visualByBeat, setVisualByBeat] = useState<Record<number, Visual>>({});

  // Playback controls
  const [isPaused, setIsPaused] = useState(false);
  const [videoSize, setVideoSize] = useState<VideoSize>("default");
  const [showStatusLog, setShowStatusLog] = useState(false);

  // History
  const [history, setHistory] = useState<HistoryEntry[]>([]);

  // Quiz state
  const [quiz, setQuiz] = useState<QuizState | null>(null);

  // Mic / voice
  const [micEnabled, setMicEnabled] = useState(false);

  // Refs
  const wsRef = useRef<WebSocket | null>(null);
  const audioEngineRef = useRef<AudioEngine | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const subtitleScrollRef = useRef<HTMLDivElement>(null);
  const statusLogRef = useRef<HTMLDivElement>(null);
  const chatInputRef = useRef<HTMLInputElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const cinemaFrameRef = useRef<HTMLDivElement>(null);
  const currentBeatRef = useRef(-1);
  const visualByBeatRef = useRef<Record<number, Visual>>({});
  const storyCompleteRef = useRef(false);
  const quizReadyRef = useRef(false);
  const historyIdRef = useRef("");
  const currentTopicRef = useRef("");

  // ── Load history on mount ──
  useEffect(() => {
    try {
      const stored = localStorage.getItem(HISTORY_KEY);
      if (stored) setHistory(JSON.parse(stored));
    } catch {}
  }, []);

  const saveToHistory = useCallback((entry: HistoryEntry) => {
    setHistory((prev) => {
      const next = [entry, ...prev.filter((e) => e.id !== entry.id)].slice(0, 20);
      try { localStorage.setItem(HISTORY_KEY, JSON.stringify(next)); } catch {}
      return next;
    });
  }, []);

  // Keep refs in sync
  useEffect(() => { currentBeatRef.current = currentBeat; }, [currentBeat]);
  useEffect(() => { visualByBeatRef.current = visualByBeat; }, [visualByBeat]);

  // Auto-scroll subtitle / status log
  useEffect(() => {
    if (subtitleScrollRef.current)
      subtitleScrollRef.current.scrollTop = subtitleScrollRef.current.scrollHeight;
  }, [subtitles]);

  useEffect(() => {
    if (statusLogRef.current)
      statusLogRef.current.scrollTop = statusLogRef.current.scrollHeight;
  }, [statusLog]);

  const addStatus = useCallback((msg: string) => {
    setStatusLog((prev) => [...prev, msg].slice(-50));
  }, []);

  const addSubtitle = useCallback((text: string) => {
    if (!text.trim()) return;
    setSubtitles((prev) => [...prev, text.trim()].slice(-6));
  }, []);

  const ensureAudio = useCallback(async () => {
    if (!audioEngineRef.current) audioEngineRef.current = new AudioEngine();
    audioEngineRef.current.init();
    await audioEngineRef.current.resume();
  }, []);

  const applyVisual = useCallback((visual: Visual) => {
    setVisualByBeat((prev) => ({ ...prev, [visual.beatIndex]: visual }));
    setCurrentVisual((prev) => {
      if (!prev) return visual;
      if (visual.type === "video" && visual.beatIndex === prev.beatIndex) return visual;
      if (visual.beatIndex >= currentBeatRef.current) return visual;
      return prev;
    });
  }, []);

  // ── WebSocket message handler ──
  const handleMessage = useCallback(
    async (data: string) => {
      let msg: Record<string, unknown>;
      try { msg = JSON.parse(data); } catch { return; }

      const type = msg.type as string;

      switch (type) {
        case "status": {
          addStatus(msg.content as string);
          break;
        }
        case "topic_received": {
          const t = (msg.title as string) || (msg.content as string);
          setDocTitle(t);
          setPhase("playing");
          break;
        }
        case "beat_start": {
          const beatIdx = msg.beat_index as number;
          const total = msg.total_beats as number;
          const dur = (msg.target_duration_seconds as number) || 35;
          setCurrentBeat(beatIdx);
          setTotalBeats(total);
          setBeatDuration(dur);
          setSubtitles([]);
          const cached = visualByBeatRef.current[beatIdx];
          if (cached) setCurrentVisual(cached);
          audioEngineRef.current?.duckBgm();
          break;
        }
        case "beat_end": {
          audioEngineRef.current?.restoreBgm();
          break;
        }
        case "audio_chunk": {
          await ensureAudio();
          if (msg.data) audioEngineRef.current?.enqueueNarrationChunk(msg.data as string);
          break;
        }
        case "narration": {
          addSubtitle(msg.content as string);
          break;
        }
        case "video": {
          const beatIdx =
            typeof msg.beat_index === "number"
              ? (msg.beat_index as number)
              : currentBeatRef.current;
          applyVisual({
            type: "video",
            data: msg.data as string,
            mimeType: (msg.mime_type as string) || "video/mp4",
            beatIndex: beatIdx,
          });
          break;
        }
        case "image": {
          const beatIdx =
            typeof msg.beat_index === "number"
              ? (msg.beat_index as number)
              : currentBeatRef.current;
          applyVisual({
            type: "image",
            data: msg.data as string,
            mimeType: (msg.mime_type as string) || "image/png",
            beatIndex: beatIdx,
          });
          break;
        }
        case "bgm_audio": {
          await ensureAudio();
          if (msg.data) {
            await audioEngineRef.current?.playBgm(msg.data as string);
            setBgmActive(true);
          }
          break;
        }
        case "bgm_fallback": {
          setBgmActive(false);
          break;
        }
        case "quiz_data": {
          const questions = msg.questions as QuizQuestion[];
          if (questions?.length > 0) {
            setQuiz({
              questions,
              currentIdx: 0,
              answers: new Array(questions.length).fill(null),
              score: 0,
              showExplanation: false,
              topic: (msg.topic as string) || currentTopicRef.current,
            });
            quizReadyRef.current = true;
            if (storyCompleteRef.current) setPhase("quiz");
          }
          break;
        }
        case "story_complete": {
          audioEngineRef.current?.restoreBgm();
          storyCompleteRef.current = true;
          if (quizReadyRef.current) {
            setPhase("quiz");
          } else {
            saveToHistory({
              id: historyIdRef.current,
              topic: currentTopicRef.current,
              date: new Date().toISOString(),
            });
          }
          break;
        }
        case "interrupted": {
          addStatus(`[${msg.source as string}] Interruption detected`);
          break;
        }
        case "error": {
          addStatus(`ERROR: ${msg.content as string}`);
          break;
        }
      }
    },
    [addStatus, addSubtitle, applyVisual, ensureAudio, saveToHistory]
  );

  // ── Connect WebSocket ──
  const connect = useCallback(async () => {
    if (wsRef.current) wsRef.current.close();
    await ensureAudio();
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;
    ws.onopen = () => addStatus("Connected to Chronos Cinema backend");
    ws.onmessage = (e) => handleMessage(e.data as string);
    ws.onclose = () => addStatus("Disconnected from backend");
    ws.onerror = () => addStatus("WebSocket error — check backend is running on port 8000");
  }, [addStatus, ensureAudio, handleMessage]);

  // ── Start documentary ──
  const startDocumentary = useCallback(async () => {
    if (!topic.trim()) return;
    await connect();

    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    historyIdRef.current = id;
    currentTopicRef.current = topic.trim();

    setPhase("loading");
    setCurrentBeat(-1);
    setSubtitles([]);
    setStatusLog([]);
    setCurrentVisual(null);
    setVisualByBeat({});
    setBgmActive(false);
    setQuiz(null);
    setDocTitle(topic);
    setIsPaused(false);
    storyCompleteRef.current = false;
    quizReadyRef.current = false;

    await new Promise((resolve) => setTimeout(resolve, 400));
    wsRef.current?.send(
      JSON.stringify({ type: "start", topic: topic.trim(), name: userName.trim() })
    );
  }, [topic, userName, connect]);

  // ── Pause / Resume ──
  const togglePause = useCallback(async () => {
    if (isPaused) {
      await audioEngineRef.current?.resume();
      videoRef.current?.play().catch(() => {});
      setIsPaused(false);
    } else {
      await audioEngineRef.current?.suspend();
      videoRef.current?.pause();
      setIsPaused(true);
    }
  }, [isPaused]);

  // ── Stop ──
  const stopPlayback = useCallback(() => {
    wsRef.current?.close();
    wsRef.current = null;
    audioEngineRef.current?.destroy();
    audioEngineRef.current = null;
    mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    setMicEnabled(false);
    setIsPaused(false);
    setPhase("idle");
  }, []);

  // ── Toggle theater / default ──
  const toggleVideoSize = useCallback(() => {
    setVideoSize((prev) => (prev === "default" ? "theater" : "default"));
  }, []);

  // ── Fullscreen ──
  const toggleFullscreen = useCallback(() => {
    if (!cinemaFrameRef.current) return;
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else {
      cinemaFrameRef.current.requestFullscreen();
    }
  }, []);

  // ── Mic recording ──
  const toggleMic = useCallback(async () => {
    if (micEnabled) {
      mediaRecorderRef.current?.stop();
      mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
      mediaRecorderRef.current = null;
      mediaStreamRef.current = null;
      setMicEnabled(false);
      wsRef.current?.send(JSON.stringify({ type: "user_audio_end" }));
    } else {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, sampleRate: 16000, echoCancellation: true, noiseSuppression: true },
        });
        mediaStreamRef.current = stream;
        const recorder = new MediaRecorder(stream, { mimeType: "audio/webm;codecs=opus" });
        recorder.ondataavailable = async (e) => {
          if (e.data.size === 0 || !wsRef.current) return;
          const reader = new FileReader();
          reader.onloadend = () => {
            const base64 = (reader.result as string).split(",")[1];
            wsRef.current?.send(JSON.stringify({ type: "user_audio", data: base64 }));
          };
          reader.readAsDataURL(e.data);
        };
        recorder.start(250);
        mediaRecorderRef.current = recorder;
        setMicEnabled(true);
      } catch (err) {
        addStatus(`Mic error: ${err}`);
      }
    }
  }, [micEnabled, addStatus]);

  // ── Chat ──
  const sendChat = useCallback((text: string) => {
    if (!text.trim() || !wsRef.current) return;
    wsRef.current.send(JSON.stringify({ type: "user_chat", content: text.trim() }));
  }, []);

  // ── Update video element when visual changes ──
  useEffect(() => {
    if (!currentVisual || currentVisual.type !== "video" || !videoRef.current) return;
    const blob = base64ToBlob(currentVisual.data, currentVisual.mimeType);
    const url = URL.createObjectURL(blob);
    videoRef.current.src = url;
    if (!isPaused) videoRef.current.play().catch(() => {});
    return () => { URL.revokeObjectURL(url); };
  }, [currentVisual]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Cleanup on unmount ──
  useEffect(() => {
    return () => {
      wsRef.current?.close();
      audioEngineRef.current?.destroy();
      mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    };
  }, []);

  // ── Quiz handlers ──
  const answerQuiz = useCallback(
    (optionLetter: string) => {
      if (!quiz || quiz.showExplanation) return;
      const q = quiz.questions[quiz.currentIdx];
      const isCorrect = optionLetter === q.correct;
      const newAnswers = [...quiz.answers];
      newAnswers[quiz.currentIdx] = optionLetter;
      setQuiz((prev) =>
        prev
          ? { ...prev, answers: newAnswers, score: isCorrect ? prev.score + 1 : prev.score, showExplanation: true }
          : prev
      );
    },
    [quiz]
  );

  const nextQuizQuestion = useCallback(() => {
    if (!quiz) return;
    const isLast = quiz.currentIdx + 1 >= quiz.questions.length;
    if (isLast) {
      saveToHistory({
        id: historyIdRef.current,
        topic: currentTopicRef.current,
        date: new Date().toISOString(),
        score: quiz.score,
        total: quiz.questions.length,
      });
      setPhase("done");
    } else {
      setQuiz((prev) =>
        prev ? { ...prev, currentIdx: prev.currentIdx + 1, showExplanation: false } : prev
      );
    }
  }, [quiz, saveToHistory]);

  // ─── Render ───────────────────────────────────────────────────────────────

  return (
    <div className="min-h-screen bg-cinema-black flex flex-col">

      {/* ── Header ── */}
      <header className="fixed top-0 left-0 right-0 z-50 flex items-center justify-between px-5 py-2.5 border-b border-cinema-border bg-cinema-black/95 backdrop-blur-md">
        <button
          onClick={() => (phase !== "idle" ? stopPlayback() : undefined)}
          className="flex items-center gap-3 group"
        >
          <span className="text-cinema-gold font-bold tracking-widest text-xs uppercase group-hover:text-cinema-gold-light transition-colors">
            ◈ CHRONOS CINEMA
          </span>
          {docTitle && phase !== "idle" && (
            <span className="hidden md:block text-cinema-muted text-xs truncate max-w-xs opacity-70">
              / {docTitle}
            </span>
          )}
        </button>

        <div className="flex items-center gap-3">
          {/* BGM indicator */}
          {bgmActive && phase === "playing" && !isPaused && (
            <div className="flex items-end gap-0.5 h-3.5">
              {[0, 1, 2, 3, 4].map((i) => (
                <div key={i} className="wave-bar w-[3px] bg-cinema-gold rounded-full" style={{ height: "8px" }} />
              ))}
            </div>
          )}

          {/* Beat label */}
          {phase === "playing" && (
            <span className="text-xs text-cinema-muted tabular-nums hidden sm:block">
              {currentBeat >= 0 ? BEAT_LABELS[currentBeat] : "…"} · {Math.max(0, currentBeat + 1)}/{totalBeats}
            </span>
          )}

          {/* Playback controls in header */}
          {phase === "playing" && (
            <div className="flex items-center gap-0.5">
              <button
                onClick={togglePause}
                title={isPaused ? "Resume" : "Pause"}
                className="p-2 rounded-lg text-cinema-muted hover:text-cinema-text hover:bg-cinema-card transition-all text-xs"
              >
                {isPaused ? "▶" : "⏸"}
              </button>
              <button
                onClick={stopPlayback}
                title="Stop"
                className="p-2 rounded-lg text-cinema-muted hover:text-red-400 hover:bg-cinema-card transition-all text-xs"
              >
                ⏹
              </button>
              <button
                onClick={toggleVideoSize}
                title={videoSize === "default" ? "Theater mode" : "Default view"}
                className="p-2 rounded-lg text-cinema-muted hover:text-cinema-text hover:bg-cinema-card transition-all text-xs hidden sm:block"
              >
                {videoSize === "default" ? "⊞" : "⊟"}
              </button>
            </div>
          )}
        </div>
      </header>

      {/* ── IDLE ── */}
      {phase === "idle" && (
        <IdleView
          topic={topic}
          setTopic={setTopic}
          userName={userName}
          setUserName={setUserName}
          inputRef={inputRef}
          onStart={startDocumentary}
          history={history}
          onClearHistory={() => {
            setHistory([]);
            try { localStorage.removeItem(HISTORY_KEY); } catch {}
          }}
          onPickTopic={(t) => setTopic(t)}
        />
      )}

      {/* ── LOADING ── */}
      {phase === "loading" && (
        <div className="flex-1 flex items-center justify-center pt-20">
          <div className="text-center space-y-6 animate-fade-in max-w-md px-6 w-full">
            {/* Spinner */}
            <div className="relative w-14 h-14 mx-auto">
              <div className="absolute inset-0 rounded-full border border-cinema-gold/15" />
              <div className="absolute inset-0 rounded-full border-2 border-transparent border-t-cinema-gold animate-spin" />
              <div
                className="absolute inset-[5px] rounded-full border border-transparent border-t-cinema-gold/40 animate-spin"
                style={{ animationDirection: "reverse", animationDuration: "1.4s" }}
              />
            </div>

            <div>
              <p className="text-cinema-muted text-xs uppercase tracking-widest font-semibold mb-1">Producing</p>
              <h2 className="text-lg font-semibold text-cinema-text">&ldquo;{topic}&rdquo;</h2>
            </div>

            <div
              ref={statusLogRef}
              className="status-log text-left bg-cinema-card border border-cinema-border rounded-xl p-4 h-44 overflow-y-auto"
            >
              {statusLog.length === 0 && (
                <div className="text-xs text-cinema-muted/50 font-mono">Waiting for backend…</div>
              )}
              {statusLog.map((s, i) => (
                <div key={i} className="text-xs text-cinema-muted font-mono leading-relaxed">
                  <span className="text-cinema-gold/30 select-none">›</span> {s}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ── PLAYING ── */}
      {phase === "playing" && (
        <div className="flex-1 flex flex-col pt-12">

          {/* Beat progress bar */}
          <div className={`px-4 py-2 flex gap-1 ${videoSize === "default" ? "max-w-5xl mx-auto w-full" : ""}`}>
            {Array.from({ length: totalBeats }).map((_, i) => (
              <div
                key={i}
                className={`flex-1 h-px rounded-full beat-segment transition-all duration-500 ${
                  i < currentBeat
                    ? "bg-cinema-gold"
                    : i === currentBeat
                    ? "bg-cinema-gold-light animate-pulse-slow"
                    : "bg-cinema-border"
                }`}
                title={BEAT_LABELS[i]}
              />
            ))}
          </div>

          {/* Cinema frame */}
          <div className={videoSize === "default" ? "max-w-5xl mx-auto w-full px-4" : "w-full"}>
            <div
              ref={cinemaFrameRef}
              className={`cinema-frame bg-black relative scanlines group ${
                videoSize === "theater" ? "theater" : "rounded-xl overflow-hidden"
              }`}
            >
              {/* Vignette */}
              <div className="absolute inset-0 vignette z-10" />

              {/* Loading skeleton */}
              {!currentVisual && (
                <div className="absolute inset-0 flex items-center justify-center z-[5] bg-cinema-black">
                  <div className="text-center space-y-4">
                    <div className="w-10 h-10 border border-cinema-gold/30 border-t-cinema-gold/80 rounded-full animate-spin mx-auto" />
                    <p className="text-cinema-gold/80 text-sm font-medium">Composing opening scene…</p>
                    <p className="text-cinema-muted text-xs">Narrator waits for the first frame</p>
                  </div>
                </div>
              )}

              {/* Video */}
              {currentVisual?.type === "video" && (
                <video
                  key={`video-${currentVisual.beatIndex}`}
                  ref={videoRef}
                  className="w-full h-full object-cover animate-fade-in"
                  muted
                  loop
                  playsInline
                  autoPlay
                />
              )}

              {/* Image with Ken Burns */}
              {currentVisual?.type === "image" && (
                <div
                  key={`${currentVisual.beatIndex}-${currentVisual.data.slice(0, 8)}`}
                  className="absolute inset-0 overflow-hidden"
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={`data:${currentVisual.mimeType};base64,${currentVisual.data}`}
                    alt="Documentary visual"
                    className={`w-full h-full object-cover ken-burns-${currentBeat % 8}`}
                    style={{ "--kb-duration": `${beatDuration + 4}s` } as React.CSSProperties}
                  />
                </div>
              )}

              {/* Beat label — top left */}
              {currentBeat >= 0 && (
                <div className="absolute top-3 left-3 z-20">
                  <span className="text-[10px] text-cinema-gold font-semibold uppercase tracking-widest glass px-2.5 py-1 rounded-full">
                    {BEAT_LABELS[currentBeat] || `Beat ${currentBeat + 1}`}
                  </span>
                </div>
              )}

              {/* Top-right overlay controls — appear on hover */}
              <div className="absolute top-3 right-3 z-20 flex items-center gap-1.5 opacity-0 group-hover:opacity-100 transition-opacity duration-200">
                {isPaused && (
                  <span className="text-[10px] text-yellow-300 glass px-2 py-1 rounded-full">PAUSED</span>
                )}
                {bgmActive && !isPaused && (
                  <span className="text-[10px] text-cinema-muted glass px-2 py-1 rounded-full flex items-center gap-1">
                    <span className="w-1.5 h-1.5 bg-cinema-gold rounded-full recording-dot inline-block" />
                    LIVE
                  </span>
                )}
                <button
                  onClick={toggleVideoSize}
                  title={videoSize === "default" ? "Theater mode" : "Default view"}
                  className="w-7 h-7 glass rounded-full flex items-center justify-center text-cinema-muted hover:text-cinema-text transition-colors text-xs"
                >
                  {videoSize === "default" ? "⊞" : "⊟"}
                </button>
                <button
                  onClick={toggleFullscreen}
                  title="Fullscreen"
                  className="w-7 h-7 glass rounded-full flex items-center justify-center text-cinema-muted hover:text-cinema-text transition-colors text-xs"
                >
                  ⛶
                </button>
              </div>

              {/* Pause overlay — click to resume */}
              {isPaused && (
                <button
                  className="absolute inset-0 z-30 flex items-center justify-center"
                  onClick={togglePause}
                >
                  <div className="w-16 h-16 glass rounded-full flex items-center justify-center border border-white/15 hover:border-white/30 transition-colors">
                    <span className="text-2xl text-white ml-1">▶</span>
                  </div>
                </button>
              )}
            </div>
          </div>

          {/* ── Control bar ── */}
          <div className={`mt-2 px-4 ${videoSize === "default" ? "max-w-5xl mx-auto w-full" : ""}`}>
            <div className="flex items-center gap-1.5 flex-wrap">

              {/* Pause / Resume */}
              <button
                onClick={togglePause}
                className={`ctrl-btn ${
                  isPaused
                    ? "bg-cinema-gold text-cinema-black hover:bg-cinema-gold-light"
                    : "bg-cinema-card border border-cinema-border text-cinema-text hover:border-cinema-gold/40"
                }`}
              >
                {isPaused ? "▶ Resume" : "⏸ Pause"}
              </button>

              {/* Stop */}
              <button
                onClick={stopPlayback}
                className="ctrl-btn bg-cinema-card border border-cinema-border text-cinema-text hover:border-red-500/50 hover:text-red-400"
              >
                ⏹ Stop
              </button>

              <div className="flex-1" />

              {/* Mic */}
              <button
                onClick={toggleMic}
                className={`ctrl-btn ${
                  micEnabled
                    ? "bg-red-600 text-white hover:bg-red-700"
                    : "bg-cinema-card border border-cinema-border text-cinema-text hover:border-cinema-gold/40"
                }`}
              >
                {micEnabled ? (
                  <>
                    <span className="w-1.5 h-1.5 bg-white rounded-full recording-dot" />
                    Listening
                  </>
                ) : (
                  <>🎙 Speak</>
                )}
              </button>

              {/* Chat */}
              <input
                ref={chatInputRef}
                type="text"
                placeholder="Interrupt or ask…"
                className="w-44 sm:w-56 bg-cinema-card border border-cinema-border rounded-lg px-3 py-1.5 text-sm text-cinema-text placeholder-cinema-muted focus:outline-none focus:border-cinema-gold/40 transition-colors"
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    sendChat((e.target as HTMLInputElement).value);
                    (e.target as HTMLInputElement).value = "";
                  }
                }}
              />

              {/* Log toggle */}
              <button
                onClick={() => setShowStatusLog((p) => !p)}
                className="ctrl-btn bg-cinema-card border border-cinema-border text-cinema-muted hover:border-cinema-gold/30 hover:text-cinema-text"
              >
                {showStatusLog ? "Hide Log" : "Log"}
              </button>
            </div>
          </div>

          {/* Subtitles */}
          <div
            ref={subtitleScrollRef}
            className={`mt-2 px-4 min-h-[4rem] max-h-[4.5rem] overflow-hidden flex flex-col justify-end ${
              videoSize === "default" ? "max-w-5xl mx-auto w-full" : ""
            }`}
          >
            {subtitles.slice(-3).map((line, i, arr) => (
              <p
                key={i}
                className={`subtitle-text text-center transition-opacity duration-300 ${
                  i === arr.length - 1 ? "text-white" : "text-cinema-muted/60"
                }`}
              >
                {line}
              </p>
            ))}
          </div>

          {/* Collapsible status log */}
          {showStatusLog && (
            <div
              ref={statusLogRef}
              className={`status-log mb-4 mt-1 px-4 ${videoSize === "default" ? "max-w-5xl mx-auto w-full" : ""}`}
            >
              <div className="bg-cinema-card/50 border border-cinema-border/50 rounded-lg p-3 h-24 overflow-y-auto">
                {statusLog.slice(-20).map((s, i) => (
                  <div key={i} className="text-xs text-cinema-muted font-mono leading-relaxed">
                    <span className="text-cinema-gold/25 select-none">›</span> {s}
                  </div>
                ))}
              </div>
            </div>
          )}

        </div>
      )}

      {/* ── QUIZ ── */}
      {phase === "quiz" && quiz && (
        <QuizView
          quiz={quiz}
          onAnswer={answerQuiz}
          onNext={nextQuizQuestion}
          onFinish={() => setPhase("done")}
        />
      )}

      {/* ── DONE ── */}
      {phase === "done" && quiz && (
        <DoneView
          quiz={quiz}
          topic={topic}
          onRestart={() => {
            setPhase("idle");
            setTopic("");
            setQuiz(null);
          }}
        />
      )}
    </div>
  );
}

// ─── Idle View ────────────────────────────────────────────────────────────────

function IdleView({
  topic, setTopic, userName, setUserName, inputRef, onStart,
  history, onClearHistory, onPickTopic,
}: {
  topic: string;
  setTopic: (t: string) => void;
  userName: string;
  setUserName: (n: string) => void;
  inputRef: React.RefObject<HTMLInputElement>;
  onStart: () => void;
  history: HistoryEntry[];
  onClearHistory: () => void;
  onPickTopic: (t: string) => void;
}) {
  return (
    <div className="flex-1 flex flex-col items-center idle-bg pt-24 pb-16 px-4">
      <div className="w-full max-w-lg animate-fade-in">

        {/* Hero */}
        <div className="text-center mb-10">
          <h1 className="text-[2.75rem] font-bold text-gold-gradient tracking-tight leading-tight mb-3">
            Chronos Cinema
          </h1>
          <p className="text-cinema-muted text-[0.95rem] leading-relaxed max-w-sm mx-auto">
            Enter any topic and watch an AI director, narrator, composer, and cinematographer
            collaborate in real time.
          </p>
        </div>

        {/* Input card */}
        <div className="bg-cinema-card border border-cinema-border rounded-2xl p-6 space-y-4 shadow-2xl">
          <div className="space-y-1.5">
            <label className="text-[10px] font-semibold text-cinema-gold uppercase tracking-widest">
              Documentary Topic
            </label>
            <input
              ref={inputRef}
              type="text"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && onStart()}
              placeholder="Black holes, DNA replication, the Roman Empire…"
              className="w-full bg-cinema-black border border-cinema-border rounded-xl px-4 py-2.5 text-cinema-text placeholder-cinema-muted focus:outline-none focus:border-cinema-gold/50 transition-colors text-[0.9375rem]"
              autoFocus
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-[10px] font-semibold text-cinema-gold uppercase tracking-widest">
              Your Name{" "}
              <span className="text-cinema-muted font-normal normal-case tracking-normal">(optional — narrator addresses you)</span>
            </label>
            <input
              type="text"
              value={userName}
              onChange={(e) => setUserName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && onStart()}
              placeholder="Alex"
              className="w-full bg-cinema-black border border-cinema-border rounded-xl px-4 py-2.5 text-cinema-text placeholder-cinema-muted focus:outline-none focus:border-cinema-gold/50 transition-colors text-[0.9375rem]"
            />
          </div>

          <button
            onClick={onStart}
            disabled={!topic.trim()}
            className="w-full py-3 rounded-xl font-semibold text-[0.9375rem] tracking-wide transition-all duration-200
              bg-cinema-gold text-cinema-black hover:bg-cinema-gold-light
              disabled:opacity-35 disabled:cursor-not-allowed active:scale-[0.98]"
          >
            ▶ Produce My Documentary
          </button>
        </div>

        {/* Feature pills */}
        <div className="mt-5 flex flex-wrap gap-2 justify-center">
          {[
            { icon: "🎬", label: "Live Narration" },
            { icon: "🎥", label: "Veo Video" },
            { icon: "🖼", label: "Imagen Stills" },
            { icon: "🎵", label: "Lyria Score" },
            { icon: "🧠", label: "Knowledge Quiz" },
          ].map((f) => (
            <span
              key={f.label}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-cinema-card border border-cinema-border text-[0.75rem] text-cinema-muted"
            >
              {f.icon} {f.label}
            </span>
          ))}
        </div>

        {/* History */}
        {history.length > 0 && (
          <div className="mt-10">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-[10px] font-semibold text-cinema-gold uppercase tracking-widest">
                Recent Documentaries
              </h3>
              <button
                onClick={onClearHistory}
                className="text-[11px] text-cinema-muted hover:text-cinema-text transition-colors"
              >
                Clear all
              </button>
            </div>

            <div className="space-y-1.5">
              {history.slice(0, 8).map((entry) => (
                <button
                  key={entry.id}
                  onClick={() => onPickTopic(entry.topic)}
                  className="history-card w-full flex items-center justify-between px-4 py-2.5 rounded-xl bg-cinema-card border border-cinema-border hover:border-cinema-gold/30 transition-all group text-left"
                >
                  <div className="flex items-center gap-2.5 min-w-0">
                    <span className="text-cinema-gold/40 text-[11px] flex-shrink-0">◈</span>
                    <span className="text-cinema-text text-sm truncate group-hover:text-white transition-colors">
                      {entry.topic}
                    </span>
                  </div>
                  <div className="flex items-center gap-3 flex-shrink-0 ml-3">
                    {entry.score !== undefined && entry.total !== undefined && (
                      <span
                        className={`text-xs font-medium tabular-nums ${
                          entry.score / entry.total >= 0.8
                            ? "text-green-400"
                            : entry.score / entry.total >= 0.6
                            ? "text-cinema-gold"
                            : "text-cinema-muted"
                        }`}
                      >
                        {entry.score}/{entry.total}
                      </span>
                    )}
                    <span className="text-[11px] text-cinema-muted/60">
                      {new Date(entry.date).toLocaleDateString(undefined, { month: "short", day: "numeric" })}
                    </span>
                  </div>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Quiz Component ───────────────────────────────────────────────────────────

function QuizView({
  quiz, onAnswer, onNext, onFinish,
}: {
  quiz: QuizState;
  onAnswer: (letter: string) => void;
  onNext: () => void;
  onFinish: () => void;
}) {
  const { questions, currentIdx, answers, score, showExplanation } = quiz;
  const q = questions[currentIdx];
  const userAnswer = answers[currentIdx];
  const isLast = currentIdx === questions.length - 1;
  const letters = ["A", "B", "C", "D"];

  return (
    <div className="flex-1 flex items-center justify-center p-6 pt-20 animate-slide-up">
      <div className="w-full max-w-2xl">

        {/* Header */}
        <div className="flex items-center justify-between mb-5">
          <div>
            <p className="text-[10px] text-cinema-gold uppercase tracking-widest font-semibold">Knowledge Check</p>
            <h2 className="text-cinema-text font-semibold mt-0.5 text-sm">{quiz.topic}</h2>
          </div>
          <div className="text-right">
            <p className="text-[11px] text-cinema-muted">
              {currentIdx + 1} / {questions.length}
            </p>
            <p className="text-cinema-gold font-bold text-lg tabular-nums">{score} pts</p>
          </div>
        </div>

        {/* Progress bar */}
        <div className="flex gap-1.5 mb-7">
          {questions.map((_, i) => (
            <div
              key={i}
              className={`flex-1 h-1 rounded-full transition-all duration-300 ${
                i < currentIdx
                  ? answers[i] === questions[i].correct
                    ? "bg-green-500"
                    : "bg-red-500"
                  : i === currentIdx
                  ? "bg-cinema-gold"
                  : "bg-cinema-border"
              }`}
            />
          ))}
        </div>

        {/* Question card */}
        <div className="bg-cinema-card border border-cinema-border rounded-2xl p-7 mb-5">
          <p className="text-base font-semibold text-cinema-text leading-relaxed mb-6">
            {q.question}
          </p>

          <div className="space-y-2.5">
            {q.options.map((option, i) => {
              const letter = letters[i];
              const isChosen = userAnswer === letter;
              const isCorrect = letter === q.correct;

              let cls = "quiz-option border rounded-xl px-4 py-3 flex items-center gap-3";
              if (!showExplanation) {
                cls += " border-cinema-border text-cinema-text";
              } else if (isCorrect) {
                cls += " correct border-green-500 text-green-400";
              } else if (isChosen && !isCorrect) {
                cls += " wrong border-red-500 text-red-400";
              } else {
                cls += " answered border-cinema-border text-cinema-muted opacity-50";
              }

              return (
                <div key={i} className={cls} onClick={() => !showExplanation && onAnswer(letter)}>
                  <span
                    className={`w-7 h-7 flex-shrink-0 rounded-lg border flex items-center justify-center text-xs font-bold ${
                      showExplanation && isCorrect
                        ? "border-green-500 bg-green-500 text-white"
                        : showExplanation && isChosen && !isCorrect
                        ? "border-red-500 bg-red-500 text-white"
                        : "border-cinema-border text-cinema-muted"
                    }`}
                  >
                    {letter}
                  </span>
                  <span className="text-sm leading-relaxed">{option}</span>
                </div>
              );
            })}
          </div>

          {showExplanation && (
            <div className="mt-5 p-4 bg-cinema-black/50 border border-cinema-border/50 rounded-xl animate-slide-up">
              <p className="text-[10px] text-cinema-gold uppercase tracking-widest font-semibold mb-1.5">
                {userAnswer === q.correct ? "✓ Correct!" : "✗ Incorrect"}
              </p>
              <p className="text-sm text-cinema-muted leading-relaxed">{q.explanation}</p>
            </div>
          )}
        </div>

        {showExplanation && (
          <button
            onClick={isLast ? onFinish : onNext}
            className="w-full py-3.5 rounded-xl font-semibold bg-cinema-gold text-cinema-black hover:bg-cinema-gold-light transition-colors"
          >
            {isLast ? "See Final Score →" : "Next Question →"}
          </button>
        )}
      </div>
    </div>
  );
}

// ─── Done Screen ──────────────────────────────────────────────────────────────

function DoneView({
  quiz, topic, onRestart,
}: {
  quiz: QuizState;
  topic: string;
  onRestart: () => void;
}) {
  const { score, questions } = quiz;
  const pct = Math.round((score / questions.length) * 100);
  const grade =
    pct === 100 ? { label: "Perfect Score!", color: "text-cinema-gold", emoji: "🏆" } :
    pct >= 80   ? { label: "Excellent!",     color: "text-green-400",   emoji: "🎉" } :
    pct >= 60   ? { label: "Well Done!",     color: "text-blue-400",    emoji: "👍" } :
                  { label: "Keep Exploring!",color: "text-cinema-muted", emoji: "🔭" };

  return (
    <div className="flex-1 flex items-center justify-center p-6 pt-20 animate-fade-in">
      <div className="w-full max-w-md text-center space-y-7">
        <div>
          <div className="text-5xl mb-3">{grade.emoji}</div>
          <h2 className={`text-3xl font-bold ${grade.color} mb-1.5`}>{grade.label}</h2>
          <p className="text-cinema-muted text-sm">
            You watched a documentary on{" "}
            <span className="text-cinema-text">{topic}</span>
          </p>
        </div>

        <div className="bg-cinema-card border border-cinema-border rounded-2xl p-7">
          <div className="text-5xl font-bold text-cinema-gold mb-1 tabular-nums">
            {score}/{questions.length}
          </div>
          <p className="text-cinema-muted text-sm">{pct}% correct</p>

          <div className="mt-5 space-y-2">
            {questions.map((q, i) => (
              <div key={i} className="flex items-center gap-2.5 text-sm text-left">
                <span
                  className={`w-5 h-5 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0 ${
                    quiz.answers[i] === q.correct ? "bg-green-500 text-white" : "bg-red-500 text-white"
                  }`}
                >
                  {quiz.answers[i] === q.correct ? "✓" : "✗"}
                </span>
                <span className="text-cinema-muted text-xs truncate">{q.question}</span>
              </div>
            ))}
          </div>
        </div>

        <button
          onClick={onRestart}
          className="w-full py-3.5 rounded-xl font-semibold bg-cinema-card border border-cinema-border text-cinema-text hover:border-cinema-gold/40 hover:bg-cinema-card/60 transition-all"
        >
          ◈ Create Another Documentary
        </button>
      </div>
    </div>
  );
}

// ─── Utility ─────────────────────────────────────────────────────────────────

function base64ToBlob(base64: string, mimeType: string): Blob {
  const byteCharacters = atob(base64);
  const byteNumbers = new Array(byteCharacters.length);
  for (let i = 0; i < byteCharacters.length; i++) {
    byteNumbers[i] = byteCharacters.charCodeAt(i);
  }
  return new Blob([new Uint8Array(byteNumbers)], { type: mimeType });
}
