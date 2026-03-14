"use client";

import {
  useEffect,
  useRef,
  useState,
  useCallback,
} from "react";
import { AudioEngine } from "./lib/audioEngine";
import { saveSession, loadSession, StoredSession } from "./lib/sessionStore";

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
  date: string;
  score?: number;
  total?: number;
  saved?: boolean; // IndexedDB session available for replay
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

const NARRATION_SAMPLE_RATE = 24000;

// ─── Main Component ──────────────────────────────────────────────────────────

export default function ChronosCinema() {
  // Core state
  const [phase, setPhase] = useState<Phase>("idle");
  const [topic, setTopic] = useState("");
  const [userName, setUserName] = useState("");
  const [apiKey, setApiKey] = useState<string>(() => {
    try { return sessionStorage.getItem("chronos_api_key") || ""; } catch { return ""; }
  });
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
  const [isReplaying, setIsReplaying] = useState(false);

  // History
  const [history, setHistory] = useState<HistoryEntry[]>([]);

  // Quiz state
  const [quiz, setQuiz] = useState<QuizState | null>(null);

  // Refs — connections
  const wsRef = useRef<WebSocket | null>(null);
  const audioEngineRef = useRef<AudioEngine | null>(null);
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Refs — DOM
  const subtitleScrollRef = useRef<HTMLDivElement>(null);
  const statusLogRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const cinemaFrameRef = useRef<HTMLDivElement>(null);

  // Refs — sync state for callbacks
  const currentBeatRef = useRef(-1);
  const visualByBeatRef = useRef<Record<number, Visual>>({});
  const storyCompleteRef = useRef(false);
  const quizReadyRef = useRef(false);
  const historyIdRef = useRef("");
  const currentTopicRef = useRef("");
  const sessionSavedRef = useRef(false);

  // Refs — replay control
  const isReplayRef = useRef(false);
  const replayAbortRef = useRef(false);

  // Refs — session accumulator (fed during live streaming)
  const accRef = useRef<{
    id: string;
    topic: string;
    docTitle: string;
    totalBeats: number;
    beatData: Record<
      number,
      {
        beatLabel: string;
        subtitles: string[];
        narrationChunks: string[];
        startMs: number;
        durationSeconds: number;
      }
    >;
    bgmBase64: string | null;
    quizQuestions: QuizQuestion[] | null;
  }>({
    id: "",
    topic: "",
    docTitle: "",
    totalBeats: 8,
    beatData: {},
    bgmBase64: null,
    quizQuestions: null,
  });

  // ── Load history from localStorage on mount ──
  useEffect(() => {
    try {
      const s = localStorage.getItem(HISTORY_KEY);
      if (s) setHistory(JSON.parse(s));
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

  // Auto-scroll subtitle + status log
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

  // ── Save completed session to IndexedDB ──
  const saveCurrentSession = useCallback(async (): Promise<boolean> => {
    const acc = accRef.current;
    if (!acc.id || Object.keys(acc.beatData).length === 0) return false;

    const beats = Array.from({ length: acc.totalBeats }, (_, i) => {
      const bd = acc.beatData[i];
      const visual = visualByBeatRef.current[i];
      return {
        beatIndex: i,
        beatLabel: BEAT_LABELS[i] || `Beat ${i + 1}`,
        visual: visual
          ? { type: visual.type, mimeType: visual.mimeType, base64: visual.data }
          : null,
        subtitles: bd?.subtitles ?? [],
        narrationChunks: bd?.narrationChunks ?? [],
        durationSeconds: bd?.durationSeconds ?? 0,
      };
    });

    const session: StoredSession = {
      id: acc.id,
      topic: acc.topic,
      docTitle: acc.docTitle || acc.topic,
      date: new Date().toISOString(),
      totalBeats: acc.totalBeats,
      beats,
      bgmBase64: acc.bgmBase64,
      quiz: acc.quizQuestions,
    };

    try {
      await saveSession(session);
      sessionSavedRef.current = true;
      return true;
    } catch (e) {
      console.warn("[ChronosCinema] Failed to save session:", e);
      return false;
    }
  }, []);

  // ── WebSocket message handler ──
  const storyCompleteRefLocal = useRef(false);
  const quizReadyRefLocal = useRef(false);

  const handleMessage = useCallback(
    async (data: string) => {
      let msg: Record<string, unknown>;
      try { msg = JSON.parse(data); } catch { return; }

      const type = msg.type as string;

      switch (type) {
        case "pong": break; // heartbeat response

        case "status": {
          addStatus(msg.content as string);
          break;
        }

        case "topic_received": {
          const t = (msg.title as string) || (msg.content as string);
          setDocTitle(t);
          accRef.current.docTitle = t;
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

          // Accumulate
          accRef.current.totalBeats = total;
          accRef.current.beatData[beatIdx] = {
            beatLabel: BEAT_LABELS[beatIdx] || `Beat ${beatIdx + 1}`,
            subtitles: [],
            narrationChunks: [],
            startMs: Date.now(),
            durationSeconds: 0,
          };

          const cached = visualByBeatRef.current[beatIdx];
          if (cached) setCurrentVisual(cached);
          audioEngineRef.current?.duckBgm();
          break;
        }

        case "beat_end": {
          audioEngineRef.current?.restoreBgm();
          const beat = msg.beat_index as number ?? currentBeatRef.current;
          const bd = accRef.current.beatData[beat];
          if (bd) bd.durationSeconds = (Date.now() - bd.startMs) / 1000;
          break;
        }

        case "audio_chunk": {
          await ensureAudio();
          const chunk = msg.data as string;
          if (chunk) {
            audioEngineRef.current?.enqueueNarrationChunk(chunk);
            // Accumulate
            const cur = currentBeatRef.current;
            if (cur >= 0 && accRef.current.beatData[cur]) {
              accRef.current.beatData[cur].narrationChunks.push(chunk);
            }
          }
          break;
        }

        case "narration": {
          const text = msg.content as string;
          addSubtitle(text);
          // Accumulate
          const cur = currentBeatRef.current;
          if (cur >= 0 && accRef.current.beatData[cur] && text?.trim()) {
            accRef.current.beatData[cur].subtitles.push(text.trim());
          }
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
            const b64 = msg.data as string;
            await audioEngineRef.current?.playBgm(b64);
            setBgmActive(true);
            accRef.current.bgmBase64 = b64; // accumulate
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
            accRef.current.quizQuestions = questions; // accumulate
            setQuiz({
              questions,
              currentIdx: 0,
              answers: new Array(questions.length).fill(null),
              score: 0,
              showExplanation: false,
              topic: (msg.topic as string) || currentTopicRef.current,
            });
            quizReadyRef.current = true;
            quizReadyRefLocal.current = true;
            if (storyCompleteRefLocal.current) setPhase("quiz");
          }
          break;
        }

        case "story_complete": {
          audioEngineRef.current?.restoreBgm();
          storyCompleteRef.current = true;
          storyCompleteRefLocal.current = true;

          const saved = await saveCurrentSession();

          if (quizReadyRefLocal.current) {
            setPhase("quiz");
          } else {
            saveToHistory({
              id: historyIdRef.current,
              topic: currentTopicRef.current,
              date: new Date().toISOString(),
              saved,
            });
          }
          break;
        }

        case "error": {
          addStatus(`ERROR: ${msg.content as string}`);
          break;
        }
      }
    },
    [addStatus, addSubtitle, applyVisual, ensureAudio, saveCurrentSession, saveToHistory]
  );

  // ── Connect WebSocket (with application-level heartbeat) ──
  const connect = useCallback(async () => {
    if (wsRef.current) wsRef.current.close();
    if (heartbeatRef.current) clearInterval(heartbeatRef.current);
    await ensureAudio();

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      addStatus("Connected to Chronos Cinema backend");
      // Send a ping every 25 s to keep the WS alive during long renders
      heartbeatRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "ping" }));
        }
      }, 25_000);
    };
    ws.onmessage = (e) => handleMessage(e.data as string);
    ws.onclose = () => {
      addStatus("Disconnected from backend");
      if (heartbeatRef.current) {
        clearInterval(heartbeatRef.current);
        heartbeatRef.current = null;
      }
    };
    ws.onerror = () =>
      addStatus("WebSocket error — check backend is running on port 8000");
  }, [addStatus, ensureAudio, handleMessage]);

  // ── Start documentary (live) ──
  const startDocumentary = useCallback(async () => {
    if (!topic.trim()) return;
    await connect();

    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    historyIdRef.current = id;
    currentTopicRef.current = topic.trim();
    sessionSavedRef.current = false;

    // Reset accumulator
    accRef.current = {
      id,
      topic: topic.trim(),
      docTitle: topic.trim(),
      totalBeats: 8,
      beatData: {},
      bgmBase64: null,
      quizQuestions: null,
    };

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
    setIsReplaying(false);
    storyCompleteRef.current = false;
    storyCompleteRefLocal.current = false;
    quizReadyRef.current = false;
    quizReadyRefLocal.current = false;

    await new Promise((resolve) => setTimeout(resolve, 400));
    wsRef.current?.send(
      JSON.stringify({ type: "start", topic: topic.trim(), name: userName.trim(), apiKey: apiKey.trim() })
    );
  }, [topic, userName, apiKey, connect]);

  // ── Replay a saved session ──
  const replayDocumentary = useCallback(
    async (sessionId: string) => {
      let session: StoredSession | null = null;
      try {
        session = await loadSession(sessionId);
      } catch {}

      if (!session || session.beats.length === 0) {
        addStatus("Replay data not found — this session may have been cleared from browser storage.");
        return;
      }

      // Reset UI state
      setPhase("playing");
      setDocTitle(session.docTitle || session.topic);
      setTopic(session.topic);
      setCurrentBeat(-1);
      setTotalBeats(session.totalBeats);
      setSubtitles([]);
      setStatusLog([]);
      setCurrentVisual(null);
      setVisualByBeat({});
      setBgmActive(false);
      setQuiz(null);
      setIsPaused(false);
      setIsReplaying(true);

      isReplayRef.current = true;
      replayAbortRef.current = false;

      await ensureAudio();

      // Start BGM
      if (session.bgmBase64) {
        try {
          await audioEngineRef.current?.playBgm(session.bgmBase64);
          setBgmActive(true);
        } catch {}
      }

      // Play each beat sequentially
      for (const beat of session.beats) {
        if (replayAbortRef.current) break;

        setCurrentBeat(beat.beatIndex);
        setTotalBeats(session.totalBeats);
        setSubtitles([]);

        // Show visual
        if (beat.visual) {
          const v: Visual = {
            type: beat.visual.type,
            data: beat.visual.base64,
            mimeType: beat.visual.mimeType,
            beatIndex: beat.beatIndex,
          };
          setCurrentVisual(v);
          setVisualByBeat((prev) => ({ ...prev, [beat.beatIndex]: v }));
          // Estimate beat duration from visual for Ken Burns timing
          setBeatDuration(beat.durationSeconds > 0 ? beat.durationSeconds : 35);
        }

        audioEngineRef.current?.duckBgm();

        // Calculate narration audio duration from PCM chunk sizes
        let totalPcmSamples = 0;
        for (const chunk of beat.narrationChunks) {
          try {
            totalPcmSamples += atob(chunk).length / 2; // Int16 = 2 bytes/sample
          } catch {}
        }
        const audioDurationMs = (totalPcmSamples / NARRATION_SAMPLE_RATE) * 1000;

        // Schedule all narration chunks
        for (const chunk of beat.narrationChunks) {
          if (replayAbortRef.current) break;
          audioEngineRef.current?.enqueueNarrationChunk(chunk);
        }

        // Show subtitles with timing proportional to audio duration
        if (beat.subtitles.length > 0 && audioDurationMs > 0) {
          const delay = audioDurationMs / beat.subtitles.length;
          beat.subtitles.forEach((sub, i) => {
            setTimeout(() => {
              if (!replayAbortRef.current) addSubtitle(sub);
            }, i * delay);
          });
        }

        // Wait for narration to finish (max = saved duration + 3 s buffer)
        const maxWaitMs = Math.max(audioDurationMs + 3000, beat.durationSeconds * 1000 + 3000, 5000);
        const t0 = Date.now();
        while (
          !replayAbortRef.current &&
          audioEngineRef.current?.isNarrationActive() &&
          Date.now() - t0 < maxWaitMs
        ) {
          await new Promise((r) => setTimeout(r, 200));
        }

        if (replayAbortRef.current) break;

        audioEngineRef.current?.restoreBgm();
        await new Promise((r) => setTimeout(r, 600));
      }

      isReplayRef.current = false;
      setIsReplaying(false);

      if (replayAbortRef.current) return;

      // Transition after replay
      if (session.quiz) {
        setQuiz({
          questions: session.quiz,
          currentIdx: 0,
          answers: new Array(session.quiz.length).fill(null),
          score: 0,
          showExplanation: false,
          topic: session.topic,
        });
        setPhase("quiz");
      } else {
        setPhase("idle");
      }
    },
    [ensureAudio, addSubtitle, addStatus]
  );

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

  // ── Stop (live or replay) ──
  const stopPlayback = useCallback(() => {
    // Abort replay loop first
    replayAbortRef.current = true;
    isReplayRef.current = false;

    // Clean up WS + heartbeat
    if (heartbeatRef.current) {
      clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
    }
    wsRef.current?.close();
    wsRef.current = null;

    audioEngineRef.current?.destroy();
    audioEngineRef.current = null;

    setIsPaused(false);
    setIsReplaying(false);
    setPhase("idle");
  }, []);

  // ── Theater / fullscreen ──
  const toggleVideoSize = useCallback(() => {
    setVideoSize((prev) => (prev === "default" ? "theater" : "default"));
  }, []);

  const toggleFullscreen = useCallback(() => {
    if (!cinemaFrameRef.current) return;
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else {
      cinemaFrameRef.current.requestFullscreen();
    }
  }, []);

  // ── Update <video> when visual changes ──
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
      if (heartbeatRef.current) clearInterval(heartbeatRef.current);
      wsRef.current?.close();
      audioEngineRef.current?.destroy();
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
        saved: sessionSavedRef.current,
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
          <span className="font-display font-semibold tracking-widest text-xs uppercase text-gold-gradient">
            ◈ CHRONOS CINEMA
          </span>
          {docTitle && phase !== "idle" && (
            <span className="hidden md:flex items-center gap-1.5 text-cinema-muted text-xs truncate max-w-xs opacity-70">
              {isReplaying && (
                <span className="text-cinema-gold/60 text-[10px] font-medium border border-cinema-gold/20 px-1.5 py-0.5 rounded-full">
                  ↺ REPLAY
                </span>
              )}
              / {docTitle}
            </span>
          )}
        </button>

        <div className="flex items-center gap-3">
          {bgmActive && phase === "playing" && !isPaused && (
            <div className="flex items-end gap-0.5 h-3.5">
              {[0, 1, 2, 3, 4].map((i) => (
                <div key={i} className="wave-bar w-[3px] bg-cinema-gold rounded-full" style={{ height: "8px" }} />
              ))}
            </div>
          )}

          {phase === "playing" && (
            <span className="text-xs text-cinema-muted tabular-nums hidden sm:block">
              {currentBeat >= 0 ? BEAT_LABELS[currentBeat] : "…"} · {Math.max(0, currentBeat + 1)}/{totalBeats}
            </span>
          )}

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
          apiKey={apiKey}
          setApiKey={(k) => {
            setApiKey(k);
            try { sessionStorage.setItem("chronos_api_key", k); } catch {}
          }}
          inputRef={inputRef}
          onStart={startDocumentary}
          history={history}
          onClearHistory={() => {
            setHistory([]);
            try { localStorage.removeItem(HISTORY_KEY); } catch {}
          }}
          onPickTopic={(t) => setTopic(t)}
          onReplay={replayDocumentary}
        />
      )}

      {/* ── LOADING ── */}
      {phase === "loading" && (
        <div className="flex-1 flex items-center justify-center pt-20">
          <div className="text-center space-y-6 animate-fade-in max-w-md px-6 w-full">
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
            <div ref={statusLogRef} className="status-log text-left bg-cinema-card border border-cinema-border rounded-xl p-4 h-44 overflow-y-auto">
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

          {/* Beat progress */}
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
                {isReplaying && !isPaused && (
                  <span className="text-[10px] text-cinema-gold glass px-2 py-1 rounded-full">↺ REPLAY</span>
                )}
                {bgmActive && !isPaused && !isReplaying && (
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

              {/* Pause overlay */}
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
              <button
                onClick={togglePause}
                className={`ctrl-btn ${
                  isPaused
                    ? "bg-cinema-gold text-white hover:bg-cinema-gold-light"
                    : "bg-cinema-card border border-cinema-border text-cinema-text hover:border-cinema-gold/40"
                }`}
              >
                {isPaused ? "▶ Resume" : "⏸ Pause"}
              </button>
              <button
                onClick={stopPlayback}
                className="ctrl-btn bg-cinema-card border border-cinema-border text-cinema-text hover:border-red-500/50 hover:text-red-400"
              >
                ⏹ Stop
              </button>

              <div className="flex-1" />

              <button
                onClick={() => setShowStatusLog((p) => !p)}
                className="ctrl-btn bg-cinema-card border border-cinema-border text-cinema-muted hover:border-cinema-gold/30 hover:text-cinema-text"
              >
                {showStatusLog ? "Hide Log" : "Log"}
              </button>
            </div>
          </div>

          {/* Collapsible status log */}
          {showStatusLog && (
            <div
              ref={statusLogRef}
              className={`mb-4 mt-1 px-4 ${videoSize === "default" ? "max-w-5xl mx-auto w-full" : ""}`}
            >
              <div className="status-log bg-cinema-card/50 border border-cinema-border/50 rounded-lg p-3 h-24 overflow-y-auto">
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
  topic, setTopic, userName, setUserName, apiKey, setApiKey, inputRef, onStart,
  history, onClearHistory, onPickTopic, onReplay,
}: {
  topic: string;
  setTopic: (t: string) => void;
  userName: string;
  setUserName: (n: string) => void;
  apiKey: string;
  setApiKey: (k: string) => void;
  inputRef: React.RefObject<HTMLInputElement>;
  onStart: () => void;
  history: HistoryEntry[];
  onClearHistory: () => void;
  onPickTopic: (t: string) => void;
  onReplay: (id: string) => void;
}) {
  return (
    <div className="flex-1 flex flex-col items-center idle-bg pt-24 pb-16 px-4">
      <div className="w-full max-w-lg animate-fade-in">

        {/* Hero */}
        <div className="text-center mb-10">
          <h1 className="text-[2.75rem] font-display font-bold text-gold-gradient tracking-tight leading-tight mb-3">
            Chronos Cinema
          </h1>
          <p className="text-cinema-muted text-[0.95rem] leading-relaxed max-w-sm mx-auto">
            Enter any topic and watch an AI director, narrator, composer, and
            cinematographer collaborate in real time.
          </p>
        </div>

        {/* Input card */}
        <div className="bg-cinema-card border border-cinema-border rounded-2xl p-6 space-y-4 shadow-2xl">
          <div className="space-y-1.5">
            <label className="text-[10px] font-semibold text-cinema-gold uppercase tracking-widest flex items-center gap-2">
              Gemini API Key
              <a
                href="https://aistudio.google.com/apikey"
                target="_blank"
                rel="noopener noreferrer"
                className="text-cinema-muted font-normal normal-case tracking-normal hover:text-cinema-gold transition-colors"
              >
                ↗ Get one free
              </a>
            </label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="AIza…"
              className="w-full bg-cinema-black border border-cinema-border rounded-xl px-4 py-2.5 text-cinema-text placeholder-cinema-muted focus:outline-none focus:border-cinema-gold/50 transition-colors text-[0.9375rem] font-mono"
              autoFocus
            />
            <p className="text-[10px] text-cinema-muted leading-relaxed">
              Stored in browser session only — never sent to any server other than your own backend.
            </p>
          </div>

          <div className="space-y-1.5">
            <label className="text-[10px] font-semibold text-cinema-gold uppercase tracking-widest">
              Documentary Topic
            </label>
            <input
              ref={inputRef}
              type="text"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && apiKey.trim() && onStart()}
              placeholder="Black holes, DNA replication, the Roman Empire…"
              className="w-full bg-cinema-black border border-cinema-border rounded-xl px-4 py-2.5 text-cinema-text placeholder-cinema-muted focus:outline-none focus:border-cinema-gold/50 transition-colors text-[0.9375rem]"
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-[10px] font-semibold text-cinema-gold uppercase tracking-widest">
              Your Name{" "}
              <span className="text-cinema-muted font-normal normal-case tracking-normal">
                (optional — narrator addresses you)
              </span>
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
            disabled={!topic.trim() || !apiKey.trim()}
            className="w-full py-3 rounded-xl font-semibold text-[0.9375rem] tracking-wide btn-produce"
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
              {history.slice(0, 10).map((entry) => (
                <div
                  key={entry.id}
                  className="history-card flex items-center gap-2 px-3 py-2.5 rounded-xl bg-cinema-card border border-cinema-border hover:border-cinema-gold/25 transition-all group"
                >
                  {/* Topic — click to pre-fill */}
                  <button
                    onClick={() => onPickTopic(entry.topic)}
                    className="flex-1 flex items-center gap-2.5 min-w-0 text-left"
                  >
                    <span className="text-cinema-gold/40 text-[11px] flex-shrink-0">◈</span>
                    <span className="text-cinema-text text-sm truncate group-hover:text-white transition-colors">
                      {entry.topic}
                    </span>
                  </button>

                  {/* Quiz score */}
                  {entry.score !== undefined && entry.total !== undefined && (
                    <span
                      className={`text-xs font-medium tabular-nums flex-shrink-0 ${
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

                  {/* Date */}
                  <span className="text-[11px] text-cinema-muted/60 flex-shrink-0">
                    {new Date(entry.date).toLocaleDateString(undefined, {
                      month: "short",
                      day: "numeric",
                    })}
                  </span>

                  {/* Replay button — only shown if session saved */}
                  {entry.saved && (
                    <button
                      onClick={() => onReplay(entry.id)}
                      title="Replay saved session (no API calls)"
                      className="flex-shrink-0 flex items-center gap-1 px-2 py-1 rounded-lg bg-cinema-black border border-cinema-gold/20 text-cinema-gold text-[10px] font-semibold hover:border-cinema-gold/50 hover:bg-cinema-gold/5 transition-all"
                    >
                      ↺ Replay
                    </button>
                  )}
                </div>
              ))}
            </div>

            <p className="mt-3 text-[10px] text-cinema-muted/40 text-center">
              Click a topic to re-create · ↺ Replay plays from saved session without API calls
            </p>
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

        <div className="flex items-center justify-between mb-5">
          <div>
            <p className="text-[10px] text-cinema-gold uppercase tracking-widest font-semibold">Knowledge Check</p>
            <h2 className="text-cinema-text font-semibold mt-0.5 text-sm">{quiz.topic}</h2>
          </div>
          <div className="text-right">
            <p className="text-[11px] text-cinema-muted">{currentIdx + 1} / {questions.length}</p>
            <p className="text-cinema-gold font-bold text-lg tabular-nums">{score} pts</p>
          </div>
        </div>

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

        <div className="bg-cinema-card border border-cinema-border rounded-2xl p-7 mb-5">
          <p className="text-base font-semibold text-cinema-text leading-relaxed mb-6">{q.question}</p>

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
            className="w-full py-3.5 rounded-xl font-semibold btn-produce"
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
    pct === 100 ? { label: "Perfect Score!", color: "text-cinema-gold",  emoji: "🏆" } :
    pct >= 80   ? { label: "Excellent!",     color: "text-green-400",    emoji: "🎉" } :
    pct >= 60   ? { label: "Well Done!",     color: "text-blue-400",     emoji: "👍" } :
                  { label: "Keep Exploring!",color: "text-cinema-muted", emoji: "🔭" };

  return (
    <div className="flex-1 flex items-center justify-center p-6 pt-20 animate-fade-in">
      <div className="w-full max-w-md text-center space-y-7">
        <div>
          <div className="text-5xl mb-3">{grade.emoji}</div>
          <h2 className={`text-3xl font-bold ${grade.color} mb-1.5`}>{grade.label}</h2>
          <p className="text-cinema-muted text-sm">
            You watched a documentary on <span className="text-cinema-text">{topic}</span>
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
